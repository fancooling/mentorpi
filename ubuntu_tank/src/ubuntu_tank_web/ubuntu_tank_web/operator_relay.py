"""
Operator Agent IPC Relay for MentorPi Tank Web Service.

Translates web API actions into authenticated Unix socket requests to the
Operator Agent daemon (/run/ubuntu_tank/operator.sock).
Manages dedicated per-session IPC sockets for active WebSocket controllers to
guarantee fail-closed disarm and emergency stop upon browser disconnect or network drop.
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Any

from ubuntu_tank_operator.constants import DEFAULT_OPERATOR_SOCKET_PATH
from ubuntu_tank_operator.enums import WebControlErrorCode
from ubuntu_tank_operator.ipc_client import OperatorIpcClient

logger = logging.getLogger(__name__)


class OperatorRelay:
    """Relay between web request handlers and the operator agent daemon."""

    def __init__(self, socket_path: str = DEFAULT_OPERATOR_SOCKET_PATH) -> None:
        self.socket_path = socket_path
        self._owner_client: OperatorIpcClient | None = None
        self._active_operator_id: str | None = None
        self._active_epoch: int | None = None
        self._active_bind_token: str | None = None
        self._bound_session_id: str | None = None
        self._lock = threading.Lock()

    def _clear_owner_state_locked(self) -> None:
        """Clear active owner connection and binding state while holding self._lock."""
        self._owner_client = None
        self._active_operator_id = None
        self._active_epoch = None
        self._active_bind_token = None
        self._bound_session_id = None

    def create_client(self) -> OperatorIpcClient:
        """Create a new dedicated IPC client instance."""
        return OperatorIpcClient(socket_path=self.socket_path)

    def get_owner_client(self) -> OperatorIpcClient | None:
        """Return the active owner client if connected."""
        with self._lock:
            return self._owner_client

    @property
    def active_epoch(self) -> int | None:
        """Return the active epoch for the owner session."""
        with self._lock:
            return self._active_epoch

    def claim_owner_binding(
        self,
        operator_id: str,
        epoch: int,
        bind_token: str,
        session_id: str,
    ) -> tuple[bool, OperatorIpcClient | None, str | None]:
        """
        Exclusively bind the active owner client to a validated WebSocket session.

        Requires matching operator_id, epoch, and single-use bind_token.
        Rejects any subsequent, duplicate, or mismatched binds until released.
        """
        with self._lock:
            if self._owner_client is None:
                return (
                    False,
                    None,
                    "No active control connection holding ownership",
                )
            if self._active_operator_id != operator_id:
                return False, None, "Operator identity mismatch"
            if self._active_epoch != epoch:
                return False, None, "Epoch mismatch"
            if not self._active_bind_token or not secrets.compare_digest(
                self._active_bind_token, bind_token
            ):
                return False, None, "Invalid or expired bind token"
            if self._bound_session_id is not None:
                return (
                    False,
                    None,
                    "Owner connection already bound to an active WebSocket session",
                )

            self._bound_session_id = session_id
            # Consume bind_token so it cannot be reused
            self._active_bind_token = None
            return True, self._owner_client, None

    def release_owner_binding(self, session_id: str) -> None:
        """Release binding if held by the given session."""
        with self._lock:
            if self._bound_session_id == session_id:
                self._bound_session_id = None

    def get_status(self, timeout_sec: float = 2.0) -> dict[str, Any]:
        """Query status from operator agent, returning fallback if agent is offline."""
        client = self.create_client()
        try:
            client.connect(timeout_sec=timeout_sec)
            return client.get_status(timeout_sec=timeout_sec)
        except Exception as exc:
            logger.debug("Failed to query operator agent status: %s", exc)
            return {
                "service_state": "unknown",
                "operator_state": "FAULT",
                "active_owner": None,
                "current_epoch": None,
                "guard_armed": False,
                "disarm_pending": False,
                "battery_voltage": None,
                "linear_speed": 0.0,
                "angular_speed": 0.0,
                "limits": {},
                "freshness": {},
                "last_fault": f"Operator agent unavailable: {exc}",
                "release_id": "unknown",
                "protocol_version": "1.0.0",
            }
        finally:
            client.close()

    def get_version(self, timeout_sec: float = 2.0) -> dict[str, Any]:
        """Query protocol version information from operator agent."""
        client = self.create_client()
        try:
            client.connect(timeout_sec=timeout_sec)
            return client.get_version(timeout_sec=timeout_sec)
        except Exception as exc:
            return {
                "protocol_version": "1.0.0",
                "api_version": "v1",
                "schema_version": 1,
                "release_id": "unknown",
                "supported_protocols": ["1.0.0"],
                "error": f"Operator agent unavailable: {exc}",
            }
        finally:
            client.close()

    def stop(
        self,
        request_id: str | None = None,
        epoch: int | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, str | None]:
        """
        Issue immediate stop request to operator agent without revoking ownership.

        Independent of ownership: any connected client or stop request can stop the tank.
        """
        with self._lock:
            owner_client = self._owner_client

        if owner_client is not None:
            try:
                return owner_client.stop(
                    request_id=request_id, epoch=epoch, timeout_sec=timeout_sec
                )
            except Exception as exc:
                logger.warning(
                    "Stop request on owner client failed, clearing broken owner and falling back: %s",
                    exc,
                )
                with self._lock:
                    if self._owner_client is owner_client:
                        try:
                            self._owner_client.close()
                        except Exception:
                            pass
                        self._clear_owner_state_locked()

        fallback_client = self.create_client()
        try:
            fallback_client.connect(timeout_sec=timeout_sec)
            return fallback_client.stop(
                request_id=request_id, epoch=epoch, timeout_sec=timeout_sec
            )
        except Exception as exc:
            logger.warning("Stop request to operator agent failed: %s", exc)
            return False, str(exc)
        finally:
            fallback_client.close()

    def acquire(
        self,
        operator_id: str,
        request_id: str | None = None,
        max_linear_speed: float | None = None,
        max_angular_speed: float | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, int | None, str | None, str | None]:
        """Acquire operator control authority and hold the connection open."""
        with self._lock:
            if self._owner_client is not None:
                return (
                    False,
                    None,
                    WebControlErrorCode.DEPLOYMENT_BUSY.value,
                    "Operator authority already held by an active session",
                )
            client = self.create_client()
            try:
                client.connect(timeout_sec=timeout_sec)
                ok, epoch, err, msg = client.acquire(
                    operator_id=operator_id,
                    request_id=request_id,
                    max_linear_speed=max_linear_speed,
                    max_angular_speed=max_angular_speed,
                    timeout_sec=timeout_sec,
                )
                if ok:
                    bind_token = secrets.token_urlsafe(24)
                    self._owner_client = client
                    self._active_operator_id = operator_id
                    self._active_epoch = epoch
                    self._active_bind_token = bind_token
                    self._bound_session_id = None
                    return True, epoch, bind_token, msg
                else:
                    client.close()
                    return False, None, err, msg
            except Exception as exc:
                client.close()
                return (
                    False,
                    None,
                    WebControlErrorCode.CONTROLLER_UNAVAILABLE.value,
                    str(exc),
                )

    def release(
        self,
        epoch: int,
        request_id: str | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, str | None, str | None]:
        """Relinquish operator authority and close the owner connection."""
        with self._lock:
            client = self._owner_client
            self._clear_owner_state_locked()

        if client is None:
            return (
                False,
                WebControlErrorCode.NOT_OWNER.value,
                "No active control connection holding ownership",
            )

        try:
            return client.release(
                epoch=epoch, request_id=request_id, timeout_sec=timeout_sec
            )
        except Exception as exc:
            return False, WebControlErrorCode.CONTROLLER_UNAVAILABLE.value, str(exc)
        finally:
            client.close()

    def arm(
        self,
        epoch: int,
        tracks_raised: bool = True,
        request_id: str | None = None,
        timeout_sec: float = 5.0,
    ) -> tuple[bool, str | None, str | None]:
        """Arm the chassis controller using the active owner connection."""
        with self._lock:
            client = self._owner_client
            if client is None:
                return (
                    False,
                    WebControlErrorCode.NOT_OWNER.value,
                    "No active control connection holding ownership",
                )
            try:
                ok, err, msg = client.arm(
                    epoch=epoch,
                    tracks_raised=tracks_raised,
                    request_id=request_id,
                    timeout_sec=timeout_sec,
                )
                if ok:
                    self._active_epoch = epoch
                return ok, err, msg
            except Exception as exc:
                return False, WebControlErrorCode.CONTROLLER_UNAVAILABLE.value, str(exc)

    def close(self) -> None:
        """Close active owner connection if held."""
        with self._lock:
            if self._owner_client is not None:
                try:
                    self._owner_client.close()
                except Exception:
                    pass
            self._clear_owner_state_locked()
