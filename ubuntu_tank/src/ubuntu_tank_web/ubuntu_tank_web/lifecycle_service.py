"""
Restricted Lifecycle Helper Service for MentorPi Tank.

Exposes only fixed start, stop, status, and bounded recent-log operations
for mentorpi-tank.service over a protected Unix domain socket (/run/ubuntu_tank/lifecycle.sock).
Enforces:
1. SO_PEERCRED credential checks (root or members of ubuntu-tank-operators).
2. Fixed command arrays with sanitized environment (PATH=/usr/bin:/bin:/usr/sbin:/sbin),
   never shell text or arbitrary caller arguments.
3. Strict 64 KiB frame cap and JSON framing.
4. Immediate stop execution without waiting for deployment locks or ROS responses.
5. Serialization of starts under the deployment lock and rejection if activation recovery is pending.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import logging
import os
import select
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Callable

from ubuntu_tank_protocol.constants import (
    DEFAULT_LIFECYCLE_SOCKET_PATH,
    MAX_LIFECYCLE_MESSAGE_BYTES,
)

logger = logging.getLogger(__name__)

TARGET_SERVICE_NAME = "mentorpi-tank.service"
RECOVERY_PENDING_FILE = "/var/opt/ubuntu_tank/deployment/recovery_pending"
DEPLOYMENT_LOCK_FILE = "/run/lock/ubuntu_tank/deploy.lock"
ACTIVATION_JOURNAL_FILE = "/var/opt/ubuntu_tank/deployment/activation-journal"


def extract_peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """Extract (pid, uid, gid) from Unix domain socket peer on Linux."""
    try:
        so_peercred = getattr(socket, "SO_PEERCRED", 17)
        cred_bytes = sock.getsockopt(
            socket.SOL_SOCKET, so_peercred, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", cred_bytes)
        return pid, uid, gid
    except Exception as exc:
        raise OSError(f"Failed to extract peer credentials: {exc}") from exc


class LifecycleHelperService:
    """Restricted lifecycle helper daemon exposing fixed mentorpi-tank operations."""

    def __init__(
        self,
        socket_path: str = DEFAULT_LIFECYCLE_SOCKET_PATH,
        allowed_uids: set[int] | None = None,
        systemctl_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
        journalctl_runner: Callable[[int], list[str]] | None = None,
        preflight_runner: Callable[[], tuple[bool, str]] | None = None,
        deployment_lock_file: str = DEPLOYMENT_LOCK_FILE,
        recovery_pending_file: str = RECOVERY_PENDING_FILE,
        journal_file: str = ACTIVATION_JOURNAL_FILE,
    ) -> None:
        self.socket_path = socket_path
        self.allowed_uids = (
            allowed_uids if allowed_uids is not None else {0, os.getuid()}
        )
        self._custom_systemctl = systemctl_runner
        self._custom_journalctl = journalctl_runner
        self._custom_preflight = preflight_runner
        self.deployment_lock_file = deployment_lock_file
        self.recovery_pending_file = recovery_pending_file
        self.journal_file = journal_file

        self._server_sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._clients: dict[socket.socket, dict[str, Any]] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="LifecycleWorker"
        )
        self._last_stop_monotonic: float = 0.0
        self._state_lock = threading.Lock()

    def _is_uid_authorized(self, uid: int) -> bool:
        """Verify peer UID is root, in allowed_uids, or member of ubuntu-tank-operators."""
        if uid == 0:
            return True
        if uid in self.allowed_uids:
            return True
        try:
            import grp
            import pwd

            op_gid = grp.getgrnam("ubuntu-tank-operators").gr_gid
            pw = pwd.getpwuid(uid)
            if pw.pw_gid == op_gid:
                return True
            if op_gid in os.getgrouplist(pw.pw_name, pw.pw_gid):
                return True
        except Exception:
            pass
        return False

    def _run_systemctl(self, args: list[str]) -> tuple[int, str, str]:
        """Execute a fixed systemctl command with clean environment and no shell."""
        if self._custom_systemctl is not None:
            return self._custom_systemctl(args)
        cmd = ["systemctl"] + args
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10.0,
                env=env,
                shell=False,
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except Exception as exc:
            return -1, "", str(exc)

    def _get_journal_logs(self, limit: int) -> list[str]:
        """Retrieve bounded recent journalctl logs for mentorpi-tank.service."""
        if self._custom_journalctl is not None:
            return self._custom_journalctl(limit)
        limit = max(1, min(200, limit))
        cmd = [
            "journalctl",
            "-u",
            TARGET_SERVICE_NAME,
            "-n",
            str(limit),
            "--no-pager",
            "-o",
            "short-iso",
        ]
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
                env=env,
                shell=False,
            )
            if res.returncode == 0:
                return res.stdout.splitlines()
            return [f"journalctl exited with code {res.returncode}: {res.stderr}"]
        except Exception as exc:
            return [f"Failed to query journalctl: {exc}"]

    def handle_request(self, req: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        """Process a validated request dictionary and return response."""
        if not self._is_uid_authorized(peer_uid):
            return {
                "success": False,
                "error": "UNAUTHORIZED",
                "message": f"Peer UID {peer_uid} not authorized for lifecycle helper",
            }

        action = req.get("action")
        if action == "status":
            code, stdout, stderr = self._run_systemctl(
                ["is-active", TARGET_SERVICE_NAME]
            )
            state = stdout.strip() if stdout else ("failed" if code != 0 else "unknown")
            if state not in ("active", "inactive", "failed"):
                state = "unknown"
            return {
                "success": True,
                "state": state,
                "active": (state == "active"),
                "service": TARGET_SERVICE_NAME,
            }

        elif action == "stop":
            # Stop executes immediately without waiting for deployment lock
            with self._state_lock:
                self._last_stop_monotonic = time.monotonic()

            code, stdout, stderr = self._run_systemctl(["stop", TARGET_SERVICE_NAME])

            with self._state_lock:
                self._last_stop_monotonic = time.monotonic()

            # Check state after stop
            chk_code, check_out, chk_err = self._run_systemctl(
                ["is-active", TARGET_SERVICE_NAME]
            )
            state = check_out.strip() if check_out else "unknown"
            if state not in ("active", "inactive", "failed"):
                state = "unknown"

            # Return success and stopped only for an explicitly confirmed "inactive" result.
            # Failed or empty is-active output remains unknown and unconfirmed (success=False, stopped=False).
            is_stopped = state == "inactive"
            return {
                "success": is_stopped,
                "state": state,
                "stopped": is_stopped,
                "message": (
                    "Controller confirmed inactive"
                    if is_stopped
                    else (
                        stderr
                        or chk_err
                        or f"Controller state after stop is {state} (expected inactive)"
                    )
                ),
            }

        elif action == "start":
            start_time = time.monotonic()
            with self._state_lock:
                if self._last_stop_monotonic >= start_time:
                    return {
                        "success": False,
                        "error": "ABORTED_BY_STOP",
                        "message": "Cannot start controller: emergency stop was requested",
                    }

            # 1. Check if activation recovery is pending
            if os.path.exists(self.recovery_pending_file):
                return {
                    "success": False,
                    "error": "OPERATION_FAILED",
                    "message": "Cannot start controller: activation recovery is pending",
                }
            if os.path.exists(self.journal_file):
                try:
                    with open(self.journal_file, "r", encoding="utf-8") as jf:
                        jdata = json.load(jf)
                    if (
                        isinstance(jdata, dict)
                        and jdata.get("current_transaction") is not None
                    ):
                        return {
                            "success": False,
                            "error": "OPERATION_FAILED",
                            "message": "Cannot start controller: activation transaction is uncommitted",
                        }
                except Exception as exc:
                    return {
                        "success": False,
                        "error": "OPERATION_FAILED",
                        "message": f"Cannot start controller: journal check failed: {exc}",
                    }

            # 2. Check deployment lock
            if os.path.exists(self.deployment_lock_file):
                try:
                    fd = os.open(self.deployment_lock_file, os.O_RDONLY)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except (IOError, BlockingIOError, OSError):
                        return {
                            "success": False,
                            "error": "DEPLOYMENT_BUSY",
                            "message": "Cannot start controller: deployment lock is held",
                        }
                    finally:
                        os.close(fd)
                except Exception as exc:
                    logger.debug("Failed to check deployment lock: %s", exc)

            # 3. Preflight check
            if self._custom_preflight is not None:
                ok, pmsg = self._custom_preflight()
                if not ok:
                    return {
                        "success": False,
                        "error": "PREFLIGHT_FAILED",
                        "message": f"Host preflight failed: {pmsg}",
                    }

            # Check race against stop before executing systemctl start
            with self._state_lock:
                if self._last_stop_monotonic >= start_time:
                    return {
                        "success": False,
                        "error": "ABORTED_BY_STOP",
                        "message": "Controller start was superseded by an emergency stop",
                    }

            code, stdout, stderr = self._run_systemctl(["start", TARGET_SERVICE_NAME])

            # Check race against stop after systemctl start: if stop was invoked during start,
            # re-execute stop immediately and fail start to guarantee stopped invariant
            with self._state_lock:
                if self._last_stop_monotonic >= start_time:
                    self._run_systemctl(["stop", TARGET_SERVICE_NAME])
                    return {
                        "success": False,
                        "error": "ABORTED_BY_STOP",
                        "message": "Controller start was superseded by an emergency stop",
                    }

            _, check_out, _ = self._run_systemctl(["is-active", TARGET_SERVICE_NAME])
            state = check_out.strip() if check_out else "unknown"
            return {
                "success": (code == 0 and state == "active"),
                "state": state,
                "started": (state == "active"),
                "message": stderr if code != 0 else "Controller started successfully",
            }

        elif action == "logs":
            raw_limit = req.get("limit", 50)
            try:
                limit = int(raw_limit)
            except (ValueError, TypeError):
                limit = 50
            lines = self._get_journal_logs(limit)
            # Enforce 64 KiB maximum payload limit
            total_bytes = sum(len(line.encode("utf-8")) for line in lines)
            while total_bytes > 60000 and lines:
                removed = lines.pop(0)
                total_bytes -= len(removed.encode("utf-8"))

            return {
                "success": True,
                "lines": lines,
                "total_lines": len(lines),
                "total_bytes": total_bytes,
            }

        else:
            return {
                "success": False,
                "error": "INVALID_PAYLOAD",
                "message": f"Unknown lifecycle action '{action}'",
            }

    def start(self) -> None:
        """Bind socket and start listener loop."""
        with self._lock:
            if self._running:
                return

            sock_dir = os.path.dirname(os.path.abspath(self.socket_path))
            os.makedirs(sock_dir, exist_ok=True)

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

            try:
                import grp

                op_grp = grp.getgrnam("ubuntu-tank-operators")
                os.chown(self.socket_path, -1, op_grp.gr_gid)
            except Exception:
                pass

            try:
                os.chmod(self.socket_path, 0o660)
            except OSError:
                pass

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
            sock.setblocking(True)
            sock.settimeout(5.0)
            sock.sendall(payload)
        except Exception as exc:
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
                                "buf": "",
                                "pid": pid,
                                "uid": uid,
                                "gid": gid,
                            }
                        except Exception:
                            pass
                    else:
                        self._handle_client_readable(sock)
            except Exception as exc:
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
            meta["buf"] += chunk.decode("utf-8", errors="replace")
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
                except Exception:
                    pass
                self._close_client(sock)
                return

            while "\n" in meta["buf"]:
                line, meta["buf"] = meta["buf"].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except Exception as exc:
                    resp = {
                        "success": False,
                        "error": "INVALID_PAYLOAD",
                        "message": f"Malformed JSON: {exc}",
                    }
                    try:
                        sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                    except Exception:
                        pass
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
        except Exception:
            self._close_client(sock)

    def _close_client(self, sock: socket.socket) -> None:
        """Close and remove a client connection."""
        try:
            sock.close()
        except OSError:
            pass
        self._clients.pop(sock, None)
