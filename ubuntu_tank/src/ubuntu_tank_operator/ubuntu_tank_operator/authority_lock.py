"""Shared filesystem lock for exclusive MentorPi command authority.

The operator daemon and every explicitly selected direct ROS client acquire the
same non-blocking advisory lock before constructing command publishers.  The
helper also establishes stable group ownership and mode for newly created lock
files so a human direct session cannot strand a file the service account cannot
open later.
"""

from __future__ import annotations

import fcntl
import grp
import os
import stat

from ubuntu_tank_protocol.constants import (
    DEFAULT_OPERATOR_SOCKET_PATH,
    ENV_OPERATOR_LOCK_PATH,
    ENV_OPERATOR_SOCKET_PATH,
)

OPERATOR_GROUP = "ubuntu-tank-operators"
OPERATOR_LOCK_MODE = 0o660


def resolve_authority_lock_path(
    socket_path: str | None = None, lock_path: str | None = None
) -> str:
    """Return the absolute shared authority-lock path.

    An explicit ``lock_path`` takes precedence, followed by
    ``UBUNTU_TANK_OPERATOR_LOCK``.  Otherwise the lock is placed beside the
    resolved operator socket with the same basename and a ``.lock`` suffix.
    """
    configured_lock = lock_path or os.environ.get(ENV_OPERATOR_LOCK_PATH)
    if configured_lock:
        return os.path.abspath(configured_lock)
    configured_socket = socket_path or os.environ.get(
        ENV_OPERATOR_SOCKET_PATH, DEFAULT_OPERATOR_SOCKET_PATH
    )
    return os.path.splitext(os.path.abspath(configured_socket))[0] + ".lock"


def acquire_authority_lock(
    socket_path: str | None = None, lock_path: str | None = None
) -> tuple[int, str]:
    """Create, normalize, and exclusively acquire the operator authority lock.

    Returns the open descriptor and resolved path.  Acquisition is non-blocking;
    an existing command producer raises ``RuntimeError``.  Permission failures
    are propagated because proceeding without a stable cross-user lock would
    permit multiple ROS command authorities.
    """
    resolved_path = resolve_authority_lock_path(socket_path, lock_path)
    os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
    open_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    fd = os.open(resolved_path, open_flags, OPERATOR_LOCK_MODE)
    try:
        target_gid = os.getgid()
        try:
            target_gid = grp.getgrnam(OPERATOR_GROUP).gr_gid
        except KeyError:
            # Development workstations need not provision the installed role.
            pass

        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                f"Operator authority lock '{resolved_path}' is not a regular file"
            )
        if metadata.st_gid != target_gid:
            os.fchown(fd, -1, target_gid)
        if stat.S_IMODE(os.fstat(fd).st_mode) != OPERATOR_LOCK_MODE:
            os.fchmod(fd, OPERATOR_LOCK_MODE)

        metadata = os.fstat(fd)
        if (
            metadata.st_gid != target_gid
            or stat.S_IMODE(metadata.st_mode) != OPERATOR_LOCK_MODE
        ):
            raise PermissionError(
                f"Operator authority lock '{resolved_path}' must be group-owned "
                f"with mode {OPERATOR_LOCK_MODE:o}"
            )

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise RuntimeError(
                f"Operator authority lock at '{resolved_path}' is held by another process: {exc}"
            ) from exc
        return fd, resolved_path
    except Exception:
        os.close(fd)
        raise


def release_authority_lock(fd: int | None) -> None:
    """Release and close an authority-lock descriptor, ignoring close races."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
