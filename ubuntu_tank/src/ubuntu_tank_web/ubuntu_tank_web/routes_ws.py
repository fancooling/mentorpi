"""
WebSocket Control Endpoint for MentorPi Pi 5 Web Control.

Implements WSS /api/v1/control:
1. Strict Origin verification (rejects cross-origin connections with 1008 Policy Violation).
2. Dedicated IPC socket connection per active controlling session, guaranteeing fail-closed
   disarm and stop upon browser disconnect or crash.
3. Stop priority: immediate processing of stop messages.
4. Bounded queue and latest-intent handling: at most one pending intent; queue congestion
   closes the socket rather than buffering delayed motion commands.
5. Challenge relay: pushes cryptographic challenge tokens to the active browser controller at 20 Hz.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.websockets import WebSocketState
from ubuntu_tank_protocol.constants import (
    CHALLENGE_INTERVAL_SEC,
    MAX_IPC_MESSAGE_BYTES,
)
from ubuntu_tank_protocol.enums import MotionDirection, WebControlErrorCode
from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["websocket"])


def _verify_ws_origin(websocket: WebSocket, allowed_origins: list[str]) -> bool:
    """Verify that WebSocket Origin header is present and matches allowed origins."""
    origin = websocket.headers.get("origin")
    if not origin:
        # Browser WebSocket connections without Origin are rejected
        return False
    # Exact match or normalized trailing slash
    clean_origin = origin.rstrip("/")
    for allowed in allowed_origins:
        if clean_origin == allowed.rstrip("/"):
            return True
    return False


@router.websocket("/control")
async def websocket_control_endpoint(websocket: WebSocket) -> None:
    """
    Dedicated WebSocket endpoint for real-time motion command delivery.

    Enforces Origin checks, exclusive binding to the active owner connection via
    a single-use bind_token, stop message priority, bounded 1-item intent queue,
    and fail-closed disarm upon disconnect.
    """
    allowed_origins = getattr(websocket.app.state, "allowed_origins", None)
    if allowed_origins is None and hasattr(websocket.app.state, "config"):
        allowed_origins = getattr(websocket.app.state.config, "allowed_origins", [])
    if allowed_origins is None:
        allowed_origins = []

    if not _verify_ws_origin(websocket, allowed_origins):
        logger.warning(
            "Rejecting WebSocket connection: invalid or missing Origin '%s'",
            websocket.headers.get("origin"),
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    relay = websocket.app.state.operator_relay
    challenge_interval = getattr(
        getattr(websocket.app.state, "config", None),
        "challenge_interval_sec",
        CHALLENGE_INTERVAL_SEC,
    )

    # Session identifier and state
    session_id = uuid.uuid4().hex
    session_ipc: OperatorIpcClient | None = None
    bound_operator_id: str | None = None
    bound_epoch: int | None = None

    # Intent queue with backpressure: at most 1 item
    intent_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    running = True
    stop_requested = False
    ws_lock = asyncio.Lock()

    async def _send_frame(type_: str, payload: dict[str, Any]) -> None:
        """Send a JSON message frame to the browser."""
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text(
                    json.dumps({"type": type_, "payload": payload})
                )
            except Exception:
                pass

    async def _challenge_loop() -> None:
        """Periodic loop relaying cryptographic challenges to the browser."""
        nonlocal bound_epoch
        while running:
            cycle_started = asyncio.get_running_loop().time()
            try:
                if session_ipc is not None:
                    # Yield immediately if an intent or stop is queued or requested
                    if not intent_queue.empty() or stop_requested:
                        await asyncio.sleep(0.01)
                        continue

                    current_epoch = relay.active_epoch or bound_epoch
                    if current_epoch is None:
                        await asyncio.sleep(challenge_interval)
                        continue

                    async with ws_lock:
                        if not intent_queue.empty() or stop_requested:
                            await asyncio.sleep(0.01)
                            continue
                        c = await asyncio.to_thread(
                            session_ipc.request_challenge, current_epoch, 0.1
                        )
                    if c and c.get("success") and "token" in c:
                        bound_epoch = c["epoch"]
                        await _send_frame(
                            "challenge",
                            {
                                "input_generation": c["input_generation"],
                                "recovery_required": c["recovery_required"],
                                "token": c["token"],
                                "epoch": c["epoch"],
                                "deadline_monotonic_ns": c["deadline_monotonic_ns"],
                                "issued_monotonic_ns": c["issued_monotonic_ns"],
                            },
                        )
                # Processing consumes the interval; adding a full sleep after
                # IPC work needlessly spends the configured input lease budget.
                elapsed = asyncio.get_running_loop().time() - cycle_started
                # After an overrun, yield to incoming intents/Stop rather than
                # issuing catch-up challenges that compete for the IPC lock.
                await asyncio.sleep(max(0.01, challenge_interval - elapsed))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Challenge loop exception: %s", exc)
                await asyncio.sleep(challenge_interval)

    challenge_task = asyncio.create_task(_challenge_loop())

    try:
        while True:
            raw_text = await websocket.receive_text()
            if len(raw_text.encode("utf-8")) > MAX_IPC_MESSAGE_BYTES:
                await _send_frame(
                    "error",
                    {
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": "Message exceeds frame cap",
                    },
                )
                await websocket.close(code=status.WS_1009_MESSAGE_TOO_BIG)
                break

            try:
                msg = json.loads(raw_text)
            except Exception as exc:
                await _send_frame(
                    "error",
                    {
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": f"Malformed JSON: {exc}",
                    },
                )
                continue

            action = msg.get("action")
            raw_payload = msg.get("payload")
            payload = (
                raw_payload
                if isinstance(raw_payload, dict)
                else {k: v for k, v in msg.items() if k != "action"}
            )

            # 1. Stop action has immediate priority
            if action == "stop":
                stop_requested = True
                try:
                    req_id = payload.get("request_id", "ws-stop")
                    epoch = payload.get("epoch")
                    # Clear any pending intent
                    while not intent_queue.empty():
                        try:
                            intent_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    disarmed = False
                    if session_ipc is not None:
                        async with ws_lock:
                            ok, _ = await asyncio.to_thread(
                                session_ipc.stop, req_id, epoch, 1.0
                            )
                    else:
                        ok, _ = await asyncio.to_thread(relay.stop, req_id, epoch, 1.0)

                    if ok:
                        disarmed = True
                    else:
                        lifecycle = websocket.app.state.lifecycle_client
                        lc_ok, lc_state, _ = await asyncio.to_thread(
                            lifecycle.stop_controller
                        )
                        ok = lc_ok
                        disarmed = bool(lc_ok and lc_state == "inactive")

                    await _send_frame(
                        "ack", {"action": "stop", "success": ok, "disarmed": disarmed}
                    )
                finally:
                    stop_requested = False

            # 2. Bind action to attach active operator session
            elif action == "bind":
                op_id = payload.get("operator_id")
                epoch = payload.get("epoch")
                bind_token = payload.get("bind_token")
                if not op_id or epoch is None or not bind_token:
                    await _send_frame(
                        "error",
                        {
                            "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                            "message": "bind requires operator_id, epoch, and bind_token",
                        },
                    )
                    continue

                # Exclusively claim active owner connection using single-use bind token
                ok, owner_ipc, err = await asyncio.to_thread(
                    relay.claim_owner_binding,
                    str(op_id),
                    int(epoch),
                    str(bind_token),
                    session_id,
                )
                if not ok or owner_ipc is None:
                    await _send_frame(
                        "error",
                        {
                            "error": WebControlErrorCode.NOT_OWNER.value,
                            "message": err or "Failed to bind owner connection",
                        },
                    )
                    continue

                session_ipc = owner_ipc
                bound_operator_id = str(op_id)
                bound_epoch = int(epoch)
                await _send_frame(
                    "ack",
                    {
                        "action": "bind",
                        "success": True,
                        "operator_id": bound_operator_id,
                        "epoch": bound_epoch,
                    },
                )

            # 3. Intent action (motion direction submission)
            elif action == "intent":
                if session_ipc is None or bound_epoch is None:
                    await _send_frame(
                        "error",
                        {
                            "error": WebControlErrorCode.NOT_OWNER.value,
                            "message": "WebSocket not bound to an active operator session",
                        },
                    )
                    continue

                token = payload.get("token")
                epoch = payload.get("epoch")
                sequence = payload.get("sequence")
                raw_dir = payload.get("direction")

                try:
                    direction = MotionDirection(raw_dir)
                except (ValueError, TypeError):
                    await _send_frame(
                        "error",
                        {
                            "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                            "message": f"Invalid direction '{raw_dir}'",
                        },
                    )
                    continue

                # Enforce backpressure: at most 1 unsent intent
                # If queue is full, drop or close congested socket
                try:
                    intent_queue.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.warning(
                        "WebSocket queue congestion: closing socket and invalidating control"
                    )
                    await _send_frame(
                        "error",
                        {
                            "error": WebControlErrorCode.TIMEOUT.value,
                            "message": "Queue congested; closing control connection",
                        },
                    )
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    break

                # Pop and execute intent under ws_lock
                _ = intent_queue.get_nowait()
                async with ws_lock:
                    ok, active_dir, err = await asyncio.to_thread(
                        session_ipc.submit_intent,
                        token,
                        epoch,
                        sequence,
                        direction,
                        payload.get("client_timestamp_ms"),
                        0.2,
                        payload.get("input_generation", -1),
                    )
                if not ok:
                    await _send_frame(
                        "error",
                        {
                            "error": err or WebControlErrorCode.LEASE_EXPIRED.value,
                            "message": f"Intent submission rejected: {err}",
                            "sequence": sequence,
                            "input_generation": session_ipc.last_intent_result.get(
                                "input_generation"
                            ),
                            "operator_state": session_ipc.last_intent_result.get(
                                "operator_state"
                            ),
                        },
                    )
                else:
                    await _send_frame(
                        "ack",
                        {
                            "action": "intent",
                            "success": True,
                            "direction": active_dir,
                            "input_generation": session_ipc.last_intent_result.get(
                                "input_generation"
                            ),
                            "operator_state": session_ipc.last_intent_result.get(
                                "operator_state"
                            ),
                            "sequence": sequence,
                        },
                    )

            # 4. Heartbeat action
            elif action == "heartbeat":
                await _send_frame("ack", {"action": "heartbeat", "success": True})

            else:
                await _send_frame(
                    "error",
                    {
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": f"Unknown action '{action}'",
                    },
                )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.warning("WebSocket handler exception: %s", exc)
    finally:
        running = False
        challenge_task.cancel()
        try:
            await challenge_task
        except asyncio.CancelledError:
            pass

        # FAIL CLOSED: If the owning WebSocket connection drops, immediately release and stop!
        relay.release_owner_binding(session_id)
        if session_ipc is not None:
            logger.info("Closing session IPC; triggering fail-closed disarm")
            try:
                if bound_epoch is not None:
                    await asyncio.to_thread(session_ipc.release, bound_epoch)
            except Exception:
                pass
            finally:
                session_ipc.close()
                relay.close()
