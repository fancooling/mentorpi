"""Camera Unix domain socket IPC server.

Handles incoming status queries and binary-framed MJPEG stream subscriptions
from the web service over /run/ubuntu_tank/camera.sock.
"""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import socket
import struct
import threading
import time
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_CAMERA_SOCKET_PATH,
    ENV_CAMERA_SOCKET_PATH,
    MAX_CAMERA_MESSAGE_BYTES,
)
from ubuntu_tank_protocol.enums import CameraState

from .v4l2_capture import V4L2CameraCapture

logger = logging.getLogger(__name__)


class CameraIpcServer:
    """Unix domain socket server for camera status and streaming."""

    STREAM_MAGIC = b"MJPG"
    HEADER_STRUCT = struct.Struct("!4sI")

    def __init__(
        self,
        capture: V4L2CameraCapture,
        socket_path: str | None = None,
    ) -> None:
        if socket_path is None:
            socket_path = os.environ.get(
                ENV_CAMERA_SOCKET_PATH, DEFAULT_CAMERA_SOCKET_PATH
            )
        self.socket_path = socket_path
        self.capture = capture

        self._server_sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start listening on the Unix domain socket."""
        with self._lock:
            if self._running:
                return

            socket_dir = os.path.dirname(self.socket_path)
            if socket_dir:
                os.makedirs(socket_dir, exist_ok=True)

            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self.socket_path)
            try:
                os.chmod(self.socket_path, 0o700)
            except OSError:
                pass
            sock.listen(16)
            self._server_sock = sock
            self._running = True

            self._thread = threading.Thread(
                target=self._accept_loop, name="CameraIpcAcceptor", daemon=True
            )
            self._thread.start()
            logger.info("Camera IPC server listening on %s", self.socket_path)

    def stop(self) -> None:
        """Stop listening and close all client connections."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            sock = self._server_sock
            self._server_sock = None

        if sock:
            try:
                sock.close()
            except Exception:
                pass

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def get_status_dict(self) -> dict[str, Any]:
        """Build status dictionary from capture state."""
        state = self.capture.state
        profile = {
            "width": self.capture.width,
            "height": self.capture.height,
            "fps": self.capture.fps,
        }
        # Measure available storage under media directory or root
        storage_available = None
        try:
            media_dir = "/var/opt/ubuntu_tank/media"
            target_path = media_dir if os.path.exists(media_dir) else "/"
            usage = shutil.disk_usage(target_path)
            storage_available = usage.free
        except Exception:
            pass

        return {
            "state": state.value if isinstance(state, CameraState) else str(state),
            "frame_age_sec": self.capture.frame_age_sec,
            "profile": profile,
            "recording_state": "disabled",
            "recording_id": None,
            "elapsed_sec": None,
            "storage_available_bytes": storage_available,
            "viewers_count": self.capture.viewers_count,
            "last_error": self.capture.last_error,
        }

    def _accept_loop(self) -> None:
        while self._running:
            try:
                if not self._server_sock:
                    break
                r, _, _ = select.select([self._server_sock], [], [], 0.5)
                if not r:
                    continue
                client_sock, _ = self._server_sock.accept()
                t = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock,),
                    daemon=True,
                )
                t.start()
            except Exception:
                if not self._running:
                    break

    def _handle_client(self, client_sock: socket.socket) -> None:
        try:
            client_sock.settimeout(5.0)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)

            line, _ = buf.split(b"\n", 1)
            req = json.loads(line.decode("utf-8"))
            action = req.get("action", "")

            if action == "status":
                status_dict = self.get_status_dict()
                resp = json.dumps({"success": True, "status": status_dict}) + "\n"
                client_sock.sendall(resp.encode("utf-8"))
                return

            elif action == "stream":
                self._handle_stream(client_sock)
                return

            elif action in ("capture", "record"):
                # Disabled in CAM-2
                resp = (
                    json.dumps(
                        {
                            "success": False,
                            "error": "FEATURE_DISABLED",
                            "detail": f"Action '{action}' is disabled in CAM-2",
                        }
                    )
                    + "\n"
                )
                client_sock.sendall(resp.encode("utf-8"))
                return

            else:
                resp = (
                    json.dumps(
                        {
                            "success": False,
                            "error": "UNKNOWN_ACTION",
                            "detail": f"Unknown action: '{action}'",
                        }
                    )
                    + "\n"
                )
                client_sock.sendall(resp.encode("utf-8"))
                return

        except Exception as exc:
            logger.debug("Camera IPC client handling error: %s", exc)
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def _handle_stream(self, client_sock: socket.socket) -> None:
        """Stream frames continuously to the client socket."""
        self.capture.add_viewer()
        last_ts: float | None = None
        client_sock.settimeout(None)  # Use select for non-blocking send
        try:
            while self._running:
                frame, ts = self.capture.wait_for_new_frame(last_ts, timeout_sec=1.0)
                if not self._running:
                    break
                if frame is None or ts == last_ts:
                    continue
                last_ts = ts

                header = self.HEADER_STRUCT.pack(self.STREAM_MAGIC, len(frame))
                payload = header + frame

                # Check if socket is writable; drop frame if congested (bounded latest-frame queue)
                _, w, x = select.select([], [client_sock], [client_sock], 0.1)
                if x:
                    break
                if not w:
                    # Slow client: drop frame
                    continue

                client_sock.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.capture.remove_viewer()
