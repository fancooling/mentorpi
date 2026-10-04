"""Bounded Aurora SDK subprocess adapter feeding the existing latest-frame IPC.

The ARM64 helper owns USB and JPEG encoding. This parent owns deadlines, idle
release and retry, so a hung or crashed SDK cannot hold up camera shutdown.
Select the physical camera with UBUNTU_TANK_CAMERA_SERIAL; no implicit mock mode.
"""

from __future__ import annotations

import os
import select
import struct
import subprocess
import time

from ubuntu_tank_protocol.enums import CameraState

from .v4l2_capture import V4L2CameraCapture


class AuroraCameraCapture(V4L2CameraCapture):
    """Capture 640x400 RGB from one serial-selected Aurora, retrying on faults.

    helper is a command prefix (injectable for process-boundary tests). The
    serial is appended as its last argument. No frames for frame_timeout seconds
    kills and reaps the child before retry. Idle workers close USB after the
    inherited idle timeout and wake on a new viewer. Startup samples frames so
    status can discover a live camera before the first browser subscribes.
    """

    def __init__(
        self,
        serial: str,
        helper: tuple[str, ...] = ("/opt/ubuntu_tank/aurora/aurora-capture",),
        frame_timeout: float = 5.0,
        **kwargs,
    ) -> None:
        super().__init__(width=640, height=400, fps=10, is_simulation=False, **kwargs)
        self.serial = serial
        self.helper = helper
        self.frame_timeout = frame_timeout
        self._process: subprocess.Popen | None = None

    def _read(self, pipe, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size and self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Aurora frame deadline exceeded")
            if not select.select([pipe], [], [], min(0.1, remaining))[0]:
                continue
            chunk = os.read(pipe.fileno(), size - len(result))
            if not chunk:
                raise EOFError("Aurora capture process exited")
            result.extend(chunk)
        if not self._running:
            raise InterruptedError("Camera stopping")
        return bytes(result)

    def _run_loop(self) -> None:
        while self._running:
            with self._condition:
                if (
                    self._state == CameraState.LIVE
                    and self._viewer_count == 0
                    and time.monotonic() - self._last_viewer_time
                    > self.idle_timeout_sec
                ):
                    # Preserve the last timestamp: status becomes stale, not falsely fresh.
                    self._condition.wait(0.2)
                    continue
                self._state = CameraState.CONNECTING
            try:
                if not self.serial:
                    raise FileNotFoundError("No Aurora serial has been provisioned")
                self._process = subprocess.Popen(
                    [*self.helper, self.serial], stdout=subprocess.PIPE, bufsize=0
                )
                pipe = self._process.stdout
                assert pipe is not None
                while self._running:
                    with self._lock:
                        if (
                            self._state == CameraState.LIVE
                            and self._viewer_count == 0
                            and time.monotonic() - self._last_viewer_time
                            > self.idle_timeout_sec
                        ):
                            break
                    deadline = time.monotonic() + self.frame_timeout
                    magic, size = struct.unpack("!4sI", self._read(pipe, 8, deadline))
                    if magic != b"MJPG" or not 4 <= size <= 2 * 1024 * 1024:
                        raise ValueError("Invalid Aurora JPEG framing")
                    frame = self._read(pipe, size, deadline)
                    if not frame.startswith(b"\xff\xd8") or not frame.endswith(
                        b"\xff\xd9"
                    ):
                        raise ValueError("Invalid Aurora JPEG")
                    with self._condition:
                        self._latest_frame = frame
                        self._latest_timestamp = time.monotonic()
                        self._state = CameraState.LIVE
                        self._last_error = None
                        self._condition.notify_all()
            except (OSError, EOFError, TimeoutError, ValueError) as error:
                with self._condition:
                    self._state = CameraState.UNAVAILABLE
                    self._last_error = str(error)
                    self._latest_frame = None
                    self._latest_timestamp = None
                    self._condition.notify_all()
            finally:
                process = self._process
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=1.5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    process.wait()
                    if process.stdout:
                        process.stdout.close()
                    self._process = None
            with self._condition:
                if self._running:
                    self._condition.wait(0.5)
