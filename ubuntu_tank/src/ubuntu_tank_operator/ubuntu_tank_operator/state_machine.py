"""
Deterministic Operator State Machine and Motion Lease Manager.

Implements Milestone 10 state transitions, single-operator ownership,
monotonic challenge validation, configurable input lease enforcement, 5 s continuous
hold cap, 30 s idle timeout, telemetry freshness gating, and fail-closed stop priority.
"""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Callable

from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.constants import (
    CHALLENGE_INTERVAL_SEC,
    DEFAULT_CONTROL_IDLE_TIMEOUT_SEC,
    DEFAULT_WEB_ANGULAR_SPEED,
    DEFAULT_WEB_LINEAR_SPEED,
    FIRST_COMMAND_DEADLINE_NS,
    IDLE_TIMEOUT_NS,
    IDLE_TIMEOUT_SEC,
    LEASE_DURATION_SEC,
    MAX_CONTINUOUS_HOLD_NS,
    MAX_CONTINUOUS_HOLD_SEC,
    MAX_PERMISSIBLE_ANGULAR_SPEED,
    MAX_PERMISSIBLE_LINEAR_SPEED,
    PROTOCOL_VERSION,
)
from ubuntu_tank_protocol.deployment import admitted
from ubuntu_tank_protocol.enums import (
    ControllerServiceState,
    MotionDirection,
    OperatorState,
    ReleaseReason,
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
        lease_duration_sec: float = LEASE_DURATION_SEC,
        challenge_interval_sec: float = CHALLENGE_INTERVAL_SEC,
        control_idle_timeout_sec: float = DEFAULT_CONTROL_IDLE_TIMEOUT_SEC,
    ) -> None:
        WebControlConfig(
            lease_duration_sec=lease_duration_sec,
            challenge_interval_sec=challenge_interval_sec,
            control_idle_timeout_sec=control_idle_timeout_sec,
        ).validate()
        self.lease_duration_sec = lease_duration_sec
        self.lease_duration_ns = int(lease_duration_sec * 1e9)
        self.control_idle_timeout_sec = float(control_idle_timeout_sec)
        self.control_idle_timeout_ns = int(control_idle_timeout_sec * 1e9)
        self.inactivity_deadline_monotonic_ns: int = 0
        self.status_revision: int = 0
        self.session_id: str | None = None
        self.release_progress: str | None = None
        self.last_release_reason: str | None = None
        self.last_released_session_id: str | None = None
        self.last_start_request_id: str | None = None
        self.last_stop_request_id: str | None = None
        self.on_idle_timeout_release: Callable[[], None] | None = None
        self.input_generation = 0
        self.pause_reason: str | None = None
        self.pause_started_ns: int | None = None
        self.zero_confirmed_ns: int | None = None
        self.on_zero_required: Callable[[], None] | None = None
        self.release_id = os.environ.get("UBUNTU_TANK_RELEASE_ID", release_id)
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

    def _reset_inactivity_deadline_locked(self, current_monotonic_ns: int) -> None:
        """Reset the ownership inactivity deadline to control_idle_timeout_sec from now."""
        self.inactivity_deadline_monotonic_ns = (
            current_monotonic_ns + self.control_idle_timeout_ns
        )

    def _release_blocks_control(self, now_ns: int) -> bool:
        """Keep expired/releasing sessions motionless until shutdown and reacquisition."""
        return self.release_progress is not None or (
            self.inactivity_deadline_monotonic_ns > 0
            and now_ns >= self.inactivity_deadline_monotonic_ns
        )

    def acquire(
        self,
        owner_id: str,
        current_monotonic_ns: int,
        max_linear_speed: float | None = None,
        max_angular_speed: float | None = None,
    ) -> tuple[bool, int | None, WebControlErrorCode | None, str | None]:
        """Acquire single operator ownership while disarmed."""
        with self._lock:
            if self.release_progress is not None:
                return (
                    False,
                    None,
                    WebControlErrorCode.DEPLOYMENT_BUSY,
                    "Controller release pending",
                )
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
            self.session_id = owner_id
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
            self.last_start_request_id = None
            self.last_stop_request_id = None
            self.release_progress = None
            self.active_linear_speed = target_linear
            self.active_angular_speed = target_angular
            self.last_fault = None
            self._reset_inactivity_deadline_locked(current_monotonic_ns)
            self.status_revision += 1

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
            self.last_release_reason = ReleaseReason.EXPLICIT_RELEASE.value
            self.last_released_session_id = self.owner_id
            self.inactivity_deadline_monotonic_ns = 0
            self.owner_id = None
            self.session_id = None
            self.state = OperatorState.NO_OWNER
            self.active_linear_speed = self.linear_speed_cap
            self.active_angular_speed = self.angular_speed_cap
            self.status_revision += 1
            return True, None, "Ownership released"

    def arm(
        self,
        owner_id: str,
        epoch: int,
        current_monotonic_ns: int,
        request_id: str | None = None,
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Initiate explicit arming after ownership, neutral and health checks."""
        with self._lock:
            if self._release_blocks_control(current_monotonic_ns):
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    "Ownership expired or release pending",
                )
            if not admitted():
                return (
                    False,
                    WebControlErrorCode.DEPLOYMENT_BUSY,
                    "Deployment is not admitted",
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
            if request_id is not None and request_id != self.last_start_request_id:
                self._reset_inactivity_deadline_locked(current_monotonic_ns)
                self.last_start_request_id = request_id
            self.status_revision += 1
            return True, None, "Arming transaction initiated"

    def start(
        self,
        owner_id: str,
        epoch: int,
        current_monotonic_ns: int,
        request_id: str | None = None,
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Explicit, idempotent start/arming command for authorized operator."""
        with self._lock:
            if self._release_blocks_control(current_monotonic_ns):
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    "Ownership expired or release pending",
                )
            if not admitted():
                return (
                    False,
                    WebControlErrorCode.DEPLOYMENT_BUSY,
                    "Deployment is not admitted",
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
            if (
                self.inactivity_deadline_monotonic_ns > 0
                and current_monotonic_ns >= self.inactivity_deadline_monotonic_ns
            ):
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    "Ownership inactive/expired",
                )

            # If already armed, return idempotent success
            if self.state in (
                OperatorState.ARMED_IDLE,
                OperatorState.DRIVING,
                OperatorState.INPUT_PAUSED,
            ):
                if request_id is not None and request_id != self.last_start_request_id:
                    self._reset_inactivity_deadline_locked(current_monotonic_ns)
                    self.last_start_request_id = request_id
                self.status_revision += 1
                return True, None, "Already armed"

            if self.state == OperatorState.ARMING:
                if request_id is not None and request_id != self.last_start_request_id:
                    self._reset_inactivity_deadline_locked(current_monotonic_ns)
                    self.last_start_request_id = request_id
                self.status_revision += 1
                return True, None, "Arming transaction initiated"

            return self.arm(
                owner_id, epoch, current_monotonic_ns, request_id=request_id
            )

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
            if not admitted() or self._release_blocks_control(current_monotonic_ns):
                self.stop(current_monotonic_ns)
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
            self.lease_deadline_monotonic_ns = (
                current_monotonic_ns + self.lease_duration_ns
            )
            self.disarm_pending = False
            self.compensating_disarm_required = False
            self.last_seen_sequence = -1
            self.status_revision += 1
            if self.telemetry:
                self.telemetry.guard_armed = True
                self.telemetry.guard_monotonic_ns = current_monotonic_ns

            return True, None, "Arming confirmed and downstream zero verified"

    def _expire_authority(self, current_monotonic_ns: int, reason: str) -> None:
        """Pause stale input without disarming a healthy controller."""
        if self.state == OperatorState.INPUT_PAUSED:
            return
        if self.state == OperatorState.DRIVING:
            self.last_directional_input_monotonic_ns = current_monotonic_ns
        # Expiry is not a release; retain the held-input budget until neutral recovery.
        self.active_direction = MotionDirection.NEUTRAL
        self.lease_deadline_monotonic_ns = 0
        self.outstanding_challenges.clear()
        self.input_generation += 1
        self.pause_started_ns = current_monotonic_ns
        self.zero_confirmed_ns = None
        self.pause_reason = reason
        self.state = OperatorState.INPUT_PAUSED
        self.status_revision += 1
        if self.on_zero_required:
            self.on_zero_required()

    def confirm_zero_delivery(self, current_monotonic_ns: int) -> None:
        """Refresh paused zero-delivery evidence; ongoing writes must remain fresh."""
        with self._lock:
            if (
                self.state == OperatorState.INPUT_PAUSED
                and self.pause_started_ns is not None
                and current_monotonic_ns >= self.pause_started_ns
            ):
                self.zero_confirmed_ns = current_monotonic_ns

    def issue_challenge(self, current_monotonic_ns: int) -> Challenge | None:
        """Issue an unpredictable single-use challenge with monotonic deadline."""
        with self._lock:
            if self._release_blocks_control(current_monotonic_ns):
                return None
            if self.state not in (
                OperatorState.ARMED_IDLE,
                OperatorState.DRIVING,
                OperatorState.INPUT_PAUSED,
            ):
                return None

            # Expire existing authority if current lease deadline has already passed
            if (
                self.lease_deadline_monotonic_ns > 0
                and current_monotonic_ns > self.lease_deadline_monotonic_ns
            ):
                self._expire_authority(
                    current_monotonic_ns,
                    (
                        f"Input lease expired (> {self.lease_duration_sec * 1000:.0f} ms); "
                        "input paused"
                    ),
                )
                return None

            token = secrets.token_urlsafe(16)
            deadline_ns = current_monotonic_ns + self.lease_duration_ns
            challenge = Challenge(
                token=token,
                epoch=self.epoch,
                deadline_monotonic_ns=deadline_ns,
                issued_monotonic_ns=current_monotonic_ns,
                input_generation=self.input_generation,
                recovery_required=self.state == OperatorState.INPUT_PAUSED,
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
            if self._release_blocks_control(current_monotonic_ns):
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    "Ownership expired or release pending",
                )
            if (
                self.inactivity_deadline_monotonic_ns > 0
                and current_monotonic_ns >= self.inactivity_deadline_monotonic_ns
            ):
                return (
                    False,
                    WebControlErrorCode.LEASE_EXPIRED,
                    "Ownership inactive/expired",
                )

            if self.state not in (
                OperatorState.ARMED_IDLE,
                OperatorState.DRIVING,
                OperatorState.INPUT_PAUSED,
            ):
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
                        "input paused"
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

            if response.input_generation != self.input_generation:
                return (
                    False,
                    WebControlErrorCode.STALE_TRANSACTION,
                    "Stale input generation",
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
                self.state
                not in (
                    OperatorState.ARMED_IDLE,
                    OperatorState.DRIVING,
                    OperatorState.INPUT_PAUSED,
                )
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

            if self.state == OperatorState.INPUT_PAUSED:
                if response.direction != MotionDirection.NEUTRAL:
                    return (
                        False,
                        WebControlErrorCode.INPUT_CONFLICT,
                        "Release controls and send fresh neutral",
                    )
                healthy, err, message = self.telemetry.is_healthy(current_monotonic_ns)
                if not healthy or self.telemetry.guard_armed is not True:
                    self.stop(current_monotonic_ns)
                    return (
                        False,
                        err or WebControlErrorCode.INVALID_STATE,
                        message or "Guard is disarmed",
                    )
                if self.zero_confirmed_ns is None:
                    return (
                        False,
                        WebControlErrorCode.INVALID_STATE,
                        "Waiting for downstream zero",
                    )
                self.pause_reason = None
                self.pause_started_ns = None
                self.zero_confirmed_ns = None
                # Invalidate challenges issued before the recovery acknowledgment.
                self.outstanding_challenges.clear()
                self.input_generation += 1

            prev_direction = self.active_direction

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

            if response.direction != prev_direction:
                self._reset_inactivity_deadline_locked(current_monotonic_ns)

            self.status_revision += 1
            self.last_seen_sequence = response.sequence
            self.lease_deadline_monotonic_ns = challenge.deadline_monotonic_ns

            return True, None, "Motion intent processed successfully"

    def check_deadlines(
        self, current_monotonic_ns: int
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Periodic deadline and telemetry freshness monitor (run at >= 50 Hz)."""
        with self._lock:
            if not admitted():
                if self.owner_id is not None:
                    self.release(self.owner_id, self.epoch, current_monotonic_ns)
                elif self.state != OperatorState.NO_OWNER:
                    self.stop(current_monotonic_ns)
                else:
                    return True, None, None
                return (
                    False,
                    WebControlErrorCode.DEPLOYMENT_BUSY,
                    "Deployment is not admitted",
                )
            # 1. Telemetry health and freshness check
            healthy, err_code, msg = self.telemetry.is_healthy(current_monotonic_ns)
            if not healthy and self.state in (
                OperatorState.ARMED_IDLE,
                OperatorState.DRIVING,
                OperatorState.ARMING,
                OperatorState.INPUT_PAUSED,
            ):
                self.stop(current_monotonic_ns)
                self.state = OperatorState.FAULT
                self.last_fault = f"Telemetry failure: {msg}"
                return False, err_code, self.last_fault

            if (
                self.state
                in (
                    OperatorState.ARMED_IDLE,
                    OperatorState.DRIVING,
                    OperatorState.INPUT_PAUSED,
                )
                and self.telemetry.guard_armed is not True
            ):
                self.stop(current_monotonic_ns)
                self.state = OperatorState.FAULT
                self.last_fault = "Guard unexpectedly disarmed"
                return False, WebControlErrorCode.INVALID_STATE, self.last_fault
            if (
                self.state == OperatorState.INPUT_PAUSED
                and self.pause_started_ns is not None
            ) and (
                current_monotonic_ns
                - (
                    self.zero_confirmed_ns
                    if self.zero_confirmed_ns is not None
                    else self.pause_started_ns
                )
                > FIRST_COMMAND_DEADLINE_NS
            ):
                self.stop(current_monotonic_ns)
                self.state = OperatorState.FAULT
                self.last_fault = "Paused motion zero delivery unconfirmed"
                return False, WebControlErrorCode.TIMEOUT, self.last_fault
            if (
                self.direction_hold_start_monotonic_ns is not None
                and current_monotonic_ns - self.direction_hold_start_monotonic_ns
                > MAX_CONTINUOUS_HOLD_NS
            ):
                self.stop(current_monotonic_ns)
                return (
                    False,
                    WebControlErrorCode.MAX_HOLD_EXCEEDED,
                    "Continuous hold cap reached",
                )

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
                        f"Input lease expired (> {self.lease_duration_sec * 1000:.0f} ms);"
                        " input paused"
                    ),
                )
                return True, None, None

            # 4. 30-second idle timeout check while armed
            if (
                self.state in (OperatorState.ARMED_IDLE, OperatorState.INPUT_PAUSED)
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

            # 5. Ownership inactivity timeout check (§4.1.1)
            if (
                self.state != OperatorState.NO_OWNER
                and self.inactivity_deadline_monotonic_ns > 0
                and current_monotonic_ns >= self.inactivity_deadline_monotonic_ns
            ):
                self.inactivity_deadline_monotonic_ns = 0
                expired_owner = self.owner_id
                self.stop(current_monotonic_ns, requester_id=expired_owner)
                self.last_release_reason = ReleaseReason.CONTROL_IDLE_TIMEOUT.value
                self.last_released_session_id = expired_owner
                self.release_progress = "stopping_controller"
                self.status_revision += 1
                if self.on_idle_timeout_release is not None:
                    self.on_idle_timeout_release()
                return (
                    False,
                    WebControlErrorCode.TIMEOUT,
                    "Control released due to inactivity",
                )

            return True, None, None

    def stop(
        self,
        current_monotonic_ns: int,
        requester_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Immediate stop priority: halt motion, disarm, and invalidate epoch."""
        with self._lock:
            # Count a fresh owner Stop once, without renewing expired authority.
            if (
                requester_id is not None
                and requester_id == self.owner_id
                and self.release_progress is None
                and 0 < self.inactivity_deadline_monotonic_ns
                and current_monotonic_ns < self.inactivity_deadline_monotonic_ns
                and request_id is not None
                and request_id != self.last_stop_request_id
            ):
                self._reset_inactivity_deadline_locked(current_monotonic_ns)
                self.last_stop_request_id = request_id

            self.input_generation += 1
            self.pause_reason = None
            self.pause_started_ns = None
            self.zero_confirmed_ns = None
            self.active_direction = MotionDirection.NEUTRAL
            self.direction_hold_start_monotonic_ns = None
            self.lease_deadline_monotonic_ns = 0
            self.arming_start_monotonic_ns = None
            self.active_arm_epoch = None
            self.active_arm_request_id = None
            self.outstanding_challenges.clear()
            self.last_seen_sequence = -1
            self.status_revision += 1
            was_armed = (
                self.state
                in (
                    OperatorState.DRIVING,
                    OperatorState.ARMED_IDLE,
                    OperatorState.ARMING,
                    OperatorState.FAULT,
                    OperatorState.INPUT_PAUSED,
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
                OperatorState.INPUT_PAUSED,
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
            if self.release_progress is not None or self.state != OperatorState.DRIVING:
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
            remaining_inactivity_sec: float | None = None
            if (
                self.state != OperatorState.NO_OWNER
                and self.inactivity_deadline_monotonic_ns > 0
            ):
                remaining_inactivity_sec = max(
                    0.0,
                    (self.inactivity_deadline_monotonic_ns - current_monotonic_ns)
                    / 1e9,
                )

            return StatusResponse(
                service_state=service_state,
                operator_state=self.state,
                active_owner=self.owner_id,
                current_epoch=self.epoch
                if self.state != OperatorState.NO_OWNER
                else None,
                guard_armed=self.telemetry.guard_armed if self.telemetry else None,
                input_generation=self.input_generation,
                pause_reason=self.pause_reason,
                recovery_ready=self.state == OperatorState.INPUT_PAUSED
                and self.zero_confirmed_ns is not None,
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
                    "lease_duration_sec": self.lease_duration_sec,
                    "max_hold_sec": MAX_CONTINUOUS_HOLD_SEC,
                    "idle_timeout_sec": IDLE_TIMEOUT_SEC,
                    "control_idle_timeout_sec": self.control_idle_timeout_sec,
                },
                freshness=freshness,
                last_fault=self.last_fault,
                release_id=self.release_id,
                protocol_version=PROTOCOL_VERSION,
                status_revision=self.status_revision,
                session_id=self.session_id,
                control_idle_timeout_sec=self.control_idle_timeout_sec,
                remaining_inactivity_sec=remaining_inactivity_sec,
                release_progress=self.release_progress,
                last_release_reason=self.last_release_reason,
                last_released_session_id=self.last_released_session_id,
            )
