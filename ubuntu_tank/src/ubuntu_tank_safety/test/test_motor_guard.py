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
        self.guard.arm(now_monotonic=10.0)
        valid_cmd = [(1, 0.5), (2, -0.5), (3, 0.5), (4, -0.5)]
        cmd, fault, reason = self.guard.handle_command(valid_cmd, now_monotonic=10.0)
        self.assertFalse(fault)
        self.assertEqual(cmd, [(1, 0.5), (2, -0.5), (3, 0.5), (4, -0.5)])

    def test_rejects_missing_motor_ids(self):
        """Command missing motor 4 must be rejected, disarming the guard."""
        self.guard.arm(now_monotonic=10.0)
        incomplete_cmd = [(1, 0.5), (2, 0.5), (3, 0.5)]
        cmd, fault, reason = self.guard.handle_command(
            incomplete_cmd, now_monotonic=10.0
        )
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(cmd, self.guard.get_zero_command())

    def test_rejects_duplicate_motor_ids(self):
        """Duplicate IDs must be rejected and cause immediate disarm."""
        self.guard.arm(now_monotonic=10.0)
        dup_cmd = [(1, 0.5), (2, 0.5), (2, 0.5), (3, 0.5)]
        cmd, fault, reason = self.guard.handle_command(dup_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(cmd, self.guard.get_zero_command())

    def test_rejects_invalid_motor_ids(self):
        """Motor IDs outside {1, 2, 3, 4} must be rejected."""
        self.guard.arm(now_monotonic=10.0)
        bad_id_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (5, 0.5)]
        cmd, fault, reason = self.guard.handle_command(bad_id_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_rejects_nan_and_inf(self):
        """NaN and +/-Inf RPS values must be rejected."""
        self.guard.arm(now_monotonic=10.0)
        nan_cmd = [(1, float("nan")), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(nan_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

        self.guard.arm(now_monotonic=10.0)
        inf_cmd = [(1, float("inf")), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(inf_cmd, now_monotonic=10.0)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_rejects_rps_exceeding_max(self):
        """RPS exceeding max_rps must be rejected and cause disarm."""
        self.guard.arm(now_monotonic=10.0)
        overspeed_cmd = [(1, 3.5), (2, 0.0), (3, 0.0), (4, 0.0)]
        cmd, fault, reason = self.guard.handle_command(
            overspeed_cmd, now_monotonic=10.0
        )
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)

    def test_timeout_disarms_and_emits_zero(self):
        """Invariant 5: Command older than 250 ms disarms and produces zero."""
        self.guard.arm(now_monotonic=100.0)
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

    def test_first_command_deadline_expires(self):
        """Milestone 8: Arming starts monotonic first-command deadline that expires if no command arrives."""
        self.guard.arm(now_monotonic=50.0)
        self.assertTrue(self.guard.is_armed)

        # Within 200 ms: waiting for command, not yet timed out
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=50.200)
        self.assertFalse(timed_out)
        self.assertIsNone(zero_cmd)
        self.assertTrue(self.guard.is_armed)

        # After 251 ms: first-command deadline expired, must disarm and emit zero!
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=50.251)
        self.assertTrue(timed_out)
        self.assertEqual(zero_cmd, self.guard.get_zero_command())
        self.assertFalse(self.guard.is_armed)

    def test_delayed_first_command_rejected_and_disarms(self):
        """A command arriving after the first-command deadline cannot resurrect a lease."""
        self.guard.arm(now_monotonic=50.0)
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]

        # Command arrives late (> 250 ms after arm)
        cmd, fault, reason = self.guard.handle_command(valid_cmd, now_monotonic=50.251)
        self.assertTrue(fault)
        self.assertFalse(self.guard.is_armed)
        self.assertEqual(cmd, self.guard.get_zero_command())
        self.assertIn("First-command deadline expired", reason)

    def test_repeated_arm_does_not_renew_deadline_or_lease(self):
        """Repeated arm requests while already armed must not silently renew deadline or lease."""
        # 1. First-command window: repeated arm does not extend deadline
        self.guard.arm(now_monotonic=50.0)
        # Call arm again at 50.200
        ok, msg = self.guard.arm(now_monotonic=50.200)
        self.assertTrue(ok)
        self.assertIn("already armed", msg)

        # Deadline still expires relative to original arm (50.0 + 0.251 = 50.251)
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=50.251)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

        # 2. Active motion lease: repeated arm does not extend command lease
        self.guard.arm(now_monotonic=100.0)
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        self.guard.handle_command(valid_cmd, now_monotonic=100.050)

        # Repeated arm at 100.200
        self.guard.arm(now_monotonic=100.200)

        # Lease expires relative to last command (100.050 + 0.251 = 100.301)
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=100.302)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

    def test_rearm_clears_stale_command_and_starts_fresh_deadline(self):
        """Re-arming after timeout requires a new command and enforces a new first-command deadline."""
        self.guard.arm(now_monotonic=10.0)
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        self.guard.handle_command(valid_cmd, now_monotonic=10.0)
        self.guard.check_timeout(now_monotonic=10.30)
        self.assertFalse(self.guard.is_armed)

        # Re-arm at 20.0
        self.guard.arm(now_monotonic=20.0)
        self.assertTrue(self.guard.is_armed)

        # Within deadline (20.100) -> not timed out
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=20.100)
        self.assertFalse(timed_out)

        # At 20.260 without new command -> first-command deadline expires!
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=20.260)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

    def test_negative_time_jump_disarms(self):
        """Negative monotonic time jump must fault immediately and disarm."""
        self.guard.arm(now_monotonic=100.0)
        valid_cmd = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        self.guard.handle_command(valid_cmd, now_monotonic=100.0)

        # Time moves backwards
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=99.0)
        self.assertTrue(timed_out)
        self.assertEqual(zero_cmd, self.guard.get_zero_command())
        self.assertFalse(self.guard.is_armed)


if __name__ == "__main__":
    unittest.main()
