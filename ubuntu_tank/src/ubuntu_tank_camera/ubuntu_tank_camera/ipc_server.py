"""Camera Unix domain socket IPC server.

Handles incoming status queries and binary-framed MJPEG stream subscriptions
from the web service over /run/ubuntu_tank/camera.sock.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
import threading
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_CAMERA_CAPTURE_TIMEOUT_SEC,
    DEFAULT_CAMERA_SOCKET_PATH,
    ENV_CAMERA_SOCKET_PATH,
    MAX_CAMERA_MESSAGE_BYTES,
)
from ubuntu_tank_protocol.enums import CameraState

from .media_storage import MediaManager, StorageError
from .v4l2_capture import V4L2CameraCapture
from .video_recorder import VideoRecorder

logger = logging.getLogger(__name__)


class CameraIpcServer:
    """Unix domain socket server for camera status, streaming, and media storage."""

    STREAM_MAGIC = b"MJPG"
    HEADER_STRUCT = struct.Struct("!4sI")

    def __init__(
        self,
        capture: V4L2CameraCapture,
        socket_path: str | None = None,
        media_manager: MediaManager | None = None,
        media_storage_dir: str | None = None,
    ) -> None:
        if socket_path is None:
            socket_path = os.environ.get(
                ENV_CAMERA_SOCKET_PATH, DEFAULT_CAMERA_SOCKET_PATH
            )
        self.socket_path = socket_path
        self.capture = capture
        if media_manager is not None:
            self.media_manager = media_manager
        elif media_storage_dir is not None:
            self.media_manager = MediaManager(media_dir=media_storage_dir)
        else:
            self.media_manager = MediaManager()

        self.recorder = VideoRecorder(self.capture, self.media_manager)

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

        if self.recorder:
            self.recorder.shutdown()

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
        storage_available = self.media_manager.get_available_storage_bytes()

        return {
            "state": state.value if isinstance(state, CameraState) else str(state),
            "frame_age_sec": self.capture.frame_age_sec,
            "profile": profile,
            "recording_state": self.recorder.state,
            "recording_id": self.recorder.recording_id,
            "elapsed_sec": self.recorder.elapsed_sec,
            "storage_available_bytes": storage_available,
            "viewers_count": self.capture.viewers_count,
            "last_error": self.recorder.last_error or self.capture.last_error,
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
                if len(buf) > MAX_CAMERA_MESSAGE_BYTES:
                    return

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

            elif action == "capture":
                request_id = req.get("request_id") or req.get("idempotency_key")
                if request_id and self.media_manager.has_idempotent(request_id):
                    cached = self.media_manager.get_idempotent(request_id)
                    if cached:
                        resp = json.dumps({"success": True, "media": cached}) + "\n"
                        client_sock.sendall(resp.encode("utf-8"))
                        return

                if self.capture.state in (
                    CameraState.UNAVAILABLE,
                    CameraState.ERROR,
                ):
                    resp = (
                        json.dumps(
                            {
                                "success": False,
                                "error": "CAMERA_UNAVAILABLE",
                                "detail": f"Camera is unavailable: {self.capture.last_error or 'device not ready'}",
                            }
                        )
                        + "\n"
                    )
                    client_sock.sendall(resp.encode("utf-8"))
                    return

                self.capture.add_viewer()
                try:
                    _, last_ts = self.capture.get_latest_frame()
                    frame, new_ts = self.capture.wait_for_new_frame(
                        last_ts, timeout_sec=DEFAULT_CAMERA_CAPTURE_TIMEOUT_SEC
                    )
                    if (
                        frame is None
                        or new_ts is None
                        or (last_ts is not None and new_ts <= last_ts)
                    ):
                        resp = (
                            json.dumps(
                                {
                                    "success": False,
                                    "error": "STALE_FRAMES",
                                    "detail": "Failed to acquire fresh camera frame within timeout",
                                }
                            )
                            + "\n"
                        )
                        client_sock.sendall(resp.encode("utf-8"))
                        return

                    item = self.media_manager.save_capture(
                        frame,
                        width=self.capture.width,
                        height=self.capture.height,
                        request_id=request_id,
                    )
                    resp = json.dumps({"success": True, "media": item}) + "\n"
                    client_sock.sendall(resp.encode("utf-8"))
                    return
                except StorageError as exc:
                    resp = (
                        json.dumps(
                            {
                                "success": False,
                                "error": exc.code,
                                "detail": exc.message,
                            }
                        )
                        + "\n"
                    )
                    client_sock.sendall(resp.encode("utf-8"))
                    return
                except Exception as exc:
                    logger.exception("Capture execution failure: %s", exc)
                    resp = (
                        json.dumps(
                            {
                                "success": False,
                                "error": "CAPTURE_FAILED",
                                "detail": str(exc),
                            }
                        )
                        + "\n"
                    )
                    client_sock.sendall(resp.encode("utf-8"))
                    return
                finally:
                    self.capture.remove_viewer()

            elif action == "list_media":
                limit = min(max(1, int(req.get("limit", 50))), 100)
                offset = max(0, int(req.get("offset", 0)))
                data = self.media_manager.list_media(limit=limit, offset=offset)
                resp = json.dumps({"success": True, **data}) + "\n"
                client_sock.sendall(resp.encode("utf-8"))
                return

            elif action == "get_media":
                media_id = str(req.get("media_id", ""))
                item, file_path = self.media_manager.get_media(media_id)
                if not item or not file_path:
                    resp = (
                        json.dumps(
                            {
                                "success": False,
                                "error": "NOT_FOUND",
                                "detail": f"Media item '{media_id}' not found",
                            }
                        )
                        + "\n"
                    )
                    client_sock.sendall(resp.encode("utf-8"))
                    return
                content_type = (
                    "video/mp4" if item.get("type") == "video" else "image/jpeg"
                )
                header = (
                    json.dumps(
                        {
                            "success": True,
                            "media_id": media_id,
                            "filename": item["filename"],
                            "content_type": content_type,
                            "size": item["bytes"],
                        }
                    )
                    + "\n"
                )
                client_sock.sendall(header.encode("utf-8"))
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        client_sock.sendall(chunk)
                return

            elif action == "record":
                request_id = req.get("request_id")
                idempotency_key = req.get("idempotency_key")
                res = self.recorder.start_recording(
                    request_id=request_id, idempotency_key=idempotency_key
                )
                resp = json.dumps(res) + "\n"
                client_sock.sendall(resp.encode("utf-8"))
                return

            elif action == "stop_recording":
                recording_id = req.get("recording_id")
                res = self.recorder.stop_recording(recording_id)
                resp = json.dumps(res) + "\n"
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
        client_sock.settimeout(0.5)  # Bound partial writes; disconnect blocked viewers.
        try:
            while self._running:
                frame, ts = self.capture.wait_for_new_frame(last_ts, timeout_sec=1.0)
                if not self._running:
                    break
                readable, _, _ = select.select([client_sock], [], [], 0)
                if readable:
                    # No further client messages are valid; EOF also releases idle USB.
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
