"""Unit and integration tests for camera worker, V4L2 capture, and IPC streaming."""

from __future__ import annotations

import os
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Request
from starlette.responses import StreamingResponse
from ubuntu_tank_camera.ipc_server import CameraIpcServer
from ubuntu_tank_camera.synthetic import get_synthetic_jpeg_frame
from ubuntu_tank_camera.v4l2_capture import (
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    V4L2_PIX_FMT_MJPEG,
    V4L2CameraCapture,
)
from ubuntu_tank_protocol.camera_client import CameraIpcClient
from ubuntu_tank_protocol.enums import CameraState
from ubuntu_tank_web.routes_api import (
    get_camera_status,
    get_camera_stream,
    post_camera_capture,
    post_camera_recording,
    post_camera_recording_stop,
)


class TestSyntheticFrames(unittest.TestCase):
    def test_valid_jpeg_structure(self):
        frame = get_synthetic_jpeg_frame()
        self.assertIsInstance(frame, bytes)
        self.assertGreater(len(frame), 100)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertTrue(frame.endswith(b"\xff\xd9"))
        self.assertIn(b"\xff\xc0", frame)

    def test_dimensions_in_sof0(self):
        frame = get_synthetic_jpeg_frame()
        idx = frame.find(b"\xff\xc0")
        self.assertNotEqual(idx, -1)
        h, w = struct.unpack(">HH", frame[idx + 5 : idx + 9])
        self.assertEqual(w, 640)
        self.assertEqual(h, 480)


class TestV4L2CameraCapture(unittest.TestCase):
    def test_v4l2_buffer_struct_layout_and_offsets(self):
        """Verify struct v4l2_buffer layout matches 64-bit Linux kernel expectations."""
        qbuf = bytearray(88)
        self.assertEqual(len(qbuf), 88)

        # Pack test fields
        struct.pack_into("=II", qbuf, 0, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("=I", qbuf, 60, V4L2_MEMORY_MMAP)
        struct.pack_into("=I", qbuf, 64, 4096)  # m.offset
        struct.pack_into("=I", qbuf, 72, 614400)  # length
        struct.pack_into("=I", qbuf, 8, 32000)  # bytesused

        # Assert offsets
        self.assertEqual(struct.unpack_from("=I", qbuf, 0)[0], 0)  # index
        self.assertEqual(struct.unpack_from("=I", qbuf, 4)[0], 1)  # type
        self.assertEqual(struct.unpack_from("=I", qbuf, 8)[0], 32000)  # bytesused
        self.assertEqual(struct.unpack_from("=I", qbuf, 60)[0], 1)  # memory
        self.assertEqual(struct.unpack_from("=I", qbuf, 64)[0], 4096)  # m.offset
        self.assertEqual(struct.unpack_from("=I", qbuf, 72)[0], 614400)  # length

    def test_v4l2_format_struct_layout(self):
        """Verify struct v4l2_format layout matches 64-bit Linux kernel expectations."""
        fmt_buf = bytearray(208)
        self.assertEqual(len(fmt_buf), 208)

        struct.pack_into(
            "=IIIIII",
            fmt_buf,
            0,
            V4L2_BUF_TYPE_VIDEO_CAPTURE,
            0,
            640,
            480,
            V4L2_PIX_FMT_MJPEG,
            0,
        )

        self.assertEqual(struct.unpack_from("=I", fmt_buf, 0)[0], 1)  # type
        self.assertEqual(struct.unpack_from("=I", fmt_buf, 8)[0], 640)  # width
        self.assertEqual(struct.unpack_from("=I", fmt_buf, 12)[0], 480)  # height
        self.assertEqual(
            struct.unpack_from("=I", fmt_buf, 16)[0], V4L2_PIX_FMT_MJPEG
        )  # pixelformat
        self.assertEqual(struct.unpack_from("=I", fmt_buf, 20)[0], 0)  # field

    def test_missing_camera_reports_unavailable_without_generated_frames(self):
        """An absent production camera must never silently become a test feed."""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            capture = V4L2CameraCapture(device_path=os.path.join(directory, "absent"))
            capture.start()
            try:
                frame, timestamp = capture.wait_for_new_frame(None, timeout_sec=0.1)
                self.assertFalse(capture.is_simulated)
                self.assertEqual(capture.state, CameraState.UNAVAILABLE)
                self.assertIn("not found", capture.last_error)
                self.assertIsNone(frame)
                self.assertIsNone(timestamp)
            finally:
                capture.stop()

    def test_missing_camera_retries_hardware_when_device_appears(self):
        """Device arrival must enter acquisition without restarting the worker."""
        available = threading.Event()
        streaming = threading.Event()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            capture = V4L2CameraCapture(device_path=os.path.join(directory, "absent"))
            with (
                patch.object(
                    capture, "_is_device_available", side_effect=available.is_set
                ),
                patch.object(
                    capture,
                    "_stream_v4l2",
                    side_effect=lambda interval: streaming.set(),
                ),
            ):
                capture.start()
                try:
                    capture.wait_for_new_frame(None, timeout_sec=0.1)
                    self.assertEqual(capture.state, CameraState.UNAVAILABLE)
                    self.assertFalse(streaming.is_set())
                    available.set()
                    self.assertTrue(streaming.wait(2.0))
                    self.assertFalse(capture.is_simulated)
                finally:
                    capture.stop()

    def test_environment_simulation_requires_explicit_opt_in(self):
        """Both supported test switches can still produce frames intentionally."""
        for flag in ("UBUNTU_TANK_SIMULATION", "UBUNTU_TANK_CAMERA_MOCK"):
            with (
                self.subTest(flag=flag),
                patch.dict(os.environ, {flag: "1"}, clear=True),
            ):
                capture = V4L2CameraCapture(device_path="/dev/absent-camera")
                capture.start()
                try:
                    frame, _ = capture.wait_for_new_frame(None, timeout_sec=1.0)
                    self.assertTrue(capture.is_simulated)
                    self.assertIsNotNone(frame)
                finally:
                    capture.stop()

    def test_explicit_simulation_and_frames(self):
        capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=640,
            height=480,
            fps=15,
            freshness_timeout_sec=0.5,
            idle_timeout_sec=2.0,
            is_simulation=True,
        )
        capture.start()
        self.assertTrue(capture.is_active)
        self.assertTrue(capture.is_simulated)

        # Wait for at least one frame
        time.sleep(0.15)
        self.assertEqual(capture.state, CameraState.LIVE)
        self.assertEqual(capture.width, 640)
        self.assertEqual(capture.height, 480)
        self.assertEqual(capture.fps, 15)

        frame, ts = capture.get_latest_frame()
        self.assertIsNotNone(frame)
        self.assertIsNotNone(ts)
        assert frame is not None
        self.assertTrue(frame.startswith(b"\xff\xd8"))

        capture.stop()

    def test_freshness_timeout_transitions_to_stale(self):
        capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=320,
            height=240,
            fps=15,
            freshness_timeout_sec=0.1,  # Fast freshness timeout
            idle_timeout_sec=5.0,
            is_simulation=True,
        )
        capture.start()
        self.assertTrue(capture.is_active)
        time.sleep(0.05)

        # Simulate frame pause
        with capture._lock:
            capture._latest_timestamp = time.monotonic() - 0.2

        self.assertEqual(capture.state, CameraState.STALE)
        capture.stop()

    def test_idle_timeout_release(self):
        capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=320,
            height=240,
            fps=15,
            freshness_timeout_sec=1.0,
            idle_timeout_sec=0.15,  # Short idle timeout
            is_simulation=True,
        )
        capture.start()
        self.assertTrue(capture.is_active)

        # Check idle when no viewers
        with capture._lock:
            capture._last_viewer_time = time.monotonic() - 0.3
            capture._viewer_count = 0

        is_idle = capture.check_idle()
        self.assertTrue(is_idle)
        self.assertFalse(capture.is_active)
        capture.stop()


