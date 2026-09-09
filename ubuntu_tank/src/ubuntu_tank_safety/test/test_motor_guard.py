"""
Unit tests for MotorGuard core safety logic.

Verifies:
- Disarmed-by-default initial state
- Explicit non-persisted arming
- Strict 4-motor ID {1, 2, 3, 4} validation
- NaN, Inf, and speed limit rejection
- 250 ms monotonic freshness timeout
- Immediate disarm and repeated zero on fault or timeout
"""

import math
import unittest
from ubuntu_tank_safety.motor_guard import MotorGuard


class TestMotorGuard(unittest.TestCase):

    def setUp(self):
        self.guard = MotorGuard(max_rps=2.0, timeout_sec=0.250)

    def test_starts_disarmed(self):
        """Invariant 2: Motor guard starts disarmed after every launch/restart."""
        self.assertFalse(self.guard.is_armed)

    def test_disarmed_drops_commands(self):
        """Commands must not be forwarded when disarmed."""
        valid_cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        cmd, fault, reason = self.guard.handle_command(valid_cmd, now_monotonic=100.0)
        self.assertIsNone(cmd)
        self.assertFalse(fault)
        self.assertFalse(self.guard.is_armed)

    def test_explicit_arm_and_disarm(self):
        """Invariant 3: Arming is explicit and never persisted."""
        success, msg = self.guard.arm()
        self.assertTrue(success)
        self.assertTrue(self.guard.is_armed)

        success, msg, zero_cmd = self.guard.disarm()
        self.assertTrue(success)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(zero_cmd, [(1, 0.0), (2, 0.0), (3, 0.0), (4, 0.0)])

    def test_valid_command_forwarded_when_armed(self):
        """Invariant 4: Complete valid command reaches output."""
        self.guard.arm()
        valid_cmd = [(1, 0.5), (2, -0.5), (3, 0.5), (4, -0.5)]
        cmd, fault, reason = self.guard.handle_command(valid_cmd, now_monotonic=10.0)
        self.assertFalse(fault)
        self.assertEqual(cmd, [(1, 0.5), (2, -0.5), (3, 0.5), (4, -0.5)])

    def test_rejects_missing_motor_ids(self):
        """Command missing motor 4 must be rejected, disarming the guard."""
        self.guard.arm()
        incomplete_cmd = [(1, 0.5), (2, 0.5), (3, 0.5)]
        cmd, fault, reason = self.guard.handle_command(incomplete_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(cmd, self.guard.get_zero_command())

    def test_rejects_duplicate_motor_ids(self):
        """Duplicate IDs must be rejected and cause immediate disarm."""
        self.guard.arm()
        dup_cmd = [(1, 0.5), (2, 0.5), (2, 0.5), (3, 0.5)]
        cmd, fault, reason = self.guard.handle_command(dup_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(cmd, self.guard.get_zero_command())

    def test_rejects_invalid_motor_ids(self):
        """Motor IDs outside {1, 2, 3, 4} must be rejected."""
        self.guard.arm()
        bad_id_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (5, 0.5)]
        cmd, fault, reason = self.guard.handle_command(bad_id_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_rejects_nan_and_inf(self):
        """NaN and +/-Inf RPS values must be rejected."""
        self.guard.arm()
        nan_cmd = [(1, float('nan')), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(nan_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

        self.guard.arm()
        inf_cmd = [(1, float('inf')), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(inf_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_rejects_rps_exceeding_max(self):
        """RPS exceeding max_rps must be rejected and cause disarm."""
        self.guard.arm()
        overspeed_cmd = [(1, 3.5), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(overspeed_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_timeout_disarms_and_emits_zero(self):
        """Invariant 5: Command older than 250 ms disarms and produces zero."""
        self.guard.arm()
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        self.guard.handle_command(valid_cmd, now_monotonic=100.0)

        # Within 200 ms: not timed out
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=100.200)
        self.assertFalse(timed_out)
        self.assertIsNone(zero_cmd)
        self.assertTrue(self.guard.is_armed)

        # After 251 ms: timed out!
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=100.251)
        self.assertTrue(timed_out)
        self.assertEqual(zero_cmd, self.guard.get_zero_command())
        self.assertFalse(self.guard.is_armed)

    def test_rearm_clears_stale_command(self):
        """Re-arming after timeout requires a new command and does not resume old command."""
        self.guard.arm()
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        self.guard.handle_command(valid_cmd, now_monotonic=10.0)
        self.guard.check_timeout(now_monotonic=10.30)
        self.assertFalse(self.guard.is_armed)

        # Re-arm
        self.guard.arm()
        self.assertTrue(self.guard.is_armed)
        # Checking timeout before any new command: must not trigger false timeout on stale command
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=20.0)
        self.assertFalse(timed_out)


if __name__ == '__main__':
    unittest.main()
