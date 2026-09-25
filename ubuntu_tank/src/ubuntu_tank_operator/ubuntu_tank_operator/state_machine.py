"""
Deterministic Operator State Machine and Motion Lease Manager.

Implements Milestone 10 state transitions, single-operator ownership,
monotonic challenge validation, 150 ms motion lease enforcement, 5 s continuous
hold cap, 30 s idle timeout, telemetry freshness gating, and fail-closed stop priority.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from typing import Any

from ubuntu_tank_protocol.constants import (
    DEFAULT_WEB_ANGULAR_SPEED,
    DEFAULT_WEB_LINEAR_SPEED,
    FIRST_COMMAND_DEADLINE_NS,
    IDLE_TIMEOUT_NS,
    IDLE_TIMEOUT_SEC,
    LEASE_DURATION_NS,
    LEASE_DURATION_SEC,
    MAX_CONTINUOUS_HOLD_NS,
    MAX_CONTINUOUS_HOLD_SEC,
    MAX_PERMISSIBLE_ANGULAR_SPEED,
    MAX_PERMISSIBLE_LINEAR_SPEED,
    PROTOCOL_VERSION,
)
from ubuntu_tank_protocol.enums import (
    ControllerServiceState,
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_protocol.schemas import (
    Challenge,
    ChallengeResponse,
    StatusResponse,
    TelemetrySnapshot,
)


class OperatorStateMachine:
    """State machine governing operator ownership, arming, and motion leases."""

    def __init__(
        self,
        release_id: str = "current",
        linear_speed_cap: float = DEFAULT_WEB_LINEAR_SPEED,
        angular_speed_cap: float = DEFAULT_WEB_ANGULAR_SPEED,
        lock: threading.RLock | None = None,
    ) -> None:
        self.release_id = release_id
        self.linear_speed_cap = min(linear_speed_cap, MAX_PERMISSIBLE_LINEAR_SPEED)
        self.angular_speed_cap = min(angular_speed_cap, MAX_PERMISSIBLE_ANGULAR_SPEED)
        self.active_linear_speed = self.linear_speed_cap
        self.active_angular_speed = self.angular_speed_cap

        self._lock = lock if lock is not None else threading.RLock()
        self.lock = self._lock

        self.state: OperatorState = OperatorState.NO_OWNER
        self.owner_id: str | None = None
        self.epoch: int = 0
        self.outstanding_challenges: dict[str, Challenge] = {}
        self.last_seen_sequence: int = -1
        self.lease_deadline_monotonic_ns: int = 0
        self.active_direction: MotionDirection = MotionDirection.NEUTRAL
        self.direction_hold_start_monotonic_ns: int | None = None
        self.last_directional_input_monotonic_ns: int | None = None
        self.arming_start_monotonic_ns: int | None = None
        self.active_arm_epoch: int | None = None
        self.active_arm_request_id: str | None = None
        self.disarm_pending: bool = False
        self.compensating_disarm_required: bool = False
        self.on_disarm_required: Callable[[], None] | None = None
        self.last_fault: str | None = None
        self.telemetry: TelemetrySnapshot = TelemetrySnapshot()

    def acquire(
        self,
        owner_id: str,
        current_monotonic_ns: int,
        max_linear_speed: float | None = None,
        max_angular_speed: float | None = None,
    ) -> tuple[bool, int | None, WebControlErrorCode | None, str | None]:
        """Acquire single operator ownership while disarmed."""
        with self._lock:
            if not isinstance(owner_id, str) or not owner_id:
                return (
                    False,
                    None,
                    WebControlErrorCode.INVALID_PAYLOAD,
                    "Operator ID must be a non-empty string",
                )

            # Validate requested operating speeds if provided
            target_linear = self.linear_speed_cap
            if max_linear_speed is not None:
                if max_linear_speed > self.linear_speed_cap:
                    return (
                        False,
                        None,
                        WebControlErrorCode.INVALID_PAYLOAD,
                        (
                            f"Requested linear speed {max_linear_speed:.2f} m/s "
                            f"exceeds limit {self.linear_speed_cap:.2f} m/s"
                        ),
                    )
                if max_linear_speed <= 0.0:
                    return (
                        False,
                        None,
                        WebControlErrorCode.INVALID_PAYLOAD,
                        "Requested linear speed must be positive",
                    )
                target_linear = float(max_linear_speed)

            target_angular = self.angular_speed_cap
            if max_angular_speed is not None:
                if max_angular_speed > self.angular_speed_cap:
                    return (
                        False,
                        None,
                        WebControlErrorCode.INVALID_PAYLOAD,
                        (
                            f"Requested angular speed {max_angular_speed:.2f} rad/s "
                            f"exceeds limit {self.angular_speed_cap:.2f} rad/s"
                        ),
                    )
                if max_angular_speed <= 0.0:
                    return (
                        False,
                        None,
                        WebControlErrorCode.INVALID_PAYLOAD,
                        "Requested angular speed must be positive",
                    )
                target_angular = float(max_angular_speed)

            # Check if currently owned by a different active operator
            if self.state != OperatorState.NO_OWNER and self.owner_id != owner_id:
                return (
                    False,
                    None,
                    WebControlErrorCode.DEPLOYMENT_BUSY,
                    f"Operator authority already held by '{self.owner_id}'",
                )

            # Check pending disarm / observed guard state: cannot acquire while disarm is pending or guard is armed
            if (
                self.disarm_pending
                or self.compensating_disarm_required
                or (self.telemetry and self.telemetry.guard_armed is True)
            ):
                return (
                    False,
                    None,
                    WebControlErrorCode.INVALID_STATE,
                    "Cannot acquire operator ownership while disarm is pending downstream confirmation",
                )

            self.owner_id = owner_id
            self.epoch += 1
            self.state = OperatorState.OWNED_DISARMED
            self.outstanding_challenges.clear()
            self.last_seen_sequence = -1
            self.lease_deadline_monotonic_ns = 0
            self.active_direction = MotionDirection.NEUTRAL
            self.direction_hold_start_monotonic_ns = None
            self.last_directional_input_monotonic_ns = None
            self.arming_start_monotonic_ns = None
            self.active_arm_epoch = None
            self.active_arm_request_id = None
            self.active_linear_speed = target_linear
            self.active_angular_speed = target_angular
            self.last_fault = None

            return True, self.epoch, None, "Ownership acquired"

    def release(
        self, owner_id: str, epoch: int, current_monotonic_ns: int
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Relinquish operator ownership, stopping and disarming first."""
        with self._lock:
            if self.owner_id != owner_id:
                return (
                    False,
                    WebControlErrorCode.NOT_OWNER,
                    f"Caller '{owner_id}' does not hold operator authority",
                )
            if epoch != self.epoch:
                return (
                    False,
                    WebControlErrorCode.INVALID_EPOCH,
                    f"Mismatched epoch {epoch} (current is {self.epoch})",
                )

            self.stop(current_monotonic_ns, requester_id=owner_id)
            self.owner_id = None
            self.state = OperatorState.NO_OWNER
            self.active_linear_speed = self.linear_speed_cap
            self.active_angular_speed = self.angular_speed_cap
            return True, None, "Ownership released"

    def arm(
        self,
        owner_id: str,
        epoch: int,
        tracks_raised: Any,
        current_monotonic_ns: int,
        request_id: str | None = None,
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Initiate arming sequence. Requires exact boolean tracks_raised=True."""
        with self._lock:
            # Safety Invariant: require exact boolean True
            if type(tracks_raised) is not bool or tracks_raised is not True:
                return (
                    False,
                    WebControlErrorCode.TRACKS_NOT_RAISED,
                    (
                        "Arming strictly requires tracks_raised affirmation (exact boolean "
                        "True)"
                    ),
                )

            if self.state != OperatorState.OWNED_DISARMED:
                return (
                    False,
                    WebControlErrorCode.INVALID_STATE,
                    f"Cannot arm from state '{self.state.value}'; must be 'OWNED_DISARMED'",
                )

            if self.owner_id != owner_id:
                return (
                    False,
                    WebControlErrorCode.NOT_OWNER,
                    f"Caller '{owner_id}' is not the authorized operator",
                )

            if epoch != self.epoch:
                return (
                    False,
                    WebControlErrorCode.INVALID_EPOCH,
                    f"Stale or invalid control epoch {epoch} (current is {self.epoch})",
                )

            # Check pending disarm / observed guard state: cannot arm while disarm is pending or guard is armed
            if (
                self.disarm_pending
                or self.compensating_disarm_required
                or (self.telemetry and self.telemetry.guard_armed is True)
            ):
                return (
                    False,
                    WebControlErrorCode.INVALID_STATE,
                    "Cannot arm while disarm operation is pending downstream confirmation",
                )

            # Telemetry and preflight freshness check
            healthy, err_code, msg = self.telemetry.is_healthy(current_monotonic_ns)
            if not healthy:
                return (
                    False,
                    err_code,
                    f"Cannot arm with unhealthy/stale telemetry: {msg}",
                )

            # Neutral input check: input must be neutral
            if self.active_direction != MotionDirection.NEUTRAL:
                return (
                    False,
                    WebControlErrorCode.INPUT_CONFLICT,
                    "Arming requires neutral input; active direction must be released",
                )

            self.state = OperatorState.ARMING
            self.arming_start_monotonic_ns = current_monotonic_ns
            self.active_arm_epoch = epoch
            self.active_arm_request_id = request_id or secrets.token_hex(8)
            return True, None, "Arming transaction initiated"

    def confirm_armed(
        self,
        guard_confirmed: bool,
        downstream_zero_confirmed: bool,
        current_monotonic_ns: int,
        epoch: int | None = None,
        request_id: str | None = None,
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Confirm arming response and downstream zero within first-command deadline."""
        with self._lock:
            if self.state != OperatorState.ARMING:
                if guard_confirmed:
                    self.compensating_disarm_required = True
                    self.disarm_pending = True
                    if self.on_disarm_required:
                        self.on_disarm_required()
                return (
                    False,
                    WebControlErrorCode.INVALID_STATE,
                    f"Cannot confirm arming from state '{self.state.value}'",
                )

            # Correlation check: verify originating epoch (strictly required)
            if epoch is None or epoch != self.active_arm_epoch:
                if guard_confirmed:
                    self.compensating_disarm_required = True
                    self.disarm_pending = True
                    if self.on_disarm_required:
                        self.on_disarm_required()
                return (
                    False,
                    WebControlErrorCode.INVALID_EPOCH,
                    (
                        f"Arm confirmation requires matching active arming epoch "
                        f"(provided: {epoch}, expected: {self.active_arm_epoch})"
                    ),
                )

            # Correlation check: verify originating request_id (strictly required)
            if not request_id or request_id != self.active_arm_request_id:
                if guard_confirmed:
                    self.compensating_disarm_required = True
                    self.disarm_pending = True
                    if self.on_disarm_required:
                        self.on_disarm_required()
                return (
                    False,
                    WebControlErrorCode.STALE_TRANSACTION,
                    (
                        f"Arm confirmation requires matching active arming request_id "
                        f"(provided: '{request_id}', expected: '{self.active_arm_request_id}')"
                    ),
                )

            # Check 250 ms first-command deadline
            if self.arming_start_monotonic_ns is not None:
                elapsed_ns = current_monotonic_ns - self.arming_start_monotonic_ns
                if elapsed_ns > FIRST_COMMAND_DEADLINE_NS:
                    self.stop(current_monotonic_ns)
                    self.state = OperatorState.FAULT
                    self.compensating_disarm_required = True
                    if self.on_disarm_required:
                        self.on_disarm_required()
                    self.last_fault = (
                        "Arming timed out before downstream zero confirmation"
                        f" ({elapsed_ns / 1e6:.1f} ms > 250 ms)"
                    )
                    return False, WebControlErrorCode.TIMEOUT, self.last_fault

            if not guard_confirmed or not downstream_zero_confirmed:
                self.stop(current_monotonic_ns)
                self.state = OperatorState.FAULT
                self.compensating_disarm_required = True
                if self.on_disarm_required:
                    self.on_disarm_required()
                self.last_fault = "Guard or downstream zero delivery confirmation failed during arming"
                return False, WebControlErrorCode.OPERATION_FAILED, self.last_fault

            self.state = OperatorState.ARMED_IDLE
            self.arming_start_monotonic_ns = None
            self.active_arm_epoch = None
            self.active_arm_request_id = None
            self.last_directional_input_monotonic_ns = current_monotonic_ns
            # Initialize bounded input lease upon arming completion (enforced in ARMED_IDLE)
            self.lease_deadline_monotonic_ns = current_monotonic_ns + LEASE_DURATION_NS
            self.disarm_pending = False
            self.compensating_disarm_required = False
            self.last_seen_sequence = -1
            if self.telemetry:
                self.telemetry.guard_armed = True
                self.telemetry.guard_monotonic_ns = current_monotonic_ns

            return True, None, "Arming confirmed and downstream zero verified"

    def _expire_authority(self, current_monotonic_ns: int, reason: str) -> None:
        """Expire operator authority upon lease deadline overrun."""
        with self._lock:
            self.stop(current_monotonic_ns)
            self.state = OperatorState.FAULT
            self.last_fault = reason

    def issue_challenge(self, current_monotonic_ns: int) -> Challenge | None:
        """Issue an unpredictable single-use challenge with monotonic deadline."""
        with self._lock:
            if self.state not in (OperatorState.ARMED_IDLE, OperatorState.DRIVING):
                return None

            # Expire existing authority if current lease deadline has already passed
            if (
                self.lease_deadline_monotonic_ns > 0
                and current_monotonic_ns > self.lease_deadline_monotonic_ns
            ):
                self._expire_authority(
                    current_monotonic_ns,
                    (
                        f"Input lease expired (> {LEASE_DURATION_SEC * 1000:.0f} ms); "
                        "authority expired"
                    ),
                )
                return None

            token = secrets.token_urlsafe(16)
            deadline_ns = current_monotonic_ns + LEASE_DURATION_NS
            challenge = Challenge(
                token=token,
                epoch=self.epoch,
                deadline_monotonic_ns=deadline_ns,
                issued_monotonic_ns=current_monotonic_ns,
            )
            self.outstanding_challenges[token] = challenge

            # Purge stale expired challenges older than 1 second
            cutoff = current_monotonic_ns - int(1.0 * 1e9)
            expired_keys = [
                k
                for k, c in self.outstanding_challenges.items()
                if c.deadline_monotonic_ns < cutoff
            ]
            for k in expired_keys:
                self.outstanding_challenges.pop(k, None)

            return challenge

    def process_challenge_response(
        self,
        owner_id: str,
        response: ChallengeResponse,
        current_monotonic_ns: int,
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Validate client challenge response and apply discrete motion intent."""
        with self._lock:
            if self.state not in (OperatorState.ARMED_IDLE, OperatorState.DRIVING):
                return (
                    False,
                    WebControlErrorCode.INVALID_STATE,
                    f"Cannot process motion intent in state '{self.state.value}'",
                )

            # 1. First: Check if existing lease has already expired before processing renewal
            if (
                self.lease_deadline_monotonic_ns > 0
                and current_monotonic_ns > self.lease_deadline_monotonic_ns
            ):
                self._expire_authority(
                    current_monotonic_ns,
                    (
                        f"Input lease expired before renewal arrived (overdue by "
                        f"{(current_monotonic_ns - self.lease_deadline_monotonic_ns) / 1e6:.1f} ms); "
                        "motion halted and disarmed"
                    ),
                )
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    self.last_fault,
                )

            if self.owner_id != owner_id:
                return (
                    False,
                    WebControlErrorCode.NOT_OWNER,
                    f"Caller '{owner_id}' is not the active control owner",
                )

            if response.epoch != self.epoch:
                return (
                    False,
                    WebControlErrorCode.INVALID_EPOCH,
                    f"Stale or mismatched epoch {response.epoch} (current is {self.epoch})",
                )

            # Challenge lookup and single-use enforcement
            if response.token not in self.outstanding_challenges:
                return (
                    False,
                    WebControlErrorCode.CHALLENGE_REUSED,
                    "Challenge token not found or already consumed",
                )

            challenge = self.outstanding_challenges.pop(response.token)

            # Strict monotonic deadline check of the individual challenge
            if current_monotonic_ns > challenge.deadline_monotonic_ns:
                return (
                    False,
                    WebControlErrorCode.CHALLENGE_EXPIRED,
                    (
                        f"Challenge response arrived after deadline (overdue by "
                        f"{(current_monotonic_ns - challenge.deadline_monotonic_ns) / 1e6:.1f} ms)"
                    ),
                )

            # Sequence monotonicity check
            if response.sequence <= self.last_seen_sequence:
                return (
                    False,
                    WebControlErrorCode.SEQUENCE_OUT_OF_ORDER,
                    (
                        f"Sequence {response.sequence} out of order "
                        f"(last seen was {self.last_seen_sequence})"
                    ),
                )

            # Fail-closed re-check: verify state, epoch, and disarm_pending before applying intent
            if (
                self.state not in (OperatorState.ARMED_IDLE, OperatorState.DRIVING)
                or response.epoch != self.epoch
                or self.disarm_pending
                or self.compensating_disarm_required
            ):
                return (
                    False,
                    WebControlErrorCode.INVALID_STATE,
                    f"State changed or stop occurred during intent processing (state={self.state.value}, epoch={self.epoch})",
                )

            # Handle immediate STOP request
            if response.direction == MotionDirection.STOP:
                self.stop(current_monotonic_ns, requester_id=owner_id)
                return True, None, "Stop requested; motion halted and disarmed"

            # Non-zero motion handling
            if response.direction in (
                MotionDirection.FORWARD,
                MotionDirection.REVERSE,
                MotionDirection.SPIN_LEFT,
                MotionDirection.SPIN_RIGHT,
            ):
                if response.direction != self.active_direction:
                    # Starting new direction or changing direction: reset hold start
                    self.direction_hold_start_monotonic_ns = current_monotonic_ns
                    self.active_direction = response.direction
                else:
                    # Continuing hold of same direction: enforce 5.0 s cap
                    if self.direction_hold_start_monotonic_ns is None:
                        self.direction_hold_start_monotonic_ns = current_monotonic_ns

                    hold_duration_ns = (
                        current_monotonic_ns - self.direction_hold_start_monotonic_ns
                    )
                    if hold_duration_ns > MAX_CONTINUOUS_HOLD_NS:
                        self.stop(current_monotonic_ns, requester_id=owner_id)
                        self.last_fault = (
                            f"Continuous hold cap ({MAX_CONTINUOUS_HOLD_SEC} s) exceeded;"
                            " motion halted and disarmed"
                        )
                        return (
                            False,
                            WebControlErrorCode.MAX_HOLD_EXCEEDED,
                            self.last_fault,
                        )

                self.state = OperatorState.DRIVING
                self.last_directional_input_monotonic_ns = current_monotonic_ns

            elif response.direction == MotionDirection.NEUTRAL:
                self.active_direction = MotionDirection.NEUTRAL
                self.direction_hold_start_monotonic_ns = None
                self.state = OperatorState.ARMED_IDLE

            self.last_seen_sequence = response.sequence
            self.lease_deadline_monotonic_ns = challenge.deadline_monotonic_ns

            return True, None, "Motion intent processed successfully"

    def check_deadlines(
        self, current_monotonic_ns: int
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Periodic deadline and telemetry freshness monitor (run at >= 50 Hz)."""
        with self._lock:
            # 1. Telemetry health and freshness check
            healthy, err_code, msg = self.telemetry.is_healthy(current_monotonic_ns)
            if not healthy and self.state in (
                OperatorState.ARMED_IDLE,
                OperatorState.DRIVING,
                OperatorState.ARMING,
            ):
                self.stop(current_monotonic_ns)
                self.state = OperatorState.FAULT
                self.last_fault = f"Telemetry failure: {msg}"
                return False, err_code, self.last_fault

            # 2. Arming transaction timeout check
            if (
                self.state == OperatorState.ARMING
                and self.arming_start_monotonic_ns is not None
            ):
                elapsed_ns = current_monotonic_ns - self.arming_start_monotonic_ns
                if elapsed_ns > FIRST_COMMAND_DEADLINE_NS:
                    self.stop(current_monotonic_ns)
                    self.state = OperatorState.FAULT
                    self.compensating_disarm_required = True
                    if self.on_disarm_required:
                        self.on_disarm_required()
                    self.last_fault = (
                        "Arming transaction timed out before downstream zero delivery"
                        f" ({elapsed_ns / 1e6:.1f} ms > 250 ms)"
                    )
                    return False, WebControlErrorCode.TIMEOUT, self.last_fault

            # 3. Input lease expiry check (enforced in both DRIVING and ARMED_IDLE)
            if (
                self.state in (OperatorState.DRIVING, OperatorState.ARMED_IDLE)
                and self.lease_deadline_monotonic_ns > 0
                and current_monotonic_ns > self.lease_deadline_monotonic_ns
            ):
                self._expire_authority(
                    current_monotonic_ns,
                    (
                        f"Input lease expired (> {LEASE_DURATION_SEC * 1000:.0f} ms);"
                        " motion halted and disarmed"
                    ),
                )
                return False, WebControlErrorCode.LEASE_EXPIRED, self.last_fault

            # 4. 30-second idle timeout check while armed
            if (
                self.state == OperatorState.ARMED_IDLE
                and self.last_directional_input_monotonic_ns is not None
            ):
                idle_ns = (
                    current_monotonic_ns - self.last_directional_input_monotonic_ns
                )
                if idle_ns > IDLE_TIMEOUT_NS:
                    self.stop(current_monotonic_ns)
                    self.last_fault = (
                        f"Armed idle timeout ({IDLE_TIMEOUT_SEC:.0f} s) reached;"
                        " automatically disarmed"
                    )
                    return False, WebControlErrorCode.TIMEOUT, self.last_fault

            return True, None, None

    def stop(
        self,
        current_monotonic_ns: int,
        requester_id: str | None = None,
    ) -> None:
        """Immediate stop priority: halt motion, disarm, and invalidate epoch."""
        with self._lock:
            self.active_direction = MotionDirection.NEUTRAL
            self.direction_hold_start_monotonic_ns = None
            self.lease_deadline_monotonic_ns = 0
            self.arming_start_monotonic_ns = None
            self.active_arm_epoch = None
            self.active_arm_request_id = None
            self.outstanding_challenges.clear()
            self.last_seen_sequence = -1
            was_armed = (
                self.state
                in (
                    OperatorState.DRIVING,
                    OperatorState.ARMED_IDLE,
                    OperatorState.ARMING,
                    OperatorState.FAULT,
                )
                or (self.telemetry and self.telemetry.guard_armed is not False)
                or self.disarm_pending
                or self.compensating_disarm_required
            )
            if was_armed:
                self.disarm_pending = True
                self.compensating_disarm_required = True
                if self.on_disarm_required:
                    self.on_disarm_required()

            # Invalidate current epoch to prevent late/replayed requests from reviving motion
            self.epoch += 1

            if self.state in (
                OperatorState.DRIVING,
                OperatorState.ARMED_IDLE,
                OperatorState.ARMING,
                OperatorState.FAULT,
            ):
                self.state = (
                    OperatorState.OWNED_DISARMED
                    if self.owner_id
                    else OperatorState.NO_OWNER
                )

    def update_guard_telemetry(
        self, guard_armed: bool, current_monotonic_ns: int
    ) -> None:
        """Update observed downstream guard telemetry."""
        with self._lock:
            if self.telemetry:
                self.telemetry.guard_armed = guard_armed
                self.telemetry.guard_monotonic_ns = current_monotonic_ns
            if not guard_armed:
                self.disarm_pending = False
                self.compensating_disarm_required = False

    def update_battery_telemetry(
        self, battery_voltage: float, current_monotonic_ns: int
    ) -> None:
        """Update observed downstream battery telemetry."""
        with self._lock:
            if self.telemetry:
                self.telemetry.battery_voltage = battery_voltage
                self.telemetry.battery_monotonic_ns = current_monotonic_ns

    def update_odom_telemetry(
        self, linear_x: float, angular_z: float, current_monotonic_ns: int
    ) -> None:
        """Update observed downstream odometry telemetry."""
        with self._lock:
            if self.telemetry:
                self.telemetry.odom_linear_x = linear_x
                self.telemetry.odom_angular_z = angular_z
                self.telemetry.odom_monotonic_ns = current_monotonic_ns

    def get_velocity_command(self) -> tuple[float, float]:
        """Map current active direction to (linear_x, angular_z) velocities."""
        with self._lock:
            if self.state != OperatorState.DRIVING:
                return 0.0, 0.0

            if self.active_direction == MotionDirection.FORWARD:
                return self.active_linear_speed, 0.0
            elif self.active_direction == MotionDirection.REVERSE:
                return -self.active_linear_speed, 0.0
            elif self.active_direction == MotionDirection.SPIN_LEFT:
                return 0.0, self.active_angular_speed
            elif self.active_direction == MotionDirection.SPIN_RIGHT:
                return 0.0, -self.active_angular_speed
            else:
                return 0.0, 0.0

    def get_status(
        self,
        current_monotonic_ns: int,
        service_state: ControllerServiceState = ControllerServiceState.ACTIVE,
    ) -> StatusResponse:
        """Generate authenticated status representation."""
        with self._lock:
            vx, wz = self.get_velocity_command()
            freshness = self.telemetry.get_freshness_dict(current_monotonic_ns)

            return StatusResponse(
                service_state=service_state,
                operator_state=self.state,
                active_owner=self.owner_id,
                current_epoch=self.epoch
                if self.state != OperatorState.NO_OWNER
                else None,
                guard_armed=self.telemetry.guard_armed if self.telemetry else None,
                disarm_pending=self.disarm_pending,
                battery_voltage=self.telemetry.battery_voltage
                if self.telemetry
                else None,
                linear_speed=vx,
                angular_speed=wz,
                limits={
                    "max_linear_speed": self.active_linear_speed,
                    "max_angular_speed": self.active_angular_speed,
                    "configured_linear_speed_cap": self.linear_speed_cap,
                    "configured_angular_speed_cap": self.angular_speed_cap,
                    "lease_duration_sec": LEASE_DURATION_SEC,
                    "max_hold_sec": MAX_CONTINUOUS_HOLD_SEC,
                    "idle_timeout_sec": IDLE_TIMEOUT_SEC,
                },
                freshness=freshness,
                last_fault=self.last_fault,
                release_id=self.release_id,
                protocol_version=PROTOCOL_VERSION,
            )
