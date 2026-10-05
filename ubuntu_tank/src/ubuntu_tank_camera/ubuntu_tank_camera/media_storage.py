"""Persistent media storage manager for MentorPi Pi 5 camera captures and recordings."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_CAMERA_HEIGHT,
    DEFAULT_CAMERA_MEDIA_DIR,
    DEFAULT_CAMERA_MIN_FREE_SPACE_BYTES,
    DEFAULT_CAMERA_STORAGE_QUOTA_BYTES,
    DEFAULT_CAMERA_WIDTH,
    ENV_CAMERA_MEDIA_DIR,
    ENV_CAMERA_MIN_FREE_SPACE_BYTES,
    ENV_CAMERA_STORAGE_QUOTA_BYTES,
)

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Exception raised when media storage operations or quota checks fail."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MediaManager:
    """Manages saved camera media files, disk quotas, and index persistence."""

    def __init__(
        self,
        media_dir: str | None = None,
        storage_quota_bytes: int | None = None,
        min_free_space_bytes: int | None = None,
        storage_dir: str | None = None,
        quota_bytes: int | None = None,
    ) -> None:
        target_dir = storage_dir if storage_dir is not None else media_dir
        if target_dir is None:
            target_dir = os.environ.get(ENV_CAMERA_MEDIA_DIR, DEFAULT_CAMERA_MEDIA_DIR)
        target_quota = quota_bytes if quota_bytes is not None else storage_quota_bytes
        if target_quota is None:
            raw_quota = os.environ.get(ENV_CAMERA_STORAGE_QUOTA_BYTES)
            target_quota = (
                int(raw_quota)
                if raw_quota and raw_quota.isdigit()
                else DEFAULT_CAMERA_STORAGE_QUOTA_BYTES
            )
        if min_free_space_bytes is None:
            raw_free = os.environ.get(ENV_CAMERA_MIN_FREE_SPACE_BYTES)
            min_free_space_bytes = (
                int(raw_free)
                if raw_free and raw_free.isdigit()
                else DEFAULT_CAMERA_MIN_FREE_SPACE_BYTES
            )

        self.media_dir = target_dir
        self.storage_dir = target_dir
        self.storage_quota_bytes = target_quota
        self.min_free_space_bytes = min_free_space_bytes
        self.index_path = os.path.join(self.media_dir, "index.json")

        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._idempotency_cache: dict[str, dict[str, Any]] = {}

        try:
            os.makedirs(self.media_dir, exist_ok=True)
        except OSError as exc:
            logger.debug(
                "Could not create media directory '%s': %s", self.media_dir, exc
            )

        self._load_index()

    def _load_index(self) -> None:
        """Load and sync persistent media index from disk."""
        with self._lock:
            loaded_entries: dict[str, dict[str, Any]] = {}
            if os.path.isfile(self.index_path):
                try:
                    with open(self.index_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and "id" in item:
                                loaded_entries[item["id"]] = item
                    elif isinstance(data, dict):
                        loaded_entries = data.get("items", {})
                except Exception as exc:
                    logger.warning(
                        "Failed to read media index at '%s': %s", self.index_path, exc
                    )

            # Reconcile files against disk
            valid_entries: dict[str, dict[str, Any]] = {}
            for media_id, item in loaded_entries.items():
                filename = item.get("filename")
                if filename and os.path.isfile(os.path.join(self.media_dir, filename)):
                    valid_entries[media_id] = item
                else:
                    logger.debug(
                        "Pruning missing media entry '%s' from index", media_id
                    )

            self._index = valid_entries
            self._save_index_locked()

    def _save_index_locked(self, *, strict: bool = False) -> None:
        """Persist current index atomically to disk while holding lock."""
        if not os.path.isdir(self.media_dir):
            if strict:
                raise FileNotFoundError(self.media_dir)
            return
        temp_path = os.path.join(self.media_dir, f".index.tmp.{uuid.uuid4().hex[:8]}")
        try:
            payload = list(self._index.values())
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.index_path)
        except OSError as exc:
            logger.warning(
                "Failed to atomically save index to '%s': %s", self.index_path, exc
            )
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if strict:
                raise

    def register_recording(
        self, item: dict[str, Any], idempotency_key: str | None = None
    ) -> None:
        """Publish video metadata only after durable index replacement.

        Raise OSError on persistence failure, leaving the file for startup recovery.
        Idempotency entries are installed only after the index is saved.
        """
        with self._lock:
            media_id = item["media_id"]
            previous = self._index.get(media_id)
            self._index[media_id] = item
            try:
                self._save_index_locked(strict=True)
            except OSError:
                if previous is None:
                    del self._index[media_id]
                else:
                    self._index[media_id] = previous
                raise
            if idempotency_key:
                self._idempotency_cache[idempotency_key] = item

    def get_total_used_bytes(self) -> int:
        """Count indexed media plus active and orphaned MP4s against the quota."""
        with self._lock:
            used = sum(item.get("bytes", 0) for item in self._index.values())
            indexed = {item["filename"] for item in self._index.values()}
            try:
                with os.scandir(self.media_dir) as entries:
                    for entry in entries:
                        if (
                            entry.name not in indexed
                            and entry.name.endswith(".mp4")
                            and entry.name.startswith((".tmp.", "record_"))
                        ):
                            try:
                                used += entry.stat().st_size
                            except FileNotFoundError:
                                continue
            except FileNotFoundError:
                pass
            return used

    def get_available_storage_bytes(self) -> int:
        """Return minimum of remaining quota and actual filesystem free space."""
        total_used = self.get_total_used_bytes()
        remaining_quota = max(0, self.storage_quota_bytes - total_used)

        try:
            usage = shutil.disk_usage(self.media_dir)
            disk_avail = max(0, usage.free - self.min_free_space_bytes)
            return min(remaining_quota, disk_avail)
        except Exception:
            return remaining_quota

    def has_idempotent(self, key: str) -> bool:
        """Check if an idempotency key or request_id has already been recorded."""
        with self._lock:
            return key in self._idempotency_cache

    def get_idempotent(self, key: str) -> dict[str, Any] | None:
        """Retrieve existing media item associated with idempotency key."""
        with self._lock:
            return self._idempotency_cache.get(key)

    def save_capture(
        self,
        frame: bytes,
        width: int = DEFAULT_CAMERA_WIDTH,
        height: int = DEFAULT_CAMERA_HEIGHT,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically persist a JPEG frame and update the persistent media index."""
        id_key = idempotency_key or request_id
        if id_key:
            with self._lock:
                if id_key in self._idempotency_cache:
                    cached = self._idempotency_cache[id_key]
                    media_id = cached.get("media_id") or cached.get("id")
                    if media_id and media_id in self._index:
                        return self._index[media_id]

        # Check free disk space
        try:
            usage = shutil.disk_usage(self.media_dir)
            if usage.free < self.min_free_space_bytes:
                raise StorageError(
                    "LOW_STORAGE",
                    f"Free disk space ({usage.free} bytes) is below minimum reserve ({self.min_free_space_bytes} bytes)",
                )
        except StorageError:
            raise
        except Exception:
            pass

        # Check storage quota
        frame_len = len(frame)
        total_used = self.get_total_used_bytes()
        if total_used + frame_len > self.storage_quota_bytes:
            raise StorageError(
                "STORAGE_QUOTA_EXCEEDED",
                f"Capture ({frame_len} bytes) would exceed storage quota ({self.storage_quota_bytes} bytes)",
            )

        media_id = uuid.uuid4().hex[:12]
        ts = time.time()
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp_str}_{media_id}.jpg"
        target_path = os.path.join(self.media_dir, filename)
        temp_path = os.path.join(self.media_dir, f".tmp.{media_id}")

        try:
            with open(temp_path, "wb") as f:
                f.write(frame)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target_path)
        except OSError as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise StorageError("WRITE_FAILED", f"Failed to persist JPEG frame: {exc}")

        item: dict[str, Any] = {
            "media_id": media_id,
            "id": media_id,
            "type": "image",
            "filename": filename,
            "timestamp": ts,
            "url": f"/api/v1/camera/media/{media_id}",
            "width": width,
            "height": height,
            "bytes": frame_len,
            "completed": True,
        }

        with self._lock:
            self._index[media_id] = item
            if id_key:
                self._idempotency_cache[id_key] = item
            self._save_index_locked()

        return item

    def list_media(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Return paginated saved media metadata sorted newest first."""
        with self._lock:
            # Reconcile on read
            valid_entries: dict[str, dict[str, Any]] = {}
            for media_id, item in self._index.items():
                filename = item.get("filename")
                if filename and os.path.isfile(os.path.join(self.media_dir, filename)):
                    valid_entries[media_id] = item
            if len(valid_entries) != len(self._index):
                self._index = valid_entries
                self._save_index_locked()

            sorted_items = sorted(
                self._index.values(),
                key=lambda x: x.get("timestamp", 0.0),
                reverse=True,
            )
            total = len(sorted_items)
            page = sorted_items[offset : offset + limit]
            return {
                "items": page,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_media(self, media_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Retrieve media item metadata and verified on-disk filepath by media ID."""
        with self._lock:
            item = self._index.get(media_id)
            if not item:
                return None, None
            filepath = os.path.join(self.media_dir, item["filename"])
            if not os.path.isfile(filepath):
                del self._index[media_id]
                self._save_index_locked()
                return None, None
            return item, filepath
