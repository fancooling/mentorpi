"""
Lock Hierarchy Specification and Non-blocking Concurrency Guards.

Defines and enforces the strict three-tier lock acquisition hierarchy:
  Level 1: DEPLOYMENT_LOCK  (/run/lock/ubuntu_tank/deploy.lock)
  Level 2: OPERATOR_LOCK    (/run/lock/ubuntu_tank/operator.lock)
  Level 3: LIFECYCLE_LOCK   (/run/lock/ubuntu_tank/lifecycle.lock)

Enforces non-blocking stop semantics: emergency stopping and disarming MUST
never wait behind long-running deployment operations or deadlocks.
"""

import errno
import fcntl
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager

from .constants import (
    LOCK_LEVEL_DEPLOYMENT,
    LOCK_LEVEL_LIFECYCLE,
    LOCK_LEVEL_OPERATOR,
    LOCK_PATH_DEPLOYMENT,
    LOCK_PATH_LIFECYCLE,
    LOCK_PATH_OPERATOR,
)

LEVEL_TO_PATH = {
    LOCK_LEVEL_DEPLOYMENT: LOCK_PATH_DEPLOYMENT,
    LOCK_LEVEL_OPERATOR: LOCK_PATH_OPERATOR,
    LOCK_LEVEL_LIFECYCLE: LOCK_PATH_LIFECYCLE,
}


class LockOrderViolationError(RuntimeError):
    """Raised when a lock is requested in violation of the strict hierarchy order."""


class LockAcquisitionTimeoutError(TimeoutError):
    """Raised when a bounded lock acquisition times out."""


class LockHierarchy:
    """Manager verifying strict lock acquisition order and providing bounded locks."""

    def __init__(self) -> None:
        self.currently_held_levels: list[int] = []

    @contextmanager
    def acquire(
        self,
        level: int,
        lock_path: str | None = None,
        timeout_sec: float | None = None,
        non_blocking: bool = False,
    ) -> Generator[int, None, None]:
        """Acquire a lock at the specified hierarchy level with ordering verification.

        Args:
            level: Integer hierarchy level (1: Deployment, 2: Operator, 3:
              Lifecycle).
            lock_path: Optional override for lock file path (e.g. for testing).
            timeout_sec: Maximum seconds to wait for lock acquisition.
            non_blocking: If True, fail immediately if lock is unavailable.
        """
        # Verify strict hierarchy ordering: requested level must be > highest currently held level
        if self.currently_held_levels:
            highest_held = max(self.currently_held_levels)
            if level <= highest_held:
                raise LockOrderViolationError(
                    f"Lock hierarchy violation: cannot acquire Level {level} while"
                    f" holding Level {highest_held}. Locks must be acquired in strictly"
                    " increasing order (1 -> 2 -> 3)."
                )

        path = lock_path or LEVEL_TO_PATH.get(level)
        if not path:
            raise ValueError(f"Unknown lock level: {level}")

        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

        start_time = time.monotonic()
        acquired = False
        try:
            while True:
                try:
                    flags = fcntl.LOCK_EX | fcntl.LOCK_NB
                    fcntl.flock(fd, flags)
                    acquired = True
                    break
                except (BlockingIOError, OSError) as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if non_blocking:
                        raise LockAcquisitionTimeoutError(
                            f"Lock at Level {level} ({path}) is busy (non-blocking"
                            " acquisition requested)"
                        )
                    if (
                        timeout_sec is not None
                        and (time.monotonic() - start_time) >= timeout_sec
                    ):
                        raise LockAcquisitionTimeoutError(
                            f"Timed out after {timeout_sec:.2f}s acquiring lock at Level"
                            f" {level} ({path})"
                        )
                    time.sleep(0.010)

            self.currently_held_levels.append(level)
            yield fd

        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                if level in self.currently_held_levels:
                    self.currently_held_levels.remove(level)
            try:
                os.close(fd)
            except OSError:
                pass


def execute_non_blocking_stop(
    operator_lock_path: str = LOCK_PATH_OPERATOR,
    stop_callback: Callable[[], None] | None = None,
    timeout_sec: float = 0.050,
) -> bool:
    """Execute emergency stop with guaranteed non-blocking bounded latency.

    Stop must never wait behind long-running deployment operations.
    """
    # Attempt quick bounded acquisition of operator lock
    try:
        hierarchy = LockHierarchy()
        with hierarchy.acquire(
            LOCK_LEVEL_OPERATOR,
            lock_path=operator_lock_path,
            timeout_sec=timeout_sec,
        ):
            if stop_callback:
                stop_callback()
            return True
    except (LockAcquisitionTimeoutError, OSError):
        # Even if operator lock is contended, immediately invoke stop_callback directly
        if stop_callback:
            stop_callback()
        return False
