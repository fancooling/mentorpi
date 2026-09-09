"""
Motor guard core logic.

Enforces disarmed-by-default operation, monotonic command freshness, 4-motor command
validation, speed limits, explicit arm/disarm boundaries, and repeated zero output
on timeout, fault, or disarm.
"""

import math
from typing import Dict, List, Optional, Tuple


class MotorGuard:
    """Core state machine and validation rules for the motor guard."""

    def __init__(self, max_rps: float = 2.0, timeout_sec: float = 0.250):
        if max_rps <= 0.0 or not math.isfinite(max_rps):
            raise ValueError(f"max_rps must be a positive finite float, got {max_rps}")
        if timeout_sec <= 0.0 or not math.isfinite(timeout_sec):
            raise ValueError(f"timeout_sec must be a positive finite float, got {timeout_sec}")

        self.max_rps: float = float(max_rps)
        self.timeout_sec: float = float(timeout_sec)

        # Invariant: starts disarmed after every launch, crash, or restart
        self._armed: bool = False
        self._last_command_monotonic: Optional[float] = None
        self._last_command: Optional[List[Tuple[int, float]]] = None

    @property
    def is_armed(self) -> bool:
        """Return current arming state."""
        return self._armed

    def arm(self) -> Tuple[bool, str]:
        """
        Explicitly arm the guard.

        Arming clears any cached command so the operator must supply a fresh command
        after arming.
        """
        self._armed = True
        self._last_command_monotonic = None
        self._last_command = None
        return True, "Motor guard armed. Fresh command required to move."

    def disarm(self) -> Tuple[bool, str, List[Tuple[int, float]]]:
        """
        Explicitly disarm the guard.

        Disarming clears active commands and returns a four-motor zero command.
        """
        self._armed = False
        self._last_command_monotonic = None
        self._last_command = None
        return True, "Motor guard disarmed.", self.get_zero_command()

    @staticmethod
    def get_zero_command() -> List[Tuple[int, float]]:
        """Return the standard four-motor zero command."""
        return [(1, 0.0), (2, 0.0), (3, 0.0), (4, 0.0)]

    def validate_command(self, motor_states: List[Tuple[int, float]]) -> Tuple[bool, str]:
        """
        Validate incoming motor command states.

        Requires:
        - Exactly 4 motor states.
        - Motor IDs must be {1, 2, 3, 4} with no duplicates or missing IDs.
        - All RPS values must be finite (no NaN, +Inf, -Inf).
        - All abs(RPS) values must be <= max_rps.
        """
        if not isinstance(motor_states, (list, tuple)):
            return False, f"Expected list of motor states, got {type(motor_states)}"

        if len(motor_states) != 4:
            return False, f"Expected exactly 4 motor states, got {len(motor_states)}"

        seen_ids = set()
        for item in motor_states:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                return False, f"Invalid motor state entry format: {item}"
            m_id, rps = item
            if not isinstance(m_id, int):
                return False, f"Motor ID must be an integer, got {type(m_id)}"
            if m_id not in (1, 2, 3, 4):
                return False, f"Motor ID must be in {1, 2, 3, 4}, got {m_id}"
            if m_id in seen_ids:
                return False, f"Duplicate motor ID detected: {m_id}"
            seen_ids.add(m_id)

            if not isinstance(rps, (int, float)):
                return False, f"Motor {m_id} RPS must be numeric, got {type(rps)}"
            if not math.isfinite(rps):
                return False, f"Motor {m_id} RPS is not finite (NaN or Inf): {rps}"
            if abs(rps) > self.max_rps:
                return False, f"Motor {m_id} RPS {rps} exceeds limit {self.max_rps}"

        if seen_ids != {1, 2, 3, 4}:
            return False, f"Motor IDs do not cover complete set {{1, 2, 3, 4}}: {seen_ids}"

        return True, "Valid"

    def handle_command(
        self,
        motor_states: List[Tuple[int, float]],
        now_monotonic: float
    ) -> Tuple[Optional[List[Tuple[int, float]]], bool, str]:
        """
        Process an incoming motor command at the given monotonic timestamp.

        Returns:
            (command_to_forward, was_disarmed_by_fault, reason)
        """
        if not self._armed:
            return None, False, "Guard is disarmed; command dropped"

        valid, reason = self.validate_command(motor_states)
        if not valid:
            # Fault invariant: invalid input immediately disarms and sends zero
            self._armed = False
            self._last_command_monotonic = None
            self._last_command = None
            return self.get_zero_command(), True, f"Invalid command: {reason}"

        self._last_command_monotonic = now_monotonic
        # Store canonical ordered list [(1, r1), (2, r2), (3, r3), (4, r4)]
        ordered = sorted(motor_states, key=lambda x: x[0])
        self._last_command = ordered
        return ordered, False, "Command accepted and forwarded"

    def check_timeout(
        self,
        now_monotonic: float
    ) -> Tuple[bool, Optional[List[Tuple[int, float]]]]:
        """
        Check if the active motion lease has timed out according to monotonic clock.

        Returns:
            (timed_out, zero_command_if_timed_out)
        """
        if not self._armed:
            return False, None

        if self._last_command_monotonic is None:
            # Armed, but waiting for first valid command. No timeout yet.
            return False, None

        elapsed = now_monotonic - self._last_command_monotonic
        if elapsed > self.timeout_sec:
            # Freshness invariant violated: disarm and emit zero command
            self._armed = False
            self._last_command_monotonic = None
            self._last_command = None
            return True, self.get_zero_command()

        return False, None
