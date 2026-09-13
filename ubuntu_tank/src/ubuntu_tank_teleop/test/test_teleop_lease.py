"""
Unit tests for TeleopLeaseManager.

Verifies:
- W/A/S/D key mapping to four tank motions
- Lease renewal on key press
- Lease expiry (> 150 ms) returning zero velocity
- Immediate stop via space bar
- Rejection of unsafe lease durations >= 250 ms
"""

import unittest
from ubuntu_tank_teleop.lease import TeleopLeaseManager


class TestTeleopLease(unittest.TestCase):
    def setUp(self):
        self.mgr = TeleopLeaseManager(
            linear_vel=0.2, angular_vel=0.5, lease_duration_sec=0.150
        )

    def test_initial_state_zero(self):
        """No key pressed yet: velocity must be zero."""
        lin, ang = self.mgr.get_velocities(now_monotonic=10.0)
        self.assertEqual(lin, 0.0)
        self.assertEqual(ang, 0.0)

    def test_forward_key(self):
        """'w' key commands forward linear velocity within lease."""
        lin, ang = self.mgr.process_key("w", now_monotonic=10.0)
        self.assertAlmostEqual(lin, 0.2)
        self.assertEqual(ang, 0.0)

        # Within lease (100 ms later)
        lin, ang = self.mgr.get_velocities(now_monotonic=10.100)
        self.assertAlmostEqual(lin, 0.2)
        self.assertEqual(ang, 0.0)

        # After lease (160 ms later)
        lin, ang = self.mgr.get_velocities(now_monotonic=10.160)
        self.assertEqual(lin, 0.0)
        self.assertEqual(ang, 0.0)

    def test_reverse_key(self):
        """'s' key commands reverse velocity."""
        lin, ang = self.mgr.process_key("s", now_monotonic=10.0)
        self.assertAlmostEqual(lin, -0.2)
        self.assertEqual(ang, 0.0)

    def test_turn_left_key(self):
        """'a' key commands positive angular velocity."""
        lin, ang = self.mgr.process_key("a", now_monotonic=10.0)
        self.assertEqual(lin, 0.0)
        self.assertAlmostEqual(ang, 0.5)

    def test_turn_right_key(self):
        """'d' key commands negative angular velocity."""
        lin, ang = self.mgr.process_key("d", now_monotonic=10.0)
        self.assertEqual(lin, 0.0)
        self.assertAlmostEqual(ang, -0.5)

    def test_space_bar_immediate_stop(self):
        """Space bar cancels lease immediately."""
        self.mgr.process_key("w", now_monotonic=10.0)
        self.mgr.process_key(" ", now_monotonic=10.050)
        lin, ang = self.mgr.get_velocities(now_monotonic=10.051)
        self.assertEqual(lin, 0.0)
        self.assertEqual(ang, 0.0)

    def test_reject_unsafe_lease_duration(self):
        """Lease duration >= 250 ms must be rejected during construction."""
        with self.assertRaises(ValueError):
            TeleopLeaseManager(lease_duration_sec=0.250)
        with self.assertRaises(ValueError):
            TeleopLeaseManager(lease_duration_sec=0.300)

    def test_reject_non_positive_or_non_finite_lease_duration(self):
        """Lease duration <= 0, NaN, or infinite must be rejected."""
        for bad_val in [0.0, -0.150, float("nan"), float("inf"), float("-inf")]:
            with self.assertRaises(ValueError):
                TeleopLeaseManager(lease_duration_sec=bad_val)

    def test_reject_invalid_velocities(self):
        """Velocities <= 0, NaN, or infinite must be rejected."""
        for bad_val in [0.0, -0.5, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                TeleopLeaseManager(linear_vel=bad_val)
            with self.assertRaises(ValueError):
                TeleopLeaseManager(angular_vel=bad_val)

    def test_key_repeat_extends_lease(self):
        """Simulated key repeat (multiple 'w' inputs) extends deadline."""
        self.mgr.process_key("w", now_monotonic=10.0)
        # Repeat key at 100 ms intervals
        self.mgr.process_key("w", now_monotonic=10.100)
        self.mgr.process_key("w", now_monotonic=10.200)

        # At 10.300s (100 ms after last key): still within lease
        lin, ang = self.mgr.get_velocities(now_monotonic=10.300)
        self.assertAlmostEqual(lin, 0.2)

        # At 10.360s (160 ms after last key): expired
        lin, ang = self.mgr.get_velocities(now_monotonic=10.360)
        self.assertEqual(lin, 0.0)


if __name__ == "__main__":
    unittest.main()
