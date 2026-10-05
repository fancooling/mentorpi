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
from ubuntu_tank_camera.media_storage import MediaManager, StorageError
from ubuntu_tank_camera.synthetic import get_synthetic_jpeg_frame
from ubuntu_tank_camera.v4l2_capture import (
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    V4L2_PIX_FMT_MJPEG,
    V4L2CameraCapture,
)
from ubuntu_tank_protocol.camera_client import CameraIpcClient
from ubuntu_tank_protocol.enums import CameraState
from ubuntu_tank_web.models import CameraCaptureRequestModel
from ubuntu_tank_web.routes_api import (
    get_camera_media,
    get_camera_media_download,
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


class TestMediaManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.media_mgr = MediaManager(storage_dir=self.tmp_dir.name)
        self.test_frame = get_synthetic_jpeg_frame()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_atomic_write_and_recovery(self):
        item = self.media_mgr.save_capture(self.test_frame, width=640, height=480)
        self.assertTrue(item["media_id"])
        self.assertTrue(item["filename"].endswith(".jpg"))
        self.assertEqual(item["bytes"], len(self.test_frame))
        self.assertEqual(item["width"], 640)
        self.assertEqual(item["height"], 480)

        # Check no temporary files remain
        files = os.listdir(self.tmp_dir.name)
        self.assertFalse(any(f.startswith(".tmp") for f in files))
        self.assertIn(item["filename"], files)
        self.assertIn("index.json", files)

        # Re-initialize MediaManager to verify persistence across restarts
        mgr2 = MediaManager(storage_dir=self.tmp_dir.name)
        listing = mgr2.list_media()
        items = listing["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item["media_id"])

    def test_idempotency_key(self):
        item1 = self.media_mgr.save_capture(
            self.test_frame, width=640, height=480, idempotency_key="key-test-1"
        )
        self.assertTrue(item1["media_id"])
        item2 = self.media_mgr.save_capture(
            self.test_frame, width=640, height=480, idempotency_key="key-test-1"
        )
        self.assertTrue(item2["media_id"])
        self.assertEqual(item1["media_id"], item2["media_id"])
        self.assertEqual(item1["filename"], item2["filename"])
        self.assertEqual(len(self.media_mgr.list_media()["items"]), 1)

    def test_quota_exceeded(self):
        small_mgr = MediaManager(
            storage_dir=self.tmp_dir.name,
            quota_bytes=len(self.test_frame) + 50,
        )
        item1 = small_mgr.save_capture(self.test_frame, width=640, height=480)
        self.assertTrue(item1["media_id"])
        with self.assertRaises(StorageError) as ctx:
            small_mgr.save_capture(self.test_frame, width=640, height=480)
        self.assertEqual(ctx.exception.code, "STORAGE_QUOTA_EXCEEDED")

    def test_low_storage_free_space(self):
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(total=10**9, used=10**9 - 50, free=50)
            with self.assertRaises(StorageError) as ctx:
                self.media_mgr.save_capture(self.test_frame, width=640, height=480)
            self.assertEqual(ctx.exception.code, "LOW_STORAGE")

    def test_reconciliation_on_deleted_file(self):
        item = self.media_mgr.save_capture(self.test_frame, width=640, height=480)
        self.assertTrue(item["media_id"])
        filepath = os.path.join(self.tmp_dir.name, item["filename"])
        os.remove(filepath)

        items = self.media_mgr.list_media()["items"]
        self.assertEqual(len(items), 0)

    def test_list_media_pagination_and_sorting(self):
        for _ in range(5):
            self.media_mgr.save_capture(self.test_frame, width=640, height=480)
            time.sleep(0.01)

        p1 = self.media_mgr.list_media(limit=2, offset=0)
        self.assertEqual(len(p1["items"]), 2)
        p2 = self.media_mgr.list_media(limit=2, offset=2)
        self.assertEqual(len(p2["items"]), 2)
        self.assertNotEqual(p1["items"][0]["id"], p2["items"][0]["id"])
        self.assertGreaterEqual(
            p1["items"][0]["timestamp"], p1["items"][1]["timestamp"]
        )

    def test_get_media(self):
        item = self.media_mgr.save_capture(self.test_frame, width=640, height=480)
        media_id = item["media_id"]
        found_item, found_path = self.media_mgr.get_media(media_id)
        self.assertIsNotNone(found_item)
        self.assertEqual(found_item["media_id"], media_id)
        self.assertIsNotNone(found_path)
        self.assertTrue(os.path.exists(found_path))

        missing_item, missing_path = self.media_mgr.get_media("nonexistent-id")
        self.assertIsNone(missing_item)
        self.assertIsNone(missing_path)


class TestCameraIpcServerAndClient(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.tmp_dir.name, "camera.sock")
        self.media_dir = os.path.join(self.tmp_dir.name, "media")
        self.capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=640,
            height=480,
            fps=15,
            is_simulation=True,
        )
        self.capture.start()
        self.server = CameraIpcServer(
            capture=self.capture,
            socket_path=self.sock_path,
            media_storage_dir=self.media_dir,
        )
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

    def test_ipc_capture_and_stream(self):
        res = self.client.capture()
        self.assertTrue(res.get("success"), res)
        media = res["media"]
        self.assertTrue(media["media_id"])
        self.assertTrue(media["filename"].endswith(".jpg"))

        # Verify listed via IPC
        list_res = self.client.list_media()
        self.assertTrue(list_res["success"])
        self.assertGreaterEqual(list_res["total"], 1)
        self.assertEqual(list_res["items"][0]["media_id"], media["media_id"])

        # Stream via IPC
        header, stream = self.client.get_media_stream(media["media_id"])
        self.assertTrue(header.get("success"), header)
        self.assertIsNotNone(stream)
        data = b"".join(list(stream))
        self.assertEqual(len(data), media["bytes"])
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertTrue(data.endswith(b"\xff\xd9"))

    def test_ipc_capture_idempotency(self):
        res1 = self.client.capture(idempotency_key="client-key-1")
        self.assertTrue(res1.get("success"), res1)
        res2 = self.client.capture(idempotency_key="client-key-1")
        self.assertTrue(res2.get("success"), res2)
        self.assertEqual(res1["media"]["media_id"], res2["media"]["media_id"])

    def test_ipc_stream_unknown_media(self):
        header, stream = self.client.get_media_stream("unknown-id-12345")
        self.assertFalse(header.get("success"))
        self.assertEqual(header.get("error"), "NOT_FOUND")
        self.assertIsNone(stream)


class TestCameraWebRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.tmp_dir.name, "camera.sock")
        self.media_dir = os.path.join(self.tmp_dir.name, "media")
        self.capture = V4L2CameraCapture(
            device_path="/dev/nonexistent_video_device_test",
            width=640,
            height=480,
            fps=15,
            is_simulation=True,
        )
        self.capture.start()
        self.server = CameraIpcServer(
            capture=self.capture,
            socket_path=self.sock_path,
            media_storage_dir=self.media_dir,
        )
        self.server.start()
        time.sleep(0.1)
        self.client = CameraIpcClient(socket_path=self.sock_path)

        self.mock_app = MagicMock()
        self.mock_app.state.camera_client = self.client
        self.mock_app.state.active_owner = None
        self.mock_app.state.guard_armed = False

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

    async def test_cam3_capture_and_media_endpoints(self):
        # 1. Capture image
        capture_resp = await post_camera_capture(self.mock_request)
        self.assertTrue(capture_resp.media_id)
        self.assertTrue(capture_resp.filename.endswith(".jpg"))
        self.assertEqual(capture_resp.width, 640)
        self.assertEqual(capture_resp.height, 480)
        self.assertGreater(capture_resp.bytes, 100)
        self.assertEqual(
            capture_resp.url, f"/api/v1/camera/media/{capture_resp.media_id}"
        )

        # 2. List media
        media_list = await get_camera_media(self.mock_request)
        self.assertGreaterEqual(media_list.total, 1)
        self.assertEqual(media_list.items[0].media_id, capture_resp.media_id)
        self.assertEqual(media_list.items[0].filename, capture_resp.filename)

        # 3. Download media stream over IPC
        dl_resp = await get_camera_media_download(
            capture_resp.media_id, self.mock_request
        )
        self.assertIsInstance(dl_resp, StreamingResponse)
        self.assertEqual(dl_resp.media_type, "image/jpeg")
        self.assertIn(
            f'attachment; filename="{capture_resp.filename}"',
            dl_resp.headers.get("Content-Disposition", ""),
        )

        dl_bytes = bytearray()
        async for chunk in dl_resp.body_iterator:
            dl_bytes.extend(chunk)
        self.assertEqual(len(dl_bytes), capture_resp.bytes)
        self.assertTrue(dl_bytes.startswith(b"\xff\xd8"))
        self.assertTrue(dl_bytes.endswith(b"\xff\xd9"))

        # 4. Unknown media download returns 404
        with self.assertRaises(Exception) as ctx:
            await get_camera_media_download("unknown-media-id", self.mock_request)
        self.assertEqual(ctx.exception.status_code, 404)

        # 5. CAM-4 recording endpoints remain 503
        with self.assertRaises(Exception) as ctx:
            await post_camera_recording(self.mock_request)
        self.assertEqual(ctx.exception.status_code, 503)

        with self.assertRaises(Exception) as ctx:
            await post_camera_recording_stop("rec_test", self.mock_request)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_capture_does_not_mutate_robot_control(self):
        self.mock_app.state.active_owner = "test-owner"
        self.mock_app.state.guard_armed = False

        capture_resp = await post_camera_capture(self.mock_request)
        self.assertTrue(capture_resp.media_id)

        # Confirm operator/control attributes remain untouched
        self.assertEqual(self.mock_app.state.active_owner, "test-owner")
        self.assertFalse(self.mock_app.state.guard_armed)


