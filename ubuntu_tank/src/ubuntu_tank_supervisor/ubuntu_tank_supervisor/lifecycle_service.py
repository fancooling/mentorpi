"""Same-UID lifecycle socket service for fixed Supervisor controller operations.

Start admission is revoked before Stop does any blocking work. A new Start waits
for previous controller-group cleanup and fresh safety progress, and remains
cancellable by Stop while waiting. The runner also checks admission before
launching ROS, closing the delayed-start race. No host
systemd, Docker, deployment locks or arbitrary process names are exposed.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import select
import signal
import socket
import struct
import threading
import time
import uuid
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_LIFECYCLE_SOCKET_PATH,
    MAX_LIFECYCLE_MESSAGE_BYTES,
)
from ubuntu_tank_protocol.deployment import admitted

from . import progress
from .supervisor_api import controller_logs, controller_operation

logger = logging.getLogger(__name__)
TARGET_SERVICE_NAME = "mentorpi-tank.service"  # Retained wire identity.


def extract_peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """Extract Linux peer PID/UID/GID from the connected Unix socket."""
    return struct.unpack(
        "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )


class LifecycleHelperService:
    """Serve the existing lifecycle wire contract using private Supervisor RPC."""

    def __init__(
        self,
        socket_path=DEFAULT_LIFECYCLE_SOCKET_PATH,
        allowed_uids=None,
        process_runner=None,
        log_runner=None,
        preflight_runner=None,
    ):
        self.socket_path = socket_path
        self.allowed_uids = (
            set(allowed_uids) if allowed_uids is not None else {os.getuid()}
        )
        self._custom_process = process_runner
        self._custom_logs = log_runner
        self._custom_preflight = preflight_runner
        self._server_sock = None
        self._running = False
        self._thread = None
        self._lock = threading.RLock()
        self._clients = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="LifecycleWorker"
        )
        self._state_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._generation = 0
        self._stopping = 0

    def _is_uid_authorized(self, uid):
        return uid in self.allowed_uids

    def _operation(self, action):
        return (self._custom_process or controller_operation)(
            [action, TARGET_SERVICE_NAME]
        )

    def _stop_controller(self):
        with self._state_lock:
            self._generation += 1
            self._stopping += 1
            if progress.enabled():
                progress.revoke()
        # Direct signals precede RPC: a hung supervisord cannot delay zero/disarm.
        groups = progress.controller_groups() if progress.enabled() else set()
        for pid in groups:
            progress.signal_group(pid, signal.SIGINT)
        _, _, error = self._operation("stop")
        kill_at = time.monotonic() + progress.KILL_GRACE
        deadline = kill_at + 1.0
        killed = False
        state = "unknown"
        while True:
            if not killed and time.monotonic() >= kill_at:
                for pid in groups:
                    progress.signal_group(pid, signal.SIGKILL)
                killed = True
            _, state, check_error = self._operation("is-active")
            state = state if state in ("active", "inactive", "failed") else "unknown"
            if state == "inactive" or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        for pid in groups:
            progress.signal_group(pid, signal.SIGKILL)
        with self._state_lock:
            self._stopping -= 1
        return {
            "success": state == "inactive",
            "state": state,
            "stopped": state == "inactive",
            "message": "Controller confirmed inactive"
            if state == "inactive"
            else error or check_error or "Controller stop unconfirmed",
        }

    def handle_request(self, req: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        """Validate and execute one fixed lifecycle operation, preserving wire fields."""
        if not self._is_uid_authorized(peer_uid):
            return {
                "success": False,
                "error": "UNAUTHORIZED",
                "message": "Peer UID not authorized",
            }
        if not isinstance(req, dict):
            return {"success": False, "error": "INVALID_PAYLOAD"}
        action = req.get("action")
        try:
            if action == "status":
                code, state, error = self._operation("is-active")
                return {
                    "success": state in ("active", "inactive", "failed"),
                    "state": state,
                    "active": state == "active",
                    "service": TARGET_SERVICE_NAME,
                    "message": error,
                }
            if action == "stop":
                return self._stop_controller()
            if action == "start":
                if not admitted():
                    return {"success": False, "error": "DEPLOYMENT_BUSY"}
                generation = req.get("_generation", self._generation)
                with self._start_lock:
                    if self._custom_preflight:
                        ok, message = self._custom_preflight()
                        if not ok:
                            return {
                                "success": False,
                                "error": "PREFLIGHT_FAILED",
                                "message": "Runtime preflight failed: " + message,
                            }
                    _, initial_state, _ = self._operation("is-active")
                    if progress.enabled() and initial_state == "inactive":
                        # Stop can finish before the monitor reaps the old ROS
                        # group. Do not grant a new permit that its cleanup will
                        # revoke, or reject a healthy monitor during that cleanup.
                        deadline = time.monotonic() + progress.STARTUP_DEADLINE
                        while True:
                            if (
                                not admitted()
                                or generation != self._generation
                                or self._stopping
                            ):
                                return {
                                    "success": False,
                                    "error": "ABORTED_BY_STOP",
                                    "message": "Start superseded by Stop",
                                }
                            if (
                                not progress.read_record("controller")
                                and progress.read_record("monitor").get(
                                    "controller_pid"
                                )
                                == 0
                                and all(
                                    progress.fresh(name)
                                    for name in ("monitor", "operator", "operator_ipc")
                                )
                            ):
                                break
                            if time.monotonic() >= deadline:
                                return {
                                    "success": False,
                                    "error": "PREFLIGHT_FAILED",
                                    "message": "Runtime safety cleanup or progress unavailable",
                                }
                            time.sleep(0.02)
                    with self._state_lock:
                        if (
                            not admitted()
                            or generation != self._generation
                            or self._stopping
                        ):
                            return {
                                "success": False,
                                "error": "ABORTED_BY_STOP",
                                "message": "Start superseded by Stop",
                            }
                        if progress.enabled():
                            if not all(
                                progress.fresh(name)
                                for name in ("monitor", "operator", "operator_ipc")
                            ):
                                return {
                                    "success": False,
                                    "error": "PREFLIGHT_FAILED",
                                    "message": "Runtime safety progress unavailable",
                                }
                            if initial_state != "active":
                                progress.revoke()
                            if not progress.read_record("permit"):
                                progress.write_record(
                                    "permit", {"token": uuid.uuid4().hex}
                                )
                    token = (
                        progress.read_record("permit").get("token")
                        if progress.enabled()
                        else None
                    )
                    code, _, error = self._operation("start")
                    deadline = time.monotonic() + progress.STARTUP_DEADLINE
                    while True:
                        if (
                            not admitted()
                            or generation != self._generation
                            or self._stopping
                        ):
                            self._operation("stop")
                            return {
                                "success": False,
                                "error": "ABORTED_BY_STOP",
                                "message": "Start superseded by Stop",
                            }
                        _, state, _ = self._operation("is-active")
                        record = (
                            progress.read_record("controller")
                            if progress.enabled()
                            else {}
                        )
                        ready = not progress.enabled() or (
                            record.get("ready", False)
                            and record.get("token") == token
                            and progress.fresh("controller")
                        )
                        if (
                            code != 0
                            or (state == "active" and ready)
                            or state == "failed"
                            or time.monotonic() >= deadline
                        ):
                            break
                        time.sleep(0.02)
                    success = code == 0 and state == "active" and ready
                    if not success:
                        self._stop_controller()
                    return {
                        "success": success,
                        "state": state,
                        "started": success,
                        "message": error
                        or (
                            "Controller started disarmed"
                            if success
                            else "Controller startup failed"
                        ),
                    }
            if action == "logs":
                try:
                    limit = max(1, min(200, int(req.get("limit", 50))))
                except (ValueError, TypeError, OverflowError):
                    limit = 50
                lines = (self._custom_logs or controller_logs)(limit)[-limit:]
                # JSON escaping is part of the wire budget, including control characters.
                while lines and len(json.dumps(lines).encode()) > 60000:
                    lines.pop(0)
                return {
                    "success": True,
                    "lines": lines,
                    "total_lines": len(lines),
                    "total_bytes": sum(len(line.encode()) for line in lines),
                }
            return {
                "success": False,
                "error": "INVALID_PAYLOAD",
                "message": "Unknown lifecycle action",
            }
        except Exception as exc:
            logger.exception("Lifecycle operation failed")
            return {"success": False, "error": "OPERATION_FAILED", "message": str(exc)}

    def start(self) -> None:
        """Bind socket and start listener loop."""
        with self._lock:
            if self._running:
                return

            sock_dir = os.path.dirname(os.path.abspath(self.socket_path))
            os.makedirs(sock_dir, mode=0o700, exist_ok=True)

            if os.path.exists(self.socket_path):
                test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    test_sock.connect(self.socket_path)
                    test_sock.close()
                    raise RuntimeError(
                        f"Lifecycle socket '{self.socket_path}' is already in use."
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

            os.chmod(self.socket_path, 0o600)

            self._server_sock.listen(16)
            self._running = True
            self._thread = threading.Thread(
                target=self._serve_loop, name="LifecycleHelperService", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop server and close connections."""
        with self._lock:
            if not self._running:
                return
            self._running = False

            self._executor.shutdown(wait=False, cancel_futures=True)

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

    def _dispatch_client_request(
        self, sock: socket.socket, req: dict[str, Any], uid: int
    ) -> None:
        """Execute request in worker thread and send response back to client."""
        try:
            resp = self.handle_request(req, uid)
            payload = json.dumps(resp).encode("utf-8") + b"\n"
            if len(payload) > MAX_LIFECYCLE_MESSAGE_BYTES:
                payload = b'{"success":false,"error":"OPERATION_FAILED","message":"Response exceeds frame cap"}\n'
            sock.setblocking(True)
            sock.settimeout(5.0)
            sock.sendall(payload)
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Failed to dispatch lifecycle response: %s", exc)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _serve_loop(self) -> None:
        """Non-blocking event loop using select."""
        while self._running:
            try:
                with self._lock:
                    if not self._running or self._server_sock is None:
                        break
                    rlist = [self._server_sock] + list(self._clients.keys())

                progress.beat("lifecycle")
                readable, _, exceptional = select.select(rlist, [], rlist, 0.05)

                for sock in exceptional:
                    self._close_client(sock)

                for sock in readable:
                    if sock is self._server_sock:
                        try:
                            client_sock, _ = self._server_sock.accept()
                            client_sock.setblocking(False)
                            pid, uid, gid = extract_peer_credentials(client_sock)
                            if not self._is_uid_authorized(uid):
                                client_sock.close()
                                continue
                            self._clients[client_sock] = {
                                "buf": b"",
                                "pid": pid,
                                "uid": uid,
                                "gid": gid,
                            }
                        except OSError as exc:
                            logger.debug("Lifecycle accept failed: %s", exc)
                    else:
                        self._handle_client_readable(sock)
            except (OSError, ValueError) as exc:
                if self._running:
                    logger.error("Error in lifecycle serve loop: %s", exc)

    def _handle_client_readable(self, sock: socket.socket) -> None:
        """Read newline-delimited requests from a client socket."""
        try:
            chunk = sock.recv(4096)
            if not chunk:
                self._close_client(sock)
                return
            meta = self._clients.get(sock)
            if meta is None:
                return
            meta["buf"] += chunk
            if len(meta["buf"]) > MAX_LIFECYCLE_MESSAGE_BYTES:
                try:
                    sock.sendall(
                        json.dumps(
                            {
                                "success": False,
                                "error": "INVALID_PAYLOAD",
                                "message": "Message exceeds frame cap",
                            }
                        ).encode("utf-8")
                        + b"\n"
                    )
                except OSError as exc:
                    logger.debug("Lifecycle response failed: %s", exc)
                self._close_client(sock)
                return

            while b"\n" in meta["buf"]:
                line, meta["buf"] = meta["buf"].split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise TypeError("Request must be an object")
                    req["_generation"] = self._generation
                except (ValueError, TypeError) as exc:
                    resp = {
                        "success": False,
                        "error": "INVALID_PAYLOAD",
                        "message": f"Malformed JSON: {exc}",
                    }
                    try:
                        sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                    except OSError as exc:
                        logger.debug("Lifecycle response failed: %s", exc)
                    continue

                # Remove client socket from select tracking before dispatching
                with self._lock:
                    self._clients.pop(sock, None)

                if req.get("action") == "stop":
                    # Priority path for Stop: dedicated immediate thread
                    threading.Thread(
                        target=self._dispatch_client_request,
                        args=(sock, req, meta["uid"]),
                        name="PriorityStopWorker",
                        daemon=True,
                    ).start()
                else:
                    # Non-stop requests dispatched to worker pool
                    self._executor.submit(
                        self._dispatch_client_request, sock, req, meta["uid"]
                    )
                return
        except (OSError, ValueError, RuntimeError):
            self._close_client(sock)

    def _close_client(self, sock: socket.socket) -> None:
        """Close and remove a client connection."""
        try:
            sock.close()
        except OSError:
            pass
        self._clients.pop(sock, None)
