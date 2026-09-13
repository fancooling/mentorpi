"""
Renewable motion lease manager for safe keyboard teleoperation.

Replaces vendor latched movement with a short lease (< 250 ms) that requires continuous
fresh input and automatically drops to zero after key repeat ceases or input is lost,
within the configured lease (or on pause, terminal loss, or cancellation).
"""

import math
from typing import Optional, Tuple


class TeleopLeaseManager:
    """Manages lease duration and converts key strokes to bounded velocity commands."""

    def __init__(
        self,
        linear_vel: float = 0.2,
        angular_vel: float = 0.5,
        lease_duration_sec: float = 0.150,
    ):
        try:
            linear_v = float(linear_vel)
            angular_v = float(angular_vel)
            lease_sec = float(lease_duration_sec)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Velocities and lease duration must be valid numbers: {e}"
            )

        if not (math.isfinite(lease_sec) and 0.0 < lease_sec < 0.250):
            raise ValueError(
                f"Lease duration {lease_duration_sec}s must be strictly positive and less than 250 ms motor guard timeout"
            )

        if not (math.isfinite(linear_v) and linear_v > 0.0):
            raise ValueError(
                f"linear_vel {linear_vel} must be a positive finite number"
            )

        if not (math.isfinite(angular_v) and angular_v > 0.0):
            raise ValueError(
                f"angular_vel {angular_vel} must be a positive finite number"
            )

        self.linear_vel = linear_v
        self.angular_vel = angular_v
        self.lease_duration_sec = lease_sec

        self._target_linear: float = 0.0
        self._target_angular: float = 0.0
        self._lease_deadline: Optional[float] = None

    @property
    def is_active(self) -> bool:
        """Return True if lease has not expired."""
        return self._lease_deadline is not None

    def process_key(self, key: str, now_monotonic: float) -> Tuple[float, float]:
        """
        Process a key press, update target velocities, and renew the lease deadline.
        Returns the resulting (linear_x, angular_z).
        """
        k = key.lower()
        if k == "w":
            self._target_linear = self.linear_vel
            self._target_angular = 0.0
            self._lease_deadline = now_monotonic + self.lease_duration_sec
        elif k == "s":
            self._target_linear = -self.linear_vel
            self._target_angular = 0.0
            self._lease_deadline = now_monotonic + self.lease_duration_sec
        elif k == "a":
            self._target_linear = 0.0
            self._target_angular = self.angular_vel
            self._lease_deadline = now_monotonic + self.lease_duration_sec
        elif k == "d":
            self._target_linear = 0.0
            self._target_angular = -self.angular_vel
            self._lease_deadline = now_monotonic + self.lease_duration_sec
        elif k == " ":
            # Immediate stop
            self.cancel_lease()
        # Unrecognized keys do not renew lease or alter target

        return self.get_velocities(now_monotonic)

    def get_velocities(self, now_monotonic: float) -> Tuple[float, float]:
        """
        Return (linear_x, angular_z). If lease has expired, returns (0.0, 0.0).
        """
        if self._lease_deadline is not None and now_monotonic <= self._lease_deadline:
            return self._target_linear, self._target_angular
        return 0.0, 0.0

    def cancel_lease(self) -> Tuple[float, float]:
        """Immediately cancel active lease and zero velocities."""
        self._target_linear = 0.0
        self._target_angular = 0.0
        self._lease_deadline = None
        return 0.0, 0.0
