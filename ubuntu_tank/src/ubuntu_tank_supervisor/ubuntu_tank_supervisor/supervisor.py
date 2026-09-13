"""
Trusted AND-gating supervisor logic.

Ensures systemd watchdog notifications are emitted only when both guard and bridge
subsystems are actively beating within their respective monotonic deadlines, and
strictly validates peer credentials on every heartbeat event.
"""

import os
import socket
from typing import Dict, Optional, Tuple


class Supervisor:
    """Core AND-gating health supervisor with credential verification."""

    def __init__(
        self,
        guard_deadline_sec: float = 1.0,
        bridge_deadline_sec: float = 1.0,
        expected_guard_pid: Optional[int] = None,
        expected_bridge_pid: Optional[int] = None,
        expected_uid: Optional[int] = None,
        notify_socket_path: Optional[str] = None,
    ):
        self.guard_deadline_sec = float(guard_deadline_sec)
        self.bridge_deadline_sec = float(bridge_deadline_sec)
        self.expected_guard_pid = expected_guard_pid
        self.expected_bridge_pid = expected_bridge_pid
        self.expected_uid = expected_uid
        self.notify_socket_path = notify_socket_path or os.environ.get("NOTIFY_SOCKET")

        # Monotonic timestamps of last received validated heartbeats
        self._last_heartbeats: Dict[str, Optional[float]] = {
            "guard": None,
            "bridge": None,
        }

    def set_expected_pid(self, sender: str, pid: int) -> None:
        """Set or update expected PID for a subsystem."""
        if sender == "guard":
            self.expected_guard_pid = pid
        elif sender == "bridge":
            self.expected_bridge_pid = pid

    def record_heartbeat(
        self,
        sender: str,
        timestamp_monotonic: float,
        peer_pid: Optional[int] = None,
        peer_uid: Optional[int] = None,
        is_trusted_channel: bool = False,
    ) -> Tuple[bool, str]:
        """
        Record a heartbeat for a specific subsystem ('guard' or 'bridge').
        Validates caller credentials strictly on socket channels.
        Inherited kernel pipes are trusted channels isolated in the process table.
        """
        if sender not in self._last_heartbeats:
            return False, f"Unknown sender subsystem: {sender}"

        if not is_trusted_channel:
            # Socket channels require strict Linux SCM_CREDENTIALS verification
            if peer_uid is None:
                return (
                    False,
                    f"Credential rejection: missing peer UID in socket datagram for {sender}",
                )
            if self.expected_uid is not None and peer_uid != self.expected_uid:
                return (
                    False,
                    f"Credential rejection: UID {peer_uid} does not match expected {self.expected_uid}",
                )

            if peer_pid is None:
                return (
                    False,
                    f"Credential rejection: missing peer PID in socket datagram for {sender}",
                )

            if sender == "guard":
                if self.expected_guard_pid is None:
                    return (
                        False,
                        "Credential rejection: expected guard PID is not configured for socket heartbeat",
                    )
                if peer_pid != self.expected_guard_pid:
                    return (
                        False,
                        f"Credential rejection: PID {peer_pid} does not match expected guard PID {self.expected_guard_pid}",
                    )
            elif sender == "bridge":
                if self.expected_bridge_pid is None:
                    return (
                        False,
                        "Credential rejection: expected bridge PID is not configured for socket heartbeat",
                    )
                if peer_pid != self.expected_bridge_pid:
                    return (
                        False,
                        f"Credential rejection: PID {peer_pid} does not match expected bridge PID {self.expected_bridge_pid}",
                    )

        self._last_heartbeats[sender] = timestamp_monotonic
        return True, f"Heartbeat accepted for {sender}"

    def check_health(self, now_monotonic: float) -> Tuple[bool, str]:
        """
        Evaluate AND-gated health.

        Both guard and bridge must have delivered a validated heartbeat within their
        respective deadline intervals.
        """
        guard_last = self._last_heartbeats["guard"]
        bridge_last = self._last_heartbeats["bridge"]

        if guard_last is None:
            return False, "Guard heartbeat has never been received"
        if bridge_last is None:
            return False, "Bridge heartbeat has never been received"

        guard_age = now_monotonic - guard_last
        bridge_age = now_monotonic - bridge_last

        if (
            guard_age > self.guard_deadline_sec
            and bridge_age > self.bridge_deadline_sec
        ):
            return (
                False,
                f"Both guard ({guard_age:.3f}s) and bridge ({bridge_age:.3f}s) heartbeats are stale",
            )
        if guard_age > self.guard_deadline_sec:
            return (
                False,
                f"Guard heartbeat is stale: age {guard_age:.3f}s > deadline {self.guard_deadline_sec}s",
            )
        if bridge_age > self.bridge_deadline_sec:
            return (
                False,
                f"Bridge heartbeat is stale: age {bridge_age:.3f}s > deadline {self.bridge_deadline_sec}s",
            )

        return True, "Both guard and bridge heartbeats are fresh"

    def send_systemd_notification(self, state: str = "WATCHDOG=1") -> bool:
        """
        Send a notification string to systemd via $NOTIFY_SOCKET.
        Supports standard systemd notify protocol (UNIX datagram, '@' for abstract).
        """
        if not self.notify_socket_path:
            return False

        sock_path = self.notify_socket_path
        if sock_path.startswith("@"):
            sock_path = "\0" + sock_path[1:]

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(state.encode("utf-8"), sock_path)
                return True
        except Exception:
            return False
