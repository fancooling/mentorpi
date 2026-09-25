"""
Client for the MentorPi Restricted Lifecycle Helper Service.

Connects to /run/ubuntu_tank/lifecycle.sock to issue fixed controller operations
(status, start, stop, bounded logs) from the unprivileged web service.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_LIFECYCLE_SOCKET_PATH,
    MAX_LIFECYCLE_MESSAGE_BYTES,
)


class LifecycleClient:
    """Client for communicating with LifecycleHelperService over Unix socket."""

    def __init__(self, socket_path: str = DEFAULT_LIFECYCLE_SOCKET_PATH) -> None:
        self.socket_path = socket_path

    def _send_request(
        self, req: dict[str, Any], timeout_sec: float = 5.0
    ) -> dict[str, Any]:
        """Send a single request to the lifecycle helper socket and return response."""
        if not os.path.exists(self.socket_path):
            return {
                "success": False,
                "error": "CONTROLLER_UNAVAILABLE",
                "message": f"Lifecycle helper socket '{self.socket_path}' is not available",
            }

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect(self.socket_path)
            payload = json.dumps(req).encode("utf-8") + b"\n"
            if len(payload) > MAX_LIFECYCLE_MESSAGE_BYTES:
                raise ValueError("Request exceeds max message size")
            sock.sendall(payload)

            buf = ""
            start_time = time.monotonic()
            while "\n" not in buf:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout_sec:
                    raise TimeoutError(
                        f"Lifecycle request timed out after {timeout_sec}s"
                    )
                sock.settimeout(max(0.1, timeout_sec - elapsed))
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionResetError("Lifecycle helper closed connection")
                buf += chunk.decode("utf-8", errors="replace")

            line, _ = buf.split("\n", 1)
            return json.loads(line.strip())
        except Exception as exc:
            return {
                "success": False,
                "error": "OPERATION_FAILED",
                "message": f"Lifecycle request failed: {exc}",
            }
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def get_status(self, timeout_sec: float = 3.0) -> tuple[bool, str, str | None]:
        """Query mentorpi-tank.service active status."""
        resp = self._send_request({"action": "status"}, timeout_sec=timeout_sec)
        success = bool(resp.get("success", False))
        state = resp.get("state", "unknown")
        message = resp.get("message")
        return success, state, message

    def stop_controller(
        self, timeout_sec: float = 10.0
    ) -> tuple[bool, str, str | None]:
        """Stop mentorpi-tank.service immediately."""
        resp = self._send_request({"action": "stop"}, timeout_sec=timeout_sec)
        success = bool(resp.get("success", False))
        state = resp.get("state", "unknown")
        message = resp.get("message")
        return success, state, message

    def start_controller(
        self, timeout_sec: float = 15.0
    ) -> tuple[bool, str, str | None]:
        """Start mentorpi-tank.service with preflight and deployment lock verification."""
        resp = self._send_request({"action": "start"}, timeout_sec=timeout_sec)
        success = bool(resp.get("success", False))
        state = resp.get("state", "unknown")
        message = resp.get("message")
        return success, state, message

    def get_logs(
        self, limit: int = 50, timeout_sec: float = 5.0
    ) -> tuple[bool, list[str], int, int, str | None]:
        """Fetch bounded recent logs for mentorpi-tank.service."""
        resp = self._send_request(
            {"action": "logs", "limit": limit}, timeout_sec=timeout_sec
        )
        success = bool(resp.get("success", False))
        lines = resp.get("lines", [])
        total_lines = resp.get("total_lines", len(lines))
        total_bytes = resp.get("total_bytes", sum(len(line) for line in lines))
        message = resp.get("message")
        return success, lines, total_lines, total_bytes, message