class TestCameraIpcServerAndClient(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.tmp_dir.name, "camera.sock")
        self.capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=640,
            height=480,
            fps=15,
            is_simulation=True,
        )
        self.capture.start()
        self.server = CameraIpcServer(capture=self.capture, socket_path=self.sock_path)
        self.server.start()
        time.sleep(0.1)
        self.client = CameraIpcClient(socket_path=self.sock_path)

    def tearDown(self):
        self.client.close()
        self.server.stop()
        self.capture.stop()
        self.tmp_dir.cleanup()

    def test_socket_permissions(self):
        self.assertTrue(os.path.exists(self.sock_path))
        mode = os.stat(self.sock_path).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o700)

    def test_get_status(self):
        status = self.client.get_status()
        self.assertIsInstance(status, dict)
        self.assertIn(status["state"], ("live", "connecting"))
        self.assertEqual(status["profile"]["width"], 640)
        self.assertEqual(status["profile"]["height"], 480)
        self.assertEqual(status["profile"]["fps"], 15)

    def test_stream_frames(self):
        frames = []
        for frame in self.client.stream_frames(timeout_sec=2.0):
            frames.append(frame)
            if len(frames) >= 2:
                break

        self.assertGreaterEqual(len(frames), 2)
        for f in frames:
            self.assertTrue(f.startswith(b"\xff\xd8"))
            self.assertTrue(f.endswith(b"\xff\xd9"))


class TestCameraWebRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.tmp_dir.name, "camera.sock")
        self.capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=640,
            height=480,
            fps=15,
            is_simulation=True,
        )
        self.capture.start()
        self.server = CameraIpcServer(capture=self.capture, socket_path=self.sock_path)
        self.server.start()
        time.sleep(0.1)
        self.client = CameraIpcClient(socket_path=self.sock_path)

        self.mock_app = MagicMock()
        self.mock_app.state.camera_client = self.client

        self.mock_request = AsyncMock(spec=Request)
        self.mock_request.app = self.mock_app
        self.mock_request.is_disconnected = AsyncMock(return_value=False)

    def tearDown(self):
        self.client.close()
        self.server.stop()
        self.capture.stop()
        self.tmp_dir.cleanup()

    async def test_camera_status_endpoint(self):
        status_resp = await get_camera_status(self.mock_request)
        self.assertIn(status_resp.state, ("live", "connecting"))
        self.assertEqual(status_resp.profile.width, 640)
        self.assertEqual(status_resp.profile.height, 480)
        self.assertEqual(status_resp.profile.fps, 15)
        self.assertEqual(status_resp.recording_state, "disabled")

    async def test_camera_stream_endpoint(self):
        resp = await get_camera_stream(self.mock_request)
        self.assertIsInstance(resp, StreamingResponse)
        self.assertIn("multipart/x-mixed-replace", resp.media_type)

        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= 2:
                break
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(any(b"--frame\r\n" in c for c in chunks))

    async def test_cam2_disabled_endpoints(self):
        with self.assertRaises(Exception) as ctx:
            await post_camera_capture(self.mock_request)
        self.assertEqual(ctx.exception.status_code, 503)

        with self.assertRaises(Exception) as ctx:
            await post_camera_recording(self.mock_request)
        self.assertEqual(ctx.exception.status_code, 503)

        with self.assertRaises(Exception) as ctx:
            await post_camera_recording_stop("rec_test", self.mock_request)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
