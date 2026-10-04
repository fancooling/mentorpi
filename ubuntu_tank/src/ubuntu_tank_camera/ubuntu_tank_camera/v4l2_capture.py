"""V4L2 camera capture adapter with frame freshness, idle timeout, and simulation support.

Reads frames from Linux V4L2 device nodes (/dev/video0) using standard
Linux ioctl and memory mapping, or generates synthetic frames under
simulation/mock mode.
"""

from __future__ import annotations

import fcntl
import logging
import mmap
import os
import select
import stat
import struct
import threading
import time

from ubuntu_tank_protocol.constants import (
    DEFAULT_CAMERA_DEVICE,
    DEFAULT_CAMERA_FPS,
    DEFAULT_CAMERA_FRESHNESS_TIMEOUT_SEC,
    DEFAULT_CAMERA_HEIGHT,
    DEFAULT_CAMERA_IDLE_TIMEOUT_SEC,
    DEFAULT_CAMERA_WIDTH,
    ENV_CAMERA_DEVICE,
)
from ubuntu_tank_protocol.enums import CameraState

from .synthetic import get_synthetic_jpeg_frame

logger = logging.getLogger(__name__)

# V4L2 ioctl codes and constants for 64-bit Linux
VIDIOC_QUERYCAP = 0x80685600
VIDIOC_G_FMT = 0xC0D05604
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_PIX_FMT_MJPEG = 0x47504A4D  # 'MJPG'
V4L2_PIX_FMT_YUYV = 0x56595559  # 'YUYV'


