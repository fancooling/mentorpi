"""
Unit tests for Supervisor AND-gating, inherited FD, and credential verification logic.

Verifies:
- Both children must beat within deadline to achieve healthy status (AND-gating).
- One healthy child NEVER masks another child's hang.
- Strict monotonic deadline expiry.
- Inherited pipe / FD heartbeat transmission and parsing.
- Peer credential verification (PID/UID) preventing unauthorized impersonation.
- Fail-closed behavior on missed heartbeat or credential rejection.
"""

import os
import socket
import struct
import unittest
from ubuntu_tank_supervisor.heartbeat import (
    format_heartbeat,
    parse_timestamp,
    send_fd_heartbeat,
    extract_credentials,
)
from ubuntu_tank_supervisor.supervisor import Supervisor


class TestHeartbeatPayload(unittest.TestCase):
    def test_format_and_parse_valid(self):
        payload = format_heartbeat(123.456789)
        self.assertEqual(payload, b"123.456789\n")
        ts = parse_timestamp(payload)
        self.assertAlmostEqual(ts, 123.456789, places=5)

    def test_parse_malformed(self):
        self.assertIsNone(parse_timestamp(b"invalid"))
        self.assertIsNone(parse_timestamp(b""))
        self.assertIsNone(parse_timestamp(b"nan\n"))
        self.assertIsNone(parse_timestamp(b"inf\n"))
        self.assertIsNone(parse_timestamp(b"-inf\n"))

    def test_send_fd_heartbeat(self):
        """Test heartbeat delivery over an isolated, unforgeable kernel pipe."""
        r_fd, w_fd = os.pipe()
        try:
            success = send_fd_heartbeat(w_fd, 999.123456)
            self.assertTrue(success)
            data = os.read(r_fd, 64)
            ts = parse_timestamp(data)
            self.assertAlmostEqual(ts, 999.123456, places=5)
        finally:
            os.close(r_fd)
            os.close(w_fd)


class TestCredentialExtraction(unittest.TestCase):
    def test_extract_credentials(self):
        cmsg_data = struct.pack("iii", 1234, 1000, 1000)
        ancdata = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, cmsg_data)]
        pid, uid, gid = extract_credentials(ancdata)
        self.assertEqual(pid, 1234)
        self.assertEqual(uid, 1000)
        self.assertEqual(gid, 1000)

    def test_extract_credentials_empty(self):
        pid, uid, gid = extract_credentials([])
        self.assertIsNone(pid)
        self.assertIsNone(uid)
        self.assertIsNone(gid)


