"""Camera IPC Client for MentorPi Pi 5 Web Control.

Provides a client for communicating with the runtime camera worker over
a dedicated Unix domain socket with newline-delimited JSON commands
and binary-framed MJPEG streaming.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
from typing import Any, Generator

from .constants import (
    DEFAULT_CAMERA_SOCKET_PATH,
    ENV_CAMERA_SOCKET_PATH,
    MAX_CAMERA_MESSAGE_BYTES,
)

logger = logging.getLogger(__name__)


class CameraIpcClient:
    """Client for runtime camera worker Unix domain socket."""

    STREAM_MAGIC = b"MJPG"
    HEADER_STRUCT = struct.Struct("!4sI")  # 4 bytes magic, 4 bytes uint32 length

    def __init__(self, socket_path: str | None = None) -> None:
        if socket_path is None:
            socket_path = os.environ.get(
                ENV_CAMERA_SOCKET_PATH, DEFAULT_CAMERA_SOCKET_PATH
            )
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._streams: set[socket.socket] = set()
        self._closed = False

    def close(self) -> None:
        """Cancel streaming reads, including a read running in another thread."""
        with self._lock:
            self._closed = True
            for sock in self._streams:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def get_status(self, timeout_sec: float = 2.0) -> dict[str, Any]:
        """Query live camera status over a short-lived request/response connection."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect(self.socket_path)
            req = json.dumps({"action": "status"}) + "\n"
            sock.sendall(req.encode("utf-8"))
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            if not buf:
                raise ConnectionError("Empty response from camera service")
            line, _ = buf.split(b"\n", 1)
            data = json.loads(line.decode("utf-8"))
            if data.get("success") and "status" in data:
                return data["status"]
            return data
        except Exception as exc:
            logger.debug(
                "Failed to query camera status at '%s': %s", self.socket_path, exc
            )
            return {
                "state": "unavailable",
                "frame_age_sec": None,
                "profile": {"width": 640, "height": 480, "fps": 15},
                "recording_state": "disabled",
                "recording_id": None,
                "elapsed_sec": None,
                "storage_available_bytes": None,
                "viewers_count": 0,
                "last_error": f"Camera service unreachable: {exc}",
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def stream_frames(self, timeout_sec: float = 5.0) -> Generator[bytes, None, None]:
        """Connect and yield binary JPEG frames as they arrive.

        Disconnecting / exiting the generator automatically closes the socket
        and releases the viewer subscription on the camera worker.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        with self._lock:
            if self._closed:
                sock.close()
                return
            self._streams.add(sock)
        try:
            sock.connect(self.socket_path)
            req = json.dumps({"action": "stream"}) + "\n"
            sock.sendall(req.encode("utf-8"))

            header_size = self.HEADER_STRUCT.size
            while True:
                # Read 8-byte frame header
                header_data = self._read_exact(sock, header_size)
                if not header_data:
                    break
                magic, length = self.HEADER_STRUCT.unpack(header_data)
                if magic != self.STREAM_MAGIC:
                    logger.warning("Invalid camera stream magic: %r", magic)
                    break
                if length > MAX_CAMERA_MESSAGE_BYTES:
                    logger.warning("Oversized camera frame: %d bytes", length)
                    break

                # Read JPEG frame bytes
                frame_data = self._read_exact(sock, length)
                if not frame_data:
                    break
                yield bytes(frame_data)
        except (socket.timeout, ConnectionResetError, BrokenPipeError, EOFError):
            pass
        except Exception as exc:
            logger.debug("Camera stream ended: %s", exc)
        finally:
            with self._lock:
                self._streams.discard(sock)
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _read_exact(sock: socket.socket, n: int) -> bytearray | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return buf

    def capture(
        self,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_sec: float = 6.0,
    ) -> dict[str, Any]:
        """Request a fresh JPEG image capture from the runtime camera worker."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect(self.socket_path)
            payload: dict[str, Any] = {"action": "capture"}
            if request_id is not None:
                payload["request_id"] = request_id
            if idempotency_key is not None:
                payload["idempotency_key"] = idempotency_key
            req = json.dumps(payload) + "\n"
            sock.sendall(req.encode("utf-8"))

            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_CAMERA_MESSAGE_BYTES:
                    break
            if not buf:
                raise ConnectionError("Empty response from camera service")
            line, _ = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            logger.debug("Failed to capture image at '%s': %s", self.socket_path, exc)
            return {
                "success": False,
                "error": "CAMERA_UNAVAILABLE",
                "detail": f"Camera capture unreachable: {exc}",
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def list_media(
        self,
        limit: int = 50,
        offset: int = 0,
        timeout_sec: float = 3.0,
    ) -> dict[str, Any]:
        """Retrieve paginated saved media metadata from the camera worker."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect(self.socket_path)
            req = (
                json.dumps(
                    {
                        "action": "list_media",
                        "limit": limit,
                        "offset": offset,
                    }
                )
                + "\n"
            )
            sock.sendall(req.encode("utf-8"))

            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_CAMERA_MESSAGE_BYTES:
                    break
            if not buf:
                raise ConnectionError("Empty response from camera service")
            line, _ = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            logger.debug(
                "Failed to list camera media at '%s': %s", self.socket_path, exc
            )
            return {
                "success": False,
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "error": "WORKER_UNAVAILABLE",
                "detail": str(exc),
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def get_media_stream(
        self,
        media_id: str,
        timeout_sec: float = 5.0,
    ) -> tuple[dict[str, Any], Generator[bytes, None, None] | None]:
        """Stream a saved media file over media IPC without direct disk access.

        Returns (header_dict, generator_yielding_chunks). The socket is closed
        when the generator is exhausted or closed.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect(self.socket_path)
            req = json.dumps({"action": "get_media", "media_id": media_id}) + "\n"
            sock.sendall(req.encode("utf-8"))

            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_CAMERA_MESSAGE_BYTES:
                    break
            if not buf:
                sock.close()
                return {
                    "success": False,
                    "error": "WORKER_UNAVAILABLE",
                    "detail": "Empty response from camera service",
                }, None

            line, remaining = buf.split(b"\n", 1)
            header = json.loads(line.decode("utf-8"))
            if not header.get("success"):
                sock.close()
                return header, None

            def chunk_generator() -> Generator[bytes, None, None]:
                try:
                    if remaining:
                        yield bytes(remaining)
                    while True:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        yield bytes(chunk)
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass

            return header, chunk_generator()
        except Exception as exc:
            try:
                sock.close()
            except Exception:
                pass
            return {
                "success": False,
                "error": "WORKER_UNAVAILABLE",
                "detail": str(exc),
            }, None
