"""
Non-ROS monotonic heartbeat utilities for process supervision.

Provides standardized framing, inherited file descriptor emission, dedicated
per-channel socket emission, and SCM_CREDENTIALS peer verification.
"""

import math
import os
import socket
import struct
import time
from typing import Optional, Tuple


def format_heartbeat(timestamp_monotonic: Optional[float] = None) -> bytes:
    """Format a monotonic timestamp payload as bytes."""
    ts = timestamp_monotonic if timestamp_monotonic is not None else time.monotonic()
    return f"{ts:.6f}\n".encode("utf-8")


def parse_timestamp(payload: bytes) -> Optional[float]:
    """Parse monotonic timestamp from raw bytes. Returns None if invalid."""
    try:
        text = payload.decode("utf-8").strip()
        ts = float(text)
        if math.isfinite(ts):
            return ts
        return None
    except Exception:
        return None


def send_fd_heartbeat(fd: int, timestamp_monotonic: Optional[float] = None) -> bool:
    """
    Write a heartbeat timestamp to an inherited file descriptor.
    File descriptors are isolated to the process table and cannot be written by unauthorized peers.
    """
    if fd < 0:
        return False
    try:
        data = format_heartbeat(timestamp_monotonic)
        os.write(fd, data)
        return True
    except Exception:
        return False


def send_socket_heartbeat(
    sock_path: str, timestamp_monotonic: Optional[float] = None
) -> bool:
    """Send a datagram heartbeat to a dedicated per-subsystem UNIX domain socket."""
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            payload = format_heartbeat(timestamp_monotonic)
            sock.sendto(payload, sock_path)
            return True
    except Exception:
        return False


def extract_credentials(
    ancillary_data,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Extract (pid, uid, gid) from SCM_CREDENTIALS socket ancillary data on Linux.
    Returns (None, None, None) if not present or malformed.
    """
    for cmsg_level, cmsg_type, cmsg_data in ancillary_data:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_CREDENTIALS:
            if len(cmsg_data) >= 12:
                pid, uid, gid = struct.unpack("iii", cmsg_data[:12])
                return pid, uid, gid
    return None, None, None