class V4L2CameraCapture:
    """Acquire V4L2 frames; generate test frames only on explicit opt-in.

    Missing hardware reports unavailable and is retried. Synthetic capture is
    selected by is_simulation or the UBUNTU_TANK_SIMULATION and
    UBUNTU_TANK_CAMERA_MOCK environment flags.
    """

    def __init__(
        self,
        device_path: str | None = None,
        width: int = DEFAULT_CAMERA_WIDTH,
        height: int = DEFAULT_CAMERA_HEIGHT,
        fps: int = DEFAULT_CAMERA_FPS,
        freshness_timeout_sec: float = DEFAULT_CAMERA_FRESHNESS_TIMEOUT_SEC,
        idle_timeout_sec: float = DEFAULT_CAMERA_IDLE_TIMEOUT_SEC,
        is_simulation: bool | None = None,
    ) -> None:
        if device_path is None:
            device_path = os.environ.get(ENV_CAMERA_DEVICE, DEFAULT_CAMERA_DEVICE)
        self.device_path = device_path
        self.width = width
        self.height = height
        self.fps = fps
        self.freshness_timeout_sec = freshness_timeout_sec
        self.idle_timeout_sec = idle_timeout_sec

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        self._latest_frame: bytes | None = None
        self._latest_timestamp: float | None = None
        self._state = CameraState.CONNECTING
        self._last_error: str | None = None

        self._viewer_count = 0
        self._last_viewer_time = time.monotonic()
        self._condition = threading.Condition(self._lock)

        if is_simulation is not None:
            self._is_simulation = is_simulation
        else:
            self._is_simulation = (
                os.environ.get("UBUNTU_TANK_SIMULATION") == "1"
                or os.environ.get("UBUNTU_TANK_CAMERA_MOCK") == "1"
            )

    @property
    def is_simulated(self) -> bool:
        return self._is_simulation

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._running

    def check_idle(self) -> bool:
        with self._lock:
            idle_duration = time.monotonic() - self._last_viewer_time
            if self._viewer_count == 0 and idle_duration > self.idle_timeout_sec:
                self._running = False
                self._condition.notify_all()
                return True
            return False

    @property
    def state(self) -> CameraState:
        with self._lock:
            if (
                self._state == CameraState.LIVE
                and self._latest_timestamp is not None
                and (time.monotonic() - self._latest_timestamp)
                > self.freshness_timeout_sec
            ):
                return CameraState.STALE
            return self._state

    @property
    def frame_age_sec(self) -> float | None:
        with self._lock:
            if self._latest_timestamp is None:
                return None
            return round(time.monotonic() - self._latest_timestamp, 3)

    @property
    def viewers_count(self) -> int:
        with self._lock:
            return self._viewer_count

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def add_viewer(self) -> None:
        with self._lock:
            self._viewer_count += 1
            self._last_viewer_time = time.monotonic()
            self._condition.notify_all()

    def remove_viewer(self) -> None:
        with self._lock:
            if self._viewer_count > 0:
                self._viewer_count -= 1
            self._last_viewer_time = time.monotonic()
            self._condition.notify_all()

    def get_latest_frame(self) -> tuple[bytes | None, float | None]:
        with self._lock:
            return self._latest_frame, self._latest_timestamp

    def wait_for_new_frame(
        self, last_timestamp: float | None, timeout_sec: float = 1.0
    ) -> tuple[bytes | None, float | None]:
        """Wait until a newer frame is captured or timeout expires."""
        with self._condition:
            end = time.monotonic() + timeout_sec
            while self._running:
                if self._latest_timestamp is not None and (
                    last_timestamp is None or self._latest_timestamp > last_timestamp
                ):
                    return self._latest_frame, self._latest_timestamp
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._latest_frame, self._latest_timestamp

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop, name="CameraCaptureWorker", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._condition.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _is_device_available(self) -> bool:
        if not os.path.exists(self.device_path):
            return False
        try:
            mode = os.stat(self.device_path).st_mode
            return stat.S_ISCHR(mode)
        except OSError:
            return False

    def _run_loop(self) -> None:
        """Main acquisition and lifecycle thread."""
        logger.info(
            "Starting camera worker loop (device=%s, sim=%s)",
            self.device_path,
            self._is_simulation,
        )

        frame_interval = 1.0 / max(1, self.fps)
        while self._running:
            # Check for simulated / mock mode first
            if self._is_simulation:
                self._run_simulated_cycle(frame_interval)
                continue

            # Real hardware V4L2 mode
            if not self._is_device_available():
                with self._lock:
                    self._state = CameraState.UNAVAILABLE
                    self._last_error = f"Device '{self.device_path}' not found"
                time.sleep(1.0)
                continue

            # Attempt to open and stream from V4L2 device
            try:
                self._stream_v4l2(frame_interval)
            except Exception as exc:  # noqa: BLE001 - Media faults must not kill the worker.
                logger.warning("V4L2 streaming error on %s: %s", self.device_path, exc)
                with self._lock:
                    self._state = CameraState.ERROR
                    self._last_error = str(exc)
                time.sleep(1.0)

    def _run_simulated_cycle(self, frame_interval: float) -> None:
        """Deliver synthetic frames at target FPS with idle-timeout awareness."""
        with self._lock:
            idle_duration = time.monotonic() - self._last_viewer_time
            if self._viewer_count == 0 and idle_duration > self.idle_timeout_sec:
                # Idle: wait for a viewer without generating unneeded frames
                self._state = CameraState.LIVE
                self._condition.wait(1.0)
                return

            frame = get_synthetic_jpeg_frame()
            now = time.monotonic()
            self._latest_frame = frame
            self._latest_timestamp = now
            self._state = CameraState.LIVE
            self._last_error = None
            self._condition.notify_all()

        time.sleep(frame_interval)

    def _stream_v4l2(self, frame_interval: float) -> None:
        """Open V4L2 device and capture frames using mmap."""
        fd = os.open(self.device_path, os.O_RDWR | os.O_NONBLOCK)
        try:
            with self._lock:
                self._state = CameraState.CONNECTING
                self._last_error = None

            # Try negotiating format: type=1 (V4L2_BUF_TYPE_VIDEO_CAPTURE), width, height, pixelformat=V4L2_PIX_FMT_MJPEG, field=0
            fmt_buf = bytearray(208)
            struct.pack_into(
                "=IIIIII",
                fmt_buf,
                0,
                V4L2_BUF_TYPE_VIDEO_CAPTURE,
                0,
                self.width,
                self.height,
                V4L2_PIX_FMT_MJPEG,
                0,
            )
            try:
                fcntl.ioctl(fd, VIDIOC_S_FMT, fmt_buf)
            except OSError as e:
                logger.debug(
                    "VIDIOC_S_FMT failed (device may use default format): %s", e
                )

            # Request 2 MMAP buffers
            reqbuf_struct = struct.Struct("=IIII")
            req = reqbuf_struct.pack(
                2, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP, 0
            )
            res = fcntl.ioctl(fd, VIDIOC_REQBUFS, req)
            count, _, _, _ = reqbuf_struct.unpack(res)
            if count == 0:
                raise RuntimeError("V4L2 driver allocated 0 buffers")

            # Query and mmap each buffer (struct v4l2_buffer is 88 bytes on 64-bit Linux)
            buffers: list[tuple[mmap.mmap, int]] = []
            for i in range(count):
                qbuf = bytearray(88)
                struct.pack_into("=II", qbuf, 0, i, V4L2_BUF_TYPE_VIDEO_CAPTURE)
                struct.pack_into("=I", qbuf, 60, V4L2_MEMORY_MMAP)
                fcntl.ioctl(fd, VIDIOC_QUERYBUF, qbuf)
                buf_length = struct.unpack_from("=I", qbuf, 72)[0]
                offset = struct.unpack_from("=I", qbuf, 64)[0]
                if buf_length == 0:
                    raise RuntimeError(f"V4L2 buffer {i} reported length 0")
                mm = mmap.mmap(
                    fd,
                    buf_length,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=offset,
                )
                buffers.append((mm, buf_length))
                # Queue buffer
                qbuf_queue = bytearray(88)
                struct.pack_into("=II", qbuf_queue, 0, i, V4L2_BUF_TYPE_VIDEO_CAPTURE)
                struct.pack_into("=I", qbuf_queue, 60, V4L2_MEMORY_MMAP)
                fcntl.ioctl(fd, VIDIOC_QBUF, qbuf_queue)

            # Start streaming
            buf_type = struct.pack("=I", V4L2_BUF_TYPE_VIDEO_CAPTURE)
            fcntl.ioctl(fd, VIDIOC_STREAMON, buf_type)

            try:
                with self._lock:
                    self._state = CameraState.LIVE

                while self._running:
                    # Check idle timeout
                    with self._lock:
                        if (
                            self._viewer_count == 0
                            and (time.monotonic() - self._last_viewer_time)
                            > self.idle_timeout_sec
                        ):
                            # Idle pause
                            self._condition.wait(1.0)
                            continue

                    # Select with timeout
                    r, _, _ = select.select([fd], [], [], frame_interval * 2)
                    if not r:
                        continue

                    # Dequeue buffer
                    dqbuf = bytearray(88)
                    struct.pack_into("=I", dqbuf, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE)
                    struct.pack_into("=I", dqbuf, 60, V4L2_MEMORY_MMAP)
                    try:
                        fcntl.ioctl(fd, VIDIOC_DQBUF, dqbuf)
                    except OSError as e:
                        if e.errno in (11, 35):  # EAGAIN / EWOULDBLOCK
                            continue
                        raise

                    buf_idx = struct.unpack_from("=I", dqbuf, 0)[0]
                    bytesused = struct.unpack_from("=I", dqbuf, 8)[0]

                    mm, buf_len = buffers[buf_idx]
                    raw_data = mm[
                        : min(bytesused, buf_len) if bytesused > 0 else buf_len
                    ]

                    # Re-queue buffer
                    qbuf_requeue = bytearray(88)
                    struct.pack_into(
                        "=II", qbuf_requeue, 0, buf_idx, V4L2_BUF_TYPE_VIDEO_CAPTURE
                    )
                    struct.pack_into("=I", qbuf_requeue, 60, V4L2_MEMORY_MMAP)
                    fcntl.ioctl(fd, VIDIOC_QBUF, qbuf_requeue)

                    # Update latest frame
                    with self._lock:
                        self._latest_frame = bytes(raw_data)
                        self._latest_timestamp = time.monotonic()
                        self._state = CameraState.LIVE
                        self._condition.notify_all()

                    time.sleep(frame_interval * 0.5)

            finally:
                try:
                    fcntl.ioctl(fd, VIDIOC_STREAMOFF, buf_type)
                except OSError as exc:
                    logger.debug("Camera stream cleanup failed: %s", exc)
                for mm, _ in buffers:
                    try:
                        mm.close()
                    except (OSError, ValueError, BufferError) as exc:
                        logger.debug("Camera buffer cleanup failed: %s", exc)
        finally:
            try:
                os.close(fd)
            except OSError as exc:
                logger.debug("Camera descriptor cleanup failed: %s", exc)
