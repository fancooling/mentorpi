"""Server-owned MP4 recording with bounded encoder shutdown and checked publication."""

import json
import logging
import os
import re
import select
import subprocess
import threading
import time
import uuid
from typing import Any

from ubuntu_tank_protocol.constants import DEFAULT_CAMERA_FRESHNESS_TIMEOUT_SEC

from .media_storage import MediaManager

logger = logging.getLogger(__name__)


class VideoRecorder:
    """Keep one recording alive across browser disconnects; publish checked MP4 files."""

    def __init__(self, capture, media_manager: MediaManager):
        self.capture = capture
        self.media_manager = media_manager
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._ffmpeg_proc: subprocess.Popen | None = None

        self.recording_id: str | None = None
        self.start_time: float | None = None
        self._started_monotonic = 0.0
        self.file_path: str | None = None
        self.temp_path: str | None = None
        self.last_error: str | None = None
        self.finalizing = False
        self._recover_interrupted()

    @property
    def state(self) -> str:
        """Return recording/finalizing/error, or disabled when idle without an error."""
        with self._lock:
            if self.finalizing:
                return "finalizing"
            if self._running:
                return "recording"
            return "error" if self.last_error else "disabled"

    @property
    def elapsed_sec(self) -> float | None:
        """Elapsed recording time, immune to wall-clock adjustments; None when idle."""
        with self._lock:
            if self._running and self.start_time:
                return time.monotonic() - self._started_monotonic
            return None

    def start_recording(
        self, request_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Start one encoder or return the active/cached ID; failures carry details."""
        id_key = idempotency_key or request_id
        if id_key and self.media_manager.has_idempotent(id_key):
            cached = self.media_manager.get_idempotent(id_key)
            if cached:
                return {
                    "success": True,
                    "recording_id": cached.get("media_id"),
                    "state": "completed" if cached.get("completed") else "error",
                }

        with self._lock:
            if self._running or self.finalizing:
                return {
                    "success": True,
                    "recording_id": self.recording_id,
                    "state": "recording" if self._running else "finalizing",
                }

            # Check storage before starting
            avail = self.media_manager.get_available_storage_bytes()
            if avail <= 5 * 1024 * 1024:  # Need at least 5MB available space
                return {
                    "success": False,
                    "error": "LOW_STORAGE",
                    "detail": "Insufficient free space to start recording",
                }

            try:
                self.recording_id = uuid.uuid4().hex[:12]
                timestamp_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                filename = f"record_{timestamp_str}_{self.recording_id}.mp4"
                self.file_path = os.path.join(self.media_manager.media_dir, filename)
                self.temp_path = os.path.join(
                    self.media_manager.media_dir, f".tmp.{self.recording_id}.mp4"
                )

                # We start the subprocess
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-r",
                    str(self.capture.fps),
                    "-i",
                    "-",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "28",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                    self.temp_path,
                ]
                self._ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )

                os.set_blocking(self._ffmpeg_proc.stdin.fileno(), False)
                self._running = True
                self.finalizing = False
                self.start_time = time.time()
                self._started_monotonic = time.monotonic()
                self.last_error = None

                self._thread = threading.Thread(
                    target=self._record_loop,
                    args=(id_key,),
                    name="VideoRecorder",
                    daemon=True,
                )
                self._thread.start()

                return {
                    "success": True,
                    "recording_id": self.recording_id,
                    "state": "recording",
                }
            except (OSError, RuntimeError, ValueError) as exc:
                self.last_error = str(exc)
                self._running = False
                return {"success": False, "error": "START_FAILED", "detail": str(exc)}

    def stop_recording(self, recording_id: str) -> dict[str, Any]:
        """Request asynchronous finalization; an old ID cannot stop a new recording."""
        with self._lock:
            if not self._running and not self.finalizing:
                return {
                    "success": False,
                    "error": "NOT_RECORDING",
                    "detail": "No active recording",
                }
            if self.recording_id != recording_id:
                return {
                    "success": False,
                    "error": "CONFLICT",
                    "detail": "Active recording ID does not match",
                }
            self._running = False
            self.finalizing = True
            rec_id = self.recording_id

        return {"success": True, "recording_id": rec_id, "state": "finalizing"}

    def _record_loop(self, idempotency_key: str | None):
        last_ts = None
        last_frame_time = time.monotonic()
        self.capture.add_viewer()
        try:
            while self._running:
                avail = self.media_manager.get_available_storage_bytes()
                if avail <= 1024 * 1024:  # 1MB safe buffer
                    logger.warning("Low storage, stopping recording")
                    break

                # Enforce max recording duration (e.g. 5 minutes)
                if self.elapsed_sec and self.elapsed_sec > 300:
                    logger.warning("Max recording duration reached")
                    break

                frame, ts = self.capture.wait_for_new_frame(last_ts, timeout_sec=1.0)
                if not self._running:
                    break
                if frame and ts != last_ts:
                    last_ts = ts
                    last_frame_time = time.monotonic()
                    try:
                        self._write_frame(frame)
                    except (OSError, TimeoutError) as exc:
                        self.last_error = f"Recording encoder input failed: {exc}"
                        logger.error(self.last_error)
                        break
                elif (
                    time.monotonic() - last_frame_time
                    > DEFAULT_CAMERA_FRESHNESS_TIMEOUT_SEC
                ):
                    self.last_error = "Recording interrupted: camera frames stopped"
                    break
        finally:
            self.capture.remove_viewer()
            with self._lock:
                self._running = False
                self.finalizing = True
                rec_id = self.recording_id
                start_ts = self.start_time

            try:
                self._finish_encoder()
                self._publish_recording(rec_id, start_ts, idempotency_key)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.last_error = f"Recording finalization failed: {exc}"
                logger.error(self.last_error)

            with self._lock:
                self.finalizing = False
                self.recording_id = None
                self.start_time = None

    def _write_frame(self, frame: bytes) -> None:
        # Bound pipe backpressure so Stop and shutdown can always reach finalization.
        pending = memoryview(frame)
        deadline = time.monotonic() + 2.0
        fd = self._ffmpeg_proc.stdin.fileno()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("encoder stopped accepting frames")
            if not select.select([], [fd], [], min(remaining, 0.1))[1]:
                continue
            try:
                pending = pending[os.write(fd, pending) :]
            except BlockingIOError:
                continue

    def _finish_encoder(self) -> None:
        proc = self._ffmpeg_proc
        try:
            proc.stdin.close()
        except OSError as exc:
            self.last_error = f"Recording encoder input failed: {exc}"
        try:
            code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
            self.last_error = "Recording encoder finalization timed out"
            return
        if code != 0:
            self.last_error = f"Recording encoder exited with status {code}"

    def _publish_recording(self, rec_id, start_ts, idempotency_key) -> None:
        if not os.path.exists(self.temp_path) or os.path.getsize(self.temp_path) == 0:
            self.last_error = self.last_error or "Recording encoder produced no video"
            if os.path.exists(self.temp_path):
                os.unlink(self.temp_path)
            return

        # Probe an initial fragment, keeping verification bounded even for long clips.
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-read_intervals",
                "%+1",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_entries",
                "stream=codec_name,nb_read_packets",
                "-of",
                "json",
                self.temp_path,
            ],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        streams = (
            json.loads(probe.stdout).get("streams", []) if probe.returncode == 0 else []
        )
        valid = any(
            stream.get("codec_name") == "h264"
            and str(stream.get("nb_read_packets", "")).isdigit()
            and int(stream["nb_read_packets"]) > 0
            for stream in streams
        )
        if not valid:
            self.last_error = (
                self.last_error or "Recording encoder produced invalid video"
            )
            # Keep nonempty failed output for owner diagnosis, outside completed media.
            return

        completed = self.last_error is None
        if not completed:
            self.file_path = self.file_path.removesuffix(".mp4") + "_interrupted.mp4"
        os.replace(self.temp_path, self.file_path)
        item = {
            "media_id": rec_id,
            "id": rec_id,
            "type": "video",
            "filename": os.path.basename(self.file_path),
            "timestamp": time.time(),
            "started_at": start_ts,
            "url": f"/api/v1/camera/media/{rec_id}",
            "width": self.capture.width,
            "height": self.capture.height,
            "bytes": os.path.getsize(self.file_path),
            "completed": completed,
        }
        self.media_manager.register_recording(item, idempotency_key)

    def _recover_interrupted(self) -> None:
        # Reconcile this worker's orphaned files before accepting any new recording.
        pattern = re.compile(
            r"(?:\.tmp\.|record_\d{8}_\d{6}_)([0-9a-f]{12})(?:_interrupted)?\.mp4"
        )
        try:
            with os.scandir(self.media_manager.media_dir) as entries:
                paths = sorted(
                    entry.path for entry in entries if pattern.fullmatch(entry.name)
                )
        except FileNotFoundError:
            return
        for path in paths:
            rec_id = pattern.fullmatch(os.path.basename(path)).group(1)
            if self.media_manager.get_media(rec_id)[0] is not None:
                continue
            self.temp_path = path
            self.file_path = os.path.join(
                self.media_manager.media_dir,
                f"record_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{rec_id}.mp4",
            )
            self.last_error = (
                "Recovered interrupted recording after camera worker restart"
            )
            try:
                self._publish_recording(rec_id, os.path.getmtime(path), None)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.last_error = f"Recording recovery failed: {exc}"
                logger.error(self.last_error)
        self.temp_path = None
        self.file_path = None

    def shutdown(self):
        """Stop accepting frames and wait for bounded encoder finalization."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=16.0)