class TestSupervisorCredentialVerification(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(
            guard_deadline_sec=1.0,
            bridge_deadline_sec=1.0,
            expected_guard_pid=2001,
            expected_bridge_pid=2002,
            expected_uid=1000,
        )

    def test_accepts_matching_credentials(self):
        ok, msg = self.supervisor.record_heartbeat(
            "guard", 10.0, peer_pid=2001, peer_uid=1000
        )
        self.assertTrue(ok)
        ok, msg = self.supervisor.record_heartbeat(
            "bridge", 10.0, peer_pid=2002, peer_uid=1000
        )
        self.assertTrue(ok)

    def test_rejects_mismatched_uid(self):
        """Rejects senders not matching expected service UID."""
        ok, msg = self.supervisor.record_heartbeat(
            "guard", 10.0, peer_pid=2001, peer_uid=1001
        )
        self.assertFalse(ok)
        self.assertIn("UID 1001 does not match", msg)

    def test_rejects_mismatched_pid(self):
        """Rejects impersonator attempting to send on behalf of guard."""
        ok, msg = self.supervisor.record_heartbeat(
            "guard", 10.0, peer_pid=9999, peer_uid=1000
        )
        self.assertFalse(ok)
        self.assertIn("PID 9999 does not match", msg)

    def test_rejects_missing_uid(self):
        """Rejects socket heartbeats lacking peer UID."""
        ok, msg = self.supervisor.record_heartbeat(
            "guard", 10.0, peer_pid=2001, peer_uid=None
        )
        self.assertFalse(ok)
        self.assertIn("missing peer UID", msg)

    def test_rejects_missing_pid(self):
        """Rejects socket heartbeats lacking peer PID."""
        ok, msg = self.supervisor.record_heartbeat(
            "guard", 10.0, peer_pid=None, peer_uid=1000
        )
        self.assertFalse(ok)
        self.assertIn("missing peer PID", msg)

    def test_rejects_unconfigured_expected_pid(self):
        """Rejects socket heartbeats when supervisor lacks expected child PID (fail-closed)."""
        unconfigured_sup = Supervisor(expected_uid=1000)
        ok, msg = unconfigured_sup.record_heartbeat(
            "guard", 10.0, peer_pid=2001, peer_uid=1000
        )
        self.assertFalse(ok)
        self.assertIn("expected guard PID is not configured", msg)

    def test_trusted_channel_bypasses_socket_creds(self):
        """Inherited kernel pipes are trusted by process table isolation."""
        unconfigured_sup = Supervisor()
        ok, msg = unconfigured_sup.record_heartbeat(
            "guard", 10.0, is_trusted_channel=True
        )
        self.assertTrue(ok)
        self.assertIn("Heartbeat accepted", msg)

    def test_rejects_unknown_subsystem(self):
        ok, msg = self.supervisor.record_heartbeat(
            "rogue", 10.0, is_trusted_channel=True
        )
        self.assertFalse(ok)
        self.assertIn("Unknown sender", msg)


class TestSupervisorAndGating(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(guard_deadline_sec=1.0, bridge_deadline_sec=1.0)

    def test_initial_state_unhealthy(self):
        healthy, reason = self.supervisor.check_health(now_monotonic=10.0)
        self.assertFalse(healthy)
        self.assertIn("never been received", reason)

    def test_guard_only_unhealthy(self):
        self.supervisor.record_heartbeat("guard", 10.0, is_trusted_channel=True)
        healthy, reason = self.supervisor.check_health(now_monotonic=10.1)
        self.assertFalse(healthy)
        self.assertIn("Bridge", reason)

    def test_bridge_only_unhealthy(self):
        self.supervisor.record_heartbeat("bridge", 10.0, is_trusted_channel=True)
        healthy, reason = self.supervisor.check_health(now_monotonic=10.1)
        self.assertFalse(healthy)
        self.assertIn("Guard", reason)

    def test_both_fresh_healthy(self):
        self.supervisor.record_heartbeat("guard", 10.0, is_trusted_channel=True)
        self.supervisor.record_heartbeat("bridge", 10.2, is_trusted_channel=True)
        healthy, reason = self.supervisor.check_health(now_monotonic=10.5)
        self.assertTrue(healthy)
        self.assertIn("fresh", reason)

    def test_bridge_hang_fails_closed(self):
        """Invariant: Guard beating cannot mask a hung bridge."""
        self.supervisor.record_heartbeat("guard", 10.0, is_trusted_channel=True)
        self.supervisor.record_heartbeat("bridge", 10.0, is_trusted_channel=True)

        # Bridge stops beating, guard continues beating at 11.2
        self.supervisor.record_heartbeat("guard", 11.2, is_trusted_channel=True)
        # At 11.3: bridge age is 1.3s (> 1.0s deadline)
        healthy, reason = self.supervisor.check_health(now_monotonic=11.3)
        self.assertFalse(healthy)
        self.assertIn("Bridge heartbeat is stale", reason)

    def test_guard_hang_fails_closed(self):
        """Invariant: Bridge beating cannot mask a hung guard."""
        self.supervisor.record_heartbeat("guard", 10.0, is_trusted_channel=True)
        self.supervisor.record_heartbeat("bridge", 10.0, is_trusted_channel=True)

        # Guard stops beating, bridge continues beating at 11.2
        self.supervisor.record_heartbeat("bridge", 11.2, is_trusted_channel=True)
        # At 11.3: guard age is 1.3s (> 1.0s deadline)
        healthy, reason = self.supervisor.check_health(now_monotonic=11.3)
        self.assertFalse(healthy)
        self.assertIn("Guard heartbeat is stale", reason)


if __name__ == "__main__":
    unittest.main()