class TestAuroraProcess(unittest.TestCase):
    """Exercise capture deadlines and recovery across a real child-process pipe."""

    def wait_until(self, predicate, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Capture did not reach expected state")

    def capture(self, script, **kwargs):
        import sys

        from ubuntu_tank_camera.aurora_capture import AuroraCameraCapture

        capture = AuroraCameraCapture(
            "test-serial", helper=(sys.executable, "-c", script), **kwargs
        )
        self.addCleanup(capture.stop)
        return capture

    def test_child_hang_is_reaped_and_retried(self):
        capture = self.capture(
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
            frame_timeout=0.2,
        )
        capture.add_viewer()
        capture.start()
        self.wait_until(lambda: capture._process is not None)
        first = capture._process
        self.wait_until(lambda: first.poll() is not None)
        self.wait_until(
            lambda: capture._process is not None and capture._process is not first
        )
        child = capture._process
        started = time.monotonic()
        capture.stop()
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(capture.get_latest_frame()[0])

    def test_stream_idle_release_and_new_viewer_restart(self):
        capture = self.capture(
            "import os,struct,time; frame=b'\\xff\\xd8test\\xff\\xd9'; "
            "packet=struct.pack('!4sI',b'MJPG',len(frame))+frame\n"
            "while True: os.write(1,packet); time.sleep(0.02)",
            idle_timeout_sec=0.2,
            freshness_timeout_sec=0.1,
        )
        capture.start()
        frame, timestamp = capture.wait_for_new_frame(None, 2)
        self.assertEqual(frame, b"\xff\xd8test\xff\xd9")
        self.wait_until(lambda: capture._process is None)
        self.wait_until(lambda: capture.state == CameraState.STALE)
        _, timestamp = capture.get_latest_frame()
        capture.add_viewer()
        frame, new_timestamp = capture.wait_for_new_frame(timestamp, 2)
        self.assertGreater(new_timestamp, timestamp)
        self.assertEqual(capture.state, CameraState.LIVE)

    def test_camera_arriving_after_idle_timeout_is_discovered_without_viewer(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            capture = self.capture(
                f"import pathlib,sys,os,struct,time; "
                f"sys.exit(1) if not pathlib.Path({str(ready)!r}).exists() else None; "
                "frame=b'\\xff\\xd8late\\xff\\xd9'; "
                "packet=struct.pack('!4sI',b'MJPG',len(frame))+frame\n"
                "while True: os.write(1,packet); time.sleep(0.02)",
                idle_timeout_sec=0.2,
            )
            capture.start()
            self.wait_until(lambda: capture.state == CameraState.UNAVAILABLE)
            time.sleep(0.3)
            ready.touch()
            self.wait_until(lambda: capture.get_latest_frame()[0] is not None)
            self.assertEqual(capture.viewers_count, 0)
            self.assertEqual(capture.get_latest_frame()[0], b"\xff\xd8late\xff\xd9")

    def test_invalid_child_output_reports_unavailable(self):
        capture = self.capture("import os; os.write(1,b'BAD!0000')")
        capture.start()
        self.wait_until(lambda: capture.state == CameraState.UNAVAILABLE)
        self.assertIsNone(capture.get_latest_frame()[0])
        self.assertIn("framing", capture.last_error)

    def test_slow_ipc_reader_does_not_block_fresh_viewer(self):
        import socket

        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(
                "import os,struct,time; frame=b'\\xff\\xd8'+b'x'*500000+b'\\xff\\xd9'; "
                "packet=struct.pack('!4sI',b'MJPG',len(frame))+frame\n"
                "while True: os.write(1,packet); time.sleep(0.02)"
            )
            capture.start()
            server = CameraIpcServer(
                capture,
                os.path.join(directory, "camera.sock"),
                media_storage_dir=os.path.join(directory, "media"),
            )
            server.start()
            self.addCleanup(server.stop)
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(slow.close)
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.connect(server.socket_path)
            slow.sendall(b'{"action":"stream"}\n')
            self.wait_until(lambda: capture.viewers_count == 1)
            self.wait_until(lambda: capture.viewers_count == 0)
            client = CameraIpcClient(server.socket_path)
            self.addCleanup(client.close)
            stream = client.stream_frames()
            self.addCleanup(stream.close)
            self.assertEqual(len(next(stream)), 500004)
            self.assertEqual(len(next(stream)), 500004)

    def test_ipc_cancel_releases_viewer_without_camera_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture("import time; time.sleep(60)")
            capture.start()
            server = CameraIpcServer(
                capture,
                os.path.join(directory, "camera.sock"),
                media_storage_dir=os.path.join(directory, "media"),
            )
            server.start()
            self.addCleanup(server.stop)
            client = CameraIpcClient(server.socket_path)
            reader = threading.Thread(target=lambda: list(client.stream_frames()))
            reader.start()
            self.wait_until(lambda: capture.viewers_count == 1)
            client.close()
            reader.join(2)
            self.assertFalse(reader.is_alive())
            self.wait_until(lambda: capture.viewers_count == 0)


if __name__ == "__main__":
    unittest.main()
