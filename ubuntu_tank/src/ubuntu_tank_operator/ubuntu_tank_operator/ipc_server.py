"""
Operator IPC Server for MentorPi Pi 5 Web Control.

Provides a secure, bounded Unix domain socket server implementing:
1. Peer credential verification via SO_PEERCRED (PID, UID, GID).
2. Exclusive single-operator authority arbitration.
3. Fail-closed disconnect handling: if the active operator disconnects,
   immediately triggers an emergency stop and disarm.
4. Stop priority: any connected client may request an immediate stop/disarm.
5. Non-blocking socket handling with newline-delimited JSON framing and a 64 KiB frame cap.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from ubuntu_tank_protocol.constants import (
    DEFAULT_OPERATOR_SOCKET_PATH,
    ENV_OPERATOR_SOCKET_PATH,
    MAX_IPC_MESSAGE_BYTES,
)
from ubuntu_tank_protocol.enums import OperatorState, WebControlErrorCode
from ubuntu_tank_protocol.schemas import (
    ChallengeResponse,
    ControlArmRequest,
    ControlStopResponse,
    VersionResponse,
)
from ubuntu_tank_supervisor import progress

if TYPE_CHECKING:
    from .state_machine import OperatorStateMachine

logger = logging.getLogger(__name__)


def extract_peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """Extract (pid, uid, gid) from a Unix domain socket peer on Linux."""
    try:
        so_peercred = getattr(socket, "SO_PEERCRED", 17)
        cred_bytes = sock.getsockopt(
            socket.SOL_SOCKET, so_peercred, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", cred_bytes)
        return pid, uid, gid
    except Exception as exc:
        raise PermissionError(f"Failed to read peer credentials: {exc}") from exc


class OperatorIpcServer:
    """Unix domain socket IPC server managing operator arbitration and commands."""

    def __init__(
        self,
        state_machine: OperatorStateMachine,
        socket_path: str | None = None,
        allowed_uids: list[int] | None = None,
        arm_callback: Callable[[int, str], tuple[bool, str | None, str | None]]
        | None = None,
        stop_callback: Callable[[], None] | None = None,
        observations_callback: Callable[[int | None], list[dict[str, Any]]]
        | None = None,
        reset_observations_callback: Callable[[], int] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        if socket_path is None:
            socket_path = os.environ.get(
                ENV_OPERATOR_SOCKET_PATH, DEFAULT_OPERATOR_SOCKET_PATH
            )
        self.socket_path = socket_path
        self.state_machine = state_machine
        self._explicit_allowed_uids = allowed_uids is not None
        self.allowed_uids = (
            set(allowed_uids) if allowed_uids is not None else {os.getuid(), 0}
        )
        self.arm_callback = arm_callback
        self.stop_callback = stop_callback
        self.observations_callback = observations_callback
        self.reset_observations_callback = reset_observations_callback

        self._server_sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = (
            lock
            if lock is not None
            else getattr(state_machine, "_lock", threading.RLock())
        )

        # Connection management: socket -> {"buf": str, "pid": int, "uid": int, "gid": int}
        self._clients: dict[socket.socket, dict[str, Any]] = {}
        self._owner_conn: socket.socket | None = None
        self._owner_id: str | None = None
        self._owner_pid: int | None = None

    def start(self) -> None:
        """Bind socket, set permissions, and start background listening thread."""
        with self._lock:
            if self._running:
                return

            sock_dir = os.path.dirname(os.path.abspath(self.socket_path))
            os.makedirs(sock_dir, exist_ok=True)

            # Check if socket file exists; remove if stale
            if os.path.exists(self.socket_path):
                test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    test_sock.connect(self.socket_path)
                    test_sock.close()
                    raise RuntimeError(
                        f"Operator IPC socket '{self.socket_path}' is already in use by an active server."
                    )
                except (ConnectionRefusedError, FileNotFoundError):
                    try:
                        os.unlink(self.socket_path)
                    except OSError:
                        pass
                finally:
                    test_sock.close()

            self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server_sock.setblocking(False)
            self._server_sock.bind(self.socket_path)
            try:
                import grp

                op_grp = grp.getgrnam("ubuntu-tank-operators")
                os.chown(self.socket_path, -1, op_grp.gr_gid)
            except Exception:
                pass
            try:
                os.chmod(self.socket_path, 0o600 if progress.enabled() else 0o660)
            except OSError:
                pass
            self._server_sock.listen(16)

            self._running = True
            self._thread = threading.Thread(
                target=self._serve_loop, name="OperatorIpcServer", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop server, close all client connections, and clean up socket file."""
        with self._lock:
            if not self._running:
                return
            self._running = False

            # Trigger emergency stop if actively owning
            if self._owner_conn is not None:
                self._handle_owner_lost("Server shutdown")

            for sock in list(self._clients.keys()):
                try:
                    sock.close()
                except OSError:
                    pass
            self._clients.clear()

            if self._server_sock is not None:
                try:
                    self._server_sock.close()
                except OSError:
                    pass
                self._server_sock = None

            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

    def _serve_loop(self) -> None:
        """Main non-blocking event loop using select."""
        while self._running:
            try:
                with self._lock:
                    if not self._running or self._server_sock is None:
                        break
                    rlist = [self._server_sock] + list(self._clients.keys())

                progress.beat("operator_ipc")
                readable, _, exceptional = select.select(rlist, [], rlist, 0.05)

                for sock in exceptional:
                    self._close_client(sock, reason="Socket exception")

                for sock in readable:
                    if sock is self._server_sock:
                        self._accept_connection()
                    else:
                        self._read_client(sock)

            except (select.error, OSError, ValueError) as exc:
                if not self._running:
                    break
                logger.warning("Error in IPC server select loop: %s", exc)

    def _is_uid_authorized(self, uid: int) -> bool:
        """Check if peer UID is authorized.

        If allowed_uids was explicitly configured, check uid in allowed_uids.
        Otherwise, authorize root (0), daemon self, or members of ubuntu-tank-operators.
        """
        if self._explicit_allowed_uids:
            return uid in self.allowed_uids

        if uid == 0 or uid == os.getuid():
            return True
        try:
            import grp
            import pwd

            pw = pwd.getpwuid(uid)
            target_grp = grp.getgrnam("ubuntu-tank-operators")
            if pw.pw_gid == target_grp.gr_gid:
                return True
            if pw.pw_name in target_grp.gr_mem:
                return True
            if hasattr(os, "getgrouplist"):
                user_gids = os.getgrouplist(pw.pw_name, pw.pw_gid)
                if target_grp.gr_gid in user_gids:
                    return True
        except (KeyError, OSError, Exception):
            pass
        return False

    def _accept_connection(self) -> None:
        """Accept incoming client connection and authenticate peer credentials."""
        try:
            conn, _ = self._server_sock.accept()
            conn.setblocking(False)
        except (BlockingIOError, OSError):
            return

        try:
            pid, uid, gid = extract_peer_credentials(conn)
        except Exception as exc:
            logger.warning(
                "Rejecting connection without valid peer credentials: %s", exc
            )
            conn.close()
            return

        if not self._is_uid_authorized(uid):
            logger.warning(
                "Rejecting connection from unauthorized UID %d (PID %d). Allowed UIDs: %s",
                uid,
                pid,
                self.allowed_uids,
            )
            try:
                err_resp = {
                    "success": False,
                    "error": WebControlErrorCode.UNAUTHORIZED.value,
                    "message": f"Peer UID {uid} not authorized",
                }
                conn.sendall(json.dumps(err_resp).encode("utf-8") + b"\n")
            except OSError:
                pass
            conn.close()
            return

        with self._lock:
            self._clients[conn] = {"buf": "", "pid": pid, "uid": uid, "gid": gid}

    def _close_client(self, sock: socket.socket, reason: str = "") -> None:
        """Close client socket and trigger emergency disarm if it was the active owner."""
        with self._lock:
            self._clients.pop(sock, None)
            is_owner = sock == self._owner_conn
            if is_owner:
                self._handle_owner_lost(f"Client disconnected ({reason})")

            try:
                sock.close()
            except OSError:
                pass

    def _handle_owner_lost(self, reason: str) -> None:
        """Safely disarm robot and invalidate ownership when the active owner disconnects."""
        logger.warning(
            "Active operator connection lost: %s. Revoking ownership and stopping.",
            reason,
        )
        self._owner_conn = None
        self._owner_id = None
        self._owner_pid = None

        if self.stop_callback is not None:
            try:
                self.stop_callback()
            except Exception as exc:
                logger.error(
                    "Error invoking stop_callback during owner disconnect: %s", exc
                )

        try:
            now_ns = time.monotonic_ns()
            self.state_machine.stop(now_ns)
            self.state_machine.release(
                self.state_machine.owner_id or "", self.state_machine.epoch, now_ns
            )
        except Exception as exc:
            logger.error(
                "Error transitioning state machine during owner disconnect: %s", exc
            )

    def _read_client(self, sock: socket.socket) -> None:
        """Read data from client socket and dispatch complete newline-delimited frames."""
        try:
            data = sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_client(sock, reason="Read error")
            return

        if not data:
            self._close_client(sock, reason="EOF")
            return

        lines: list[str] = []
        with self._lock:
            client = self._clients.get(sock)
            if client is None:
                return

            client["buf"] += data.decode("utf-8", errors="replace")

            if len(client["buf"]) > MAX_IPC_MESSAGE_BYTES:
                err_resp = {
                    "success": False,
                    "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                    "message": f"Frame exceeds maximum limit of {MAX_IPC_MESSAGE_BYTES} bytes",
                }
                self._send_frame(sock, err_resp)
                self._close_client(sock, reason="Oversized frame")
                return

            while "\n" in client["buf"]:
                line, client["buf"] = client["buf"].split("\n", 1)
                line = line.strip()
                if line:
                    lines.append(line)

        for line in lines:
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("Payload must be a JSON object")
            except Exception as exc:
                err_resp = {
                    "success": False,
                    "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                    "message": f"Malformed JSON request: {exc}",
                }
                self._send_frame(sock, err_resp)
                continue

            try:
                response = self._dispatch_request(sock, req, client)
            except Exception as exc:
                logger.error("Internal error handling IPC request %s: %s", req, exc)
                response = {
                    "success": False,
                    "error": WebControlErrorCode.OPERATION_FAILED.value,
                    "message": f"Internal server error: {exc}",
                }
            self._send_frame(sock, response)

    def _send_frame(self, sock: socket.socket, payload: dict[str, Any]) -> None:
        """Serialize and send newline-delimited JSON frame."""
        try:
            encoded = json.dumps(payload).encode("utf-8") + b"\n"
            sock.sendall(encoded)
        except OSError:
            self._close_client(sock, reason="Write error")

    def _dispatch_request(
        self, sock: socket.socket, req: dict[str, Any], client: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch validated IPC request to appropriate handler."""
        action = req.get("action")

        if action == "status":
            with self._lock:
                now_ns = time.monotonic_ns()
                return self.state_machine.get_status(now_ns).to_dict()

        if action == "version":
            with self._lock:
                return VersionResponse(
                    release_id=self.state_machine.release_id
                ).to_dict()

        # Stop has absolute priority and can be called by ANY client at ANY time
        if action == "stop":
            if self.stop_callback is not None:
                try:
                    self.stop_callback()
                except Exception as exc:
                    logger.error("Error in stop_callback: %s", exc)
            with self._lock:
                now_ns = time.monotonic_ns()
                self.state_machine.stop(now_ns)
            return ControlStopResponse(success=True, disarmed=True).to_dict()

        if action == "acquire":
            op_id = req.get("operator_id")
            if not isinstance(op_id, str) or not op_id:
                return {
                    "success": False,
                    "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                    "message": "operator_id must be non-empty string",
                }
            max_linear = req.get("max_linear_speed")
            max_angular = req.get("max_angular_speed")
            if max_linear is not None:
                try:
                    max_linear = float(max_linear)
                except (ValueError, TypeError):
                    return {
                        "success": False,
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": "max_linear_speed must be a valid float",
                    }
            if max_angular is not None:
                try:
                    max_angular = float(max_angular)
                except (ValueError, TypeError):
                    return {
                        "success": False,
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": "max_angular_speed must be a valid float",
                    }

            with self._lock:
                if self._owner_conn is not None and self._owner_conn is not sock:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.DEPLOYMENT_BUSY.value,
                        "message": f"Operator authority already held by '{self._owner_id}' (PID {self._owner_pid})",
                    }

                now_ns = time.monotonic_ns()
                ok, epoch, err, msg = self.state_machine.acquire(
                    op_id,
                    now_ns,
                    max_linear_speed=max_linear,
                    max_angular_speed=max_angular,
                )
                if ok:
                    self._owner_conn = sock
                    self._owner_id = op_id
                    self._owner_pid = client.get("pid")
                    return {
                        "success": True,
                        "epoch": epoch,
                        "error": None,
                        "message": "Acquired ownership",
                    }
                return {
                    "success": False,
                    "epoch": None,
                    "error": err.value
                    if err
                    else WebControlErrorCode.OPERATION_FAILED.value,
                    "message": msg,
                }

        if action == "release":
            epoch = req.get("epoch")
            if not isinstance(epoch, int) or isinstance(epoch, bool):
                return {
                    "success": False,
                    "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                    "message": "epoch must be integer",
                }
            with self._lock:
                if self._owner_conn is not sock:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.NOT_OWNER.value,
                        "message": "Caller does not hold operator ownership",
                    }

            if self.stop_callback is not None:
                try:
                    self.stop_callback()
                except Exception as exc:
                    logger.error("Error in stop_callback during release: %s", exc)

            with self._lock:
                now_ns = time.monotonic_ns()
                ok, err, msg = self.state_machine.release(
                    self._owner_id or "", epoch, now_ns
                )
                if ok:
                    self._owner_conn = None
                    self._owner_id = None
                    self._owner_pid = None
                return {
                    "success": ok,
                    "error": err.value if err else None,
                    "message": msg,
                }

        if action == "arm":
            with self._lock:
                if self._owner_conn is not sock:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.NOT_OWNER.value,
                        "message": "Caller does not hold operator ownership",
                    }

                try:
                    arm_data = {k: v for k, v in req.items() if k != "action"}
                    arm_req = ControlArmRequest.from_dict(arm_data)
                except Exception as exc:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                        "message": str(exc),
                    }

                # Transition state machine into ARMING (validates preflight, tracks_raised, epoch)
                now_ns = time.monotonic_ns()
                ok, err, msg = self.state_machine.arm(
                    self._owner_id or "",
                    arm_req.epoch,
                    arm_req.tracks_raised,
                    now_ns,
                    arm_req.request_id,
                )
                if not ok:
                    return {
                        "success": False,
                        "error": err.value
                        if err
                        else WebControlErrorCode.OPERATION_FAILED.value,
                        "message": msg,
                    }

                arm_epoch = arm_req.epoch
                arm_req_id = arm_req.request_id

            # If arm_callback provided, invoke agent hardware/ROS arming OUTSIDE self._lock
            if self.arm_callback is not None:
                cb_ok, err_code, cb_msg = self.arm_callback(arm_epoch, arm_req_id)
                return {
                    "success": cb_ok,
                    "error": err_code,
                    "message": cb_msg,
                }
            else:
                with self._lock:
                    ok, err, msg = self.state_machine.confirm_armed(
                        guard_confirmed=True,
                        downstream_zero_confirmed=True,
                        current_monotonic_ns=time.monotonic_ns(),
                        epoch=arm_epoch,
                        request_id=arm_req_id,
                    )
                    return {
                        "success": ok,
                        "error": err.value if err else None,
                        "message": "Armed successfully" if ok else msg,
                    }

        if action == "disarm":
            if self.stop_callback is not None:
                try:
                    self.stop_callback()
                except Exception as exc:
                    logger.error("Error in stop_callback during disarm: %s", exc)
            with self._lock:
                now_ns = time.monotonic_ns()
                self.state_machine.stop(now_ns)
            return {"success": True, "disarmed": True}

        if action == "challenge":
            with self._lock:
                if self._owner_conn is not sock:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.NOT_OWNER.value,
                        "message": "Caller does not hold operator ownership",
                    }
                epoch = req.get("epoch")
                if epoch != self.state_machine.epoch:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.INVALID_EPOCH.value,
                        "message": f"Epoch mismatch (expected {self.state_machine.epoch}, got {epoch})",
                    }
                now_ns = time.monotonic_ns()
                challenge = self.state_machine.issue_challenge(now_ns)
                if challenge is None:
                    err_code = (
                        WebControlErrorCode.INVALID_STATE.value
                        if self.state_machine.state
                        not in (OperatorState.ARMED_IDLE, OperatorState.DRIVING)
                        else WebControlErrorCode.LEASE_EXPIRED.value
                    )
                    return {
                        "success": False,
                        "error": err_code,
                        "message": f"Cannot issue challenge in state '{self.state_machine.state.value}'",
                    }
                return {
                    "success": True,
                    "token": challenge.token,
                    "epoch": challenge.epoch,
                    "deadline_monotonic_ns": challenge.deadline_monotonic_ns,
                    "issued_monotonic_ns": challenge.issued_monotonic_ns,
                }

        if action == "intent":
            try:
                resp_payload = req.get("response")
                if not isinstance(resp_payload, dict):
                    resp_payload = {k: v for k, v in req.items() if k != "action"}
                resp = ChallengeResponse.from_dict(resp_payload)
            except Exception as exc:
                return {
                    "success": False,
                    "error": WebControlErrorCode.INVALID_PAYLOAD.value,
                    "message": str(exc),
                }

            with self._lock:
                if self._owner_conn is not sock:
                    return {
                        "success": False,
                        "error": WebControlErrorCode.NOT_OWNER.value,
                        "message": "Caller does not hold operator ownership",
                    }

                now_ns = time.monotonic_ns()
                ok, err, msg = self.state_machine.process_challenge_response(
                    self._owner_id or "", resp, now_ns
                )
                return {
                    "success": ok,
                    "direction": self.state_machine.active_direction.value,
                    "error": err.value if err else None,
                    "message": msg,
                }

        if action == "motion_burst":
            return {
                "success": False,
                "error": WebControlErrorCode.OPERATION_FAILED.value,
                "message": (
                    "Direct motion_burst action is deprecated; execute bursts via "
                    "deadline-checked challenge-intent renewals."
                ),
            }

        if action == "get_observations":
            if self.observations_callback is not None:
                since = req.get("since_mono_ns")
                obs = self.observations_callback(since)
                return {"success": True, "observations": obs}
            return {"success": True, "observations": []}

        if action == "reset_observations":
            if self.reset_observations_callback is not None:
                reset_time = self.reset_observations_callback()
                return {"success": True, "reset_monotonic_ns": reset_time}
            return {
                "success": True,
                "reset_monotonic_ns": time.monotonic_ns(),
            }

        return {
            "success": False,
            "error": WebControlErrorCode.INVALID_PAYLOAD.value,
            "message": f"Unknown action: '{action}'",
        }
