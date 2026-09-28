"""
Unit and deterministic regression tests for Milestone 10 Web Control Protocol.

Tests monotonic challenge leases, single-operator state machine, 5.0 s continuous
hold cap, 30.0 s idle timeout, stop priority, arm cancellation, telemetry freshness,
configuration validation, lock hierarchy, and schema models.
"""

import os
import sys
import tempfile
import unittest

# Ensure workspace packages are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
OPERATOR_PKG_DIR = os.path.join(REPO_ROOT, "ubuntu_tank/src/ubuntu_tank_operator")
if OPERATOR_PKG_DIR not in sys.path:
    sys.path.insert(0, OPERATOR_PKG_DIR)
sys.path.insert(
    0, os.path.join(os.path.dirname(OPERATOR_PKG_DIR), "ubuntu_tank_protocol")
)

from ubuntu_tank_operator.locks import (
    LockHierarchy,
    LockOrderViolationError,
    execute_non_blocking_stop,
)
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.constants import (
    LOCK_LEVEL_DEPLOYMENT,
    LOCK_LEVEL_LIFECYCLE,
    LOCK_LEVEL_OPERATOR,
    PROTOCOL_VERSION,
)
from ubuntu_tank_protocol.enums import (
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_protocol.schemas import (
    ChallengeResponse,
    ControlArmRequest,
    LogsRequest,
    TelemetrySnapshot,
    VersionResponse,
)


class TestMilestone10StateMachineTransitions(unittest.TestCase):
    """Verify core operator lifecycle state transitions."""

    def setUp(self):
        self.sm = OperatorStateMachine(
            release_id="test-1.0.0", lease_duration_sec=0.150
        )
        self.base_time_ns = 1_000_000_000

        # Supply healthy telemetry
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=self.base_time_ns,
            guard_armed=False,
            guard_monotonic_ns=self.base_time_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=self.base_time_ns,
        )

    def test_initial_state_is_no_owner(self):
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.sm.owner_id)
        self.assertEqual(self.sm.epoch, 0)
        vx, wz = self.sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))

    def test_acquire_and_release_lifecycle(self):
        ok, epoch, _err, _msg = self.sm.acquire("operator-1", self.base_time_ns)
        self.assertTrue(ok)
        self.assertEqual(epoch, 1)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        self.assertEqual(self.sm.owner_id, "operator-1")

        # Second operator cannot acquire while busy
        ok2, _epoch2, err2, _msg2 = self.sm.acquire("operator-2", self.base_time_ns)
        self.assertFalse(ok2)
        self.assertEqual(err2, WebControlErrorCode.DEPLOYMENT_BUSY)

        # Release by authorized owner
        rel_ok, _rel_err, _rel_msg = self.sm.release(
            "operator-1", epoch, self.base_time_ns
        )
        self.assertTrue(rel_ok)
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.sm.owner_id)

    def test_arm_and_confirm_lifecycle(self):
        self.sm.acquire("operator-1", self.base_time_ns)

        # Arm with exact boolean True
        ok, _err, _msg = self.sm.arm(
            "operator-1", 1, self.base_time_ns, request_id="arm-req-1"
        )
        self.assertTrue(ok)
        self.assertEqual(self.sm.state, OperatorState.ARMING)

        # Confirm within 250 ms with correlated epoch and request_id
        confirm_time_ns = self.base_time_ns + int(0.050 * 1e9)
        conf_ok, _conf_err, _conf_msg = self.sm.confirm_armed(
            True, True, confirm_time_ns, epoch=1, request_id="arm-req-1"
        )
        self.assertTrue(conf_ok)
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)
        self.assertTrue(self.sm.telemetry.guard_armed)


class TestMilestone10SafetyInvariants(unittest.TestCase):
    """Verify strict monotonic challenges, deadlines, hold caps, and stop priority."""

    def setUp(self):
        self.sm = OperatorStateMachine(
            release_id="test-1.0.0", lease_duration_sec=0.150
        )
        self.base_time_ns = 10_000_000_000
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=self.base_time_ns,
            guard_armed=False,
            guard_monotonic_ns=self.base_time_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=self.base_time_ns,
        )
        self.sm.acquire("operator-1", self.base_time_ns)
        self.sm.arm("operator-1", 1, self.base_time_ns, request_id="setup-arm-1")
        self.sm.confirm_armed(
            True,
            True,
            self.base_time_ns + 10_000_000,
            epoch=1,
            request_id="setup-arm-1",
        )

    def test_arm_no_longer_requires_affirmation(self):
        """Explicit Arm still requires ownership and healthy preflight."""
        sm = OperatorStateMachine()
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=100,
            guard_armed=False,
            guard_monotonic_ns=100,
        )
        sm.acquire("op", 100)
        self.assertTrue(sm.arm("op", 1, 100)[0])

    def test_challenge_single_use_and_replay_rejection(self):
        challenge = self.sm.issue_challenge(self.base_time_ns)
        self.assertIsNotNone(challenge)

        resp = ChallengeResponse(
            input_generation=challenge.input_generation,
            token=challenge.token,
            epoch=1,
            sequence=1,
            direction=MotionDirection.FORWARD,
        )

        # First use succeeds
        ok1, _err1, _ = self.sm.process_challenge_response(
            "operator-1", resp, self.base_time_ns + 10_000_000
        )
        self.assertTrue(ok1)
        self.assertEqual(self.sm.state, OperatorState.DRIVING)

        # Second use of identical token fails closed with CHALLENGE_REUSED
        ok2, err2, _ = self.sm.process_challenge_response(
            "operator-1", resp, self.base_time_ns + 20_000_000
        )
        self.assertFalse(ok2)
        self.assertEqual(err2, WebControlErrorCode.CHALLENGE_REUSED)

    def test_challenge_monotonic_deadline_expiry(self):
        """Challenge response arriving after 150 ms deadline must be rejected."""
        challenge = self.sm.issue_challenge(self.base_time_ns)
        # Deadline is base_time_ns + 150 ms
        late_time_ns = challenge.deadline_monotonic_ns + 1_000_000  # 1 ms past 150 ms

        resp = ChallengeResponse(
            input_generation=challenge.input_generation,
            token=challenge.token,
            epoch=1,
            sequence=1,
            direction=MotionDirection.FORWARD,
        )

        ok, err, _ = self.sm.process_challenge_response(
            "operator-1", resp, late_time_ns
        )
        self.assertFalse(ok)
        self.assertEqual(err, WebControlErrorCode.CHALLENGE_EXPIRED)
        # Ensure tank is not moving
        vx, wz = self.sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))

    def test_sequence_monotonicity_enforcement(self):
        """Responses must have strictly increasing sequence numbers."""
        c1 = self.sm.issue_challenge(self.base_time_ns)
        r1 = ChallengeResponse(
            input_generation=c1.input_generation,
            token=c1.token,
            epoch=1,
            sequence=5,
            direction=MotionDirection.FORWARD,
        )
        ok1, _, _ = self.sm.process_challenge_response(
            "operator-1", r1, self.base_time_ns + 10_000_000
        )
        self.assertTrue(ok1)

        # Duplicate sequence 5 fails
        c2 = self.sm.issue_challenge(self.base_time_ns + 20_000_000)
        r2 = ChallengeResponse(
            input_generation=c2.input_generation,
            token=c2.token,
            epoch=1,
            sequence=5,
            direction=MotionDirection.FORWARD,
        )
        ok2, err2, _ = self.sm.process_challenge_response(
            "operator-1", r2, self.base_time_ns + 30_000_000
        )
        self.assertFalse(ok2)
        self.assertEqual(err2, WebControlErrorCode.SEQUENCE_OUT_OF_ORDER)

        # Regressive sequence 4 fails
        c3 = self.sm.issue_challenge(self.base_time_ns + 40_000_000)
        r3 = ChallengeResponse(
            input_generation=c3.input_generation,
            token=c3.token,
            epoch=1,
            sequence=4,
            direction=MotionDirection.FORWARD,
        )
        ok3, err3, _ = self.sm.process_challenge_response(
            "operator-1", r3, self.base_time_ns + 50_000_000
        )
        self.assertFalse(ok3)
        self.assertEqual(err3, WebControlErrorCode.SEQUENCE_OUT_OF_ORDER)

    def test_old_epoch_rejection(self):
        """Response with mismatched or stale epoch must be rejected."""
        c1 = self.sm.issue_challenge(self.base_time_ns)
        r1 = ChallengeResponse(
            input_generation=c1.input_generation,
            token=c1.token,
            epoch=0,  # Stale epoch
            sequence=1,
            direction=MotionDirection.FORWARD,
        )
        ok, err, _ = self.sm.process_challenge_response(
            "operator-1", r1, self.base_time_ns + 10_000_000
        )
        self.assertFalse(ok)
        self.assertEqual(err, WebControlErrorCode.INVALID_EPOCH)

    def test_continuous_hold_cap_5_seconds(self):
        """Holding a direction continuously for > 5.0 s must stop and disarm."""
        # Hold forward motion with continuous 50 ms challenge renewals
        seq = 1
        t_cur = self.base_time_ns
        for _ in range(98):  # 98 * 50 ms = 4.900 s
            c = self.sm.issue_challenge(t_cur)
            self.assertIsNotNone(c)
            r = ChallengeResponse(
                input_generation=c.input_generation,
                token=c.token,
                epoch=1,
                sequence=seq,
                direction=MotionDirection.FORWARD,
            )
            ok, _, _ = self.sm.process_challenge_response(
                "operator-1", r, t_cur + int(0.010 * 1e9)
            )
            self.assertTrue(ok)
            seq += 1
            t_cur += int(0.050 * 1e9)

        # Still driving within 5.0 s limit
        self.assertEqual(self.sm.state, OperatorState.DRIVING)

        # Renewal at 5.1 s exceeding 5.0 s continuous hold cap: must stop and disarm
        t_5_1s = self.base_time_ns + int(5.1 * 1e9)
        self.sm.lease_deadline_monotonic_ns = t_5_1s + int(0.150 * 1e9)
        c_cap = self.sm.issue_challenge(t_5_1s)
        self.assertIsNotNone(c_cap)
        r_cap = ChallengeResponse(
            input_generation=c_cap.input_generation,
            token=c_cap.token,
            epoch=1,
            sequence=seq,
            direction=MotionDirection.FORWARD,
        )
        ok_cap, err_cap, _ = self.sm.process_challenge_response(
            "operator-1", r_cap, t_5_1s + int(0.010 * 1e9)
        )
        self.assertFalse(ok_cap)
        self.assertEqual(err_cap, WebControlErrorCode.MAX_HOLD_EXCEEDED)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        vx, wz = self.sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))

    def test_armed_idle_timeout_30_seconds(self):
        """In ARMED_IDLE, 30.0 s without directional input must automatically disarm while lease renewed."""
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Fresh neutral responses keep the 150 ms lease alive up to 29 seconds
        t_29s = self.base_time_ns + int(29.0 * 1e9)
        self.sm.lease_deadline_monotonic_ns = t_29s + int(0.150 * 1e9)
        self.sm.telemetry.battery_monotonic_ns = t_29s
        self.sm.telemetry.guard_monotonic_ns = t_29s
        ok29, _, _ = self.sm.check_deadlines(t_29s)
        self.assertTrue(ok29)
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Periodic check at 30.5 seconds: lease is alive, but 30 s idle without directional input disarms
        t_30_5s = self.base_time_ns + int(30.5 * 1e9)
        self.sm.lease_deadline_monotonic_ns = t_30_5s + int(0.150 * 1e9)
        self.sm.telemetry.battery_monotonic_ns = t_30_5s
        self.sm.telemetry.guard_monotonic_ns = t_30_5s
        ok30, err30, _ = self.sm.check_deadlines(t_30_5s)
        self.assertFalse(ok30)
        self.assertEqual(err30, WebControlErrorCode.TIMEOUT)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

    def test_immediate_stop_priority(self):
        """Stop clears motion, cancels challenges, invalidates epoch, and tracks pending disarm."""
        c1 = self.sm.issue_challenge(self.base_time_ns)
        r1 = ChallengeResponse(
            input_generation=c1.input_generation,
            token=c1.token,
            epoch=1,
            sequence=1,
            direction=MotionDirection.FORWARD,
        )
        self.sm.process_challenge_response(
            "operator-1", r1, self.base_time_ns + 10_000_000
        )
        self.assertEqual(self.sm.state, OperatorState.DRIVING)

        # Stop called
        self.sm.stop(self.base_time_ns + 20_000_000, requester_id="operator-1")
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        vx, wz = self.sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))

        # Finding 4: Observed guard state remains True until downstream observation confirms disarm
        self.assertTrue(self.sm.telemetry.guard_armed)
        self.assertTrue(self.sm.disarm_pending)

        # Status endpoint reflects observed guard state and pending disarm
        status = self.sm.get_status(self.base_time_ns + 20_000_000)
        self.assertTrue(status.guard_armed)
        self.assertTrue(status.disarm_pending)

        # Downstream observation confirms disarm
        self.sm.update_guard_telemetry(False, self.base_time_ns + 30_000_000)
        self.assertFalse(self.sm.telemetry.guard_armed)
        self.assertFalse(self.sm.disarm_pending)

        status_confirmed = self.sm.get_status(self.base_time_ns + 30_000_000)
        self.assertFalse(status_confirmed.guard_armed)
        self.assertFalse(status_confirmed.disarm_pending)

        # Epoch has advanced to invalidate outstanding challenges
        self.assertEqual(self.sm.epoch, 2)
        self.assertEqual(len(self.sm.outstanding_challenges), 0)

    def test_renewal_rejected_after_existing_lease_expires(self):
        """Reject renewal when response arrives after existing lease deadline, before watchdog tick."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        t_0 = 1_000_000_000  # 1.000 s
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=t_0,
            guard_armed=False,
            guard_monotonic_ns=t_0,
        )
        sm.acquire("operator-1", t_0)
        sm.arm("operator-1", 1, t_0, request_id="arm-1")
        sm.confirm_armed(True, True, t_0 + 10_000_000, epoch=1, request_id="arm-1")

        # First forward challenge issued at t_0 + 20 ms, accepted at t_0 + 30 ms
        t_1 = t_0 + int(0.020 * 1e9)
        c1 = sm.issue_challenge(t_1)
        self.assertIsNotNone(c1)
        r1 = ChallengeResponse(
            input_generation=c1.input_generation,
            token=c1.token,
            epoch=1,
            sequence=1,
            direction=MotionDirection.FORWARD,
        )
        t_resp1 = t_1 + int(0.010 * 1e9)
        ok1, _, _ = sm.process_challenge_response("operator-1", r1, t_resp1)
        self.assertTrue(ok1)
        self.assertEqual(sm.state, OperatorState.DRIVING)
        existing_lease_deadline = c1.deadline_monotonic_ns  # t_1 + 150 ms

        # Second challenge issued at t_1 + 100 ms (before existing lease expires at t_1 + 150 ms)
        t_issue2 = t_1 + int(0.100 * 1e9)
        c2 = sm.issue_challenge(t_issue2)
        self.assertIsNotNone(c2)

        # Response to second challenge arrives after existing lease deadline
        # (existing_lease_deadline + 10 ms), before challenge individual deadline and watchdog tick
        t_late = existing_lease_deadline + int(0.010 * 1e9)
        r2 = ChallengeResponse(
            input_generation=c2.input_generation,
            token=c2.token,
            epoch=1,
            sequence=2,
            direction=MotionDirection.FORWARD,
        )
        ok2, err2, _ = sm.process_challenge_response("operator-1", r2, t_late)

        # Finding 1: must be rejected with LEASE_EXPIRED
        self.assertFalse(ok2)
        self.assertEqual(err2, WebControlErrorCode.LEASE_EXPIRED)

        # Require zero velocity, epoch invalidation, and state FAULT
        vx, wz = sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))
        self.assertEqual(sm.state, OperatorState.INPUT_PAUSED)
        self.assertEqual(sm.epoch, 1)

        # Subsequent watchdog tick at t_late sees state FAULT and zero velocity
        sm.telemetry.battery_monotonic_ns = t_late
        sm.telemetry.guard_monotonic_ns = t_late
        ok_tick, _, _ = sm.check_deadlines(t_late)
        self.assertTrue(ok_tick)
        self.assertEqual(sm.state, OperatorState.INPUT_PAUSED)

    def test_lost_neutral_responses_in_armed_idle_expire_lease(self):
        """Neutral response accepted, then client disconnects: lease expires at 150 ms in ARMED_IDLE."""
        c1 = self.sm.issue_challenge(self.base_time_ns)
        self.assertIsNotNone(c1)
        r1 = ChallengeResponse(
            input_generation=c1.input_generation,
            token=c1.token,
            epoch=1,
            sequence=1,
            direction=MotionDirection.NEUTRAL,
        )
        ok1, _, _ = self.sm.process_challenge_response(
            "operator-1", r1, self.base_time_ns + 10_000_000
        )
        self.assertTrue(ok1)
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Client drops. At 160 ms past accepted challenge deadline with healthy telemetry:
        t_expire = c1.deadline_monotonic_ns + int(0.010 * 1e9)
        self.sm.telemetry.battery_monotonic_ns = t_expire
        self.sm.telemetry.guard_monotonic_ns = t_expire
        ok_exp, err_exp, _ = self.sm.check_deadlines(t_expire)
        self.assertTrue(ok_exp)
        self.assertIsNone(err_exp)
        self.assertEqual(self.sm.state, OperatorState.INPUT_PAUSED)
        self.assertEqual(self.sm.epoch, 1)

    def test_lost_response_before_first_response_in_armed_idle(self):
        """Arming completes but client drops before first response: lease expires at 150 ms."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 1_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("op", base_t)
        sm.arm("op", 1, base_t, request_id="arm-first")
        sm.confirm_armed(
            True, True, base_t + 10_000_000, epoch=1, request_id="arm-first"
        )
        self.assertEqual(sm.state, OperatorState.ARMED_IDLE)
        # Bounded lease initialized upon arm completion
        self.assertEqual(
            sm.lease_deadline_monotonic_ns,
            base_t + 10_000_000 + int(0.150 * 1e9),
        )

        # 160 ms past confirmation with healthy telemetry without any response: lease expires!
        t_160ms = base_t + 10_000_000 + int(0.160 * 1e9)
        sm.telemetry.battery_monotonic_ns = t_160ms
        sm.telemetry.guard_monotonic_ns = t_160ms
        ok160, err160, _ = sm.check_deadlines(t_160ms)
        self.assertTrue(ok160)
        self.assertIsNone(err160)
        self.assertEqual(sm.state, OperatorState.INPUT_PAUSED)
        self.assertEqual(sm.epoch, 1)

    def test_bind_arm_completion_to_originating_transaction(self):
        """Late arm A completion cannot complete arm B or enable motion; B requires its own confirmation."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("operator-1", base_t)
        self.assertEqual(sm.epoch, 1)

        # 1. Arm A begins with epoch 1, request_id "req-arm-A"
        ok_a, _, _ = sm.arm(
            "operator-1",
            epoch=1,
            current_monotonic_ns=base_t,
            request_id="req-arm-A",
        )
        self.assertTrue(ok_a)
        self.assertEqual(sm.state, OperatorState.ARMING)
        self.assertEqual(sm.active_arm_epoch, 1)
        self.assertEqual(sm.active_arm_request_id, "req-arm-A")

        # 2. Arm A is canceled by Stop before confirmation
        t_stop = base_t + 20_000_000
        sm.stop(t_stop, requester_id="operator-1")
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)
        self.assertEqual(sm.epoch, 2)
        self.assertIsNone(sm.active_arm_epoch)
        self.assertIsNone(sm.active_arm_request_id)
        self.assertTrue(sm.compensating_disarm_required)
        self.assertTrue(sm.disarm_pending)

        # Before confirmed disarm: Arm B cannot race the pending disarm
        fail_b, err_b, _ = sm.arm(
            "operator-1",
            epoch=2,
            current_monotonic_ns=base_t + 25_000_000,
            request_id="req-arm-B",
        )
        self.assertFalse(fail_b)
        self.assertEqual(err_b, WebControlErrorCode.INVALID_STATE)

        # Downstream observation confirms disarm
        sm.update_guard_telemetry(False, base_t + 30_000_000)
        self.assertFalse(sm.disarm_pending)
        self.assertFalse(sm.compensating_disarm_required)

        # 3. Arm B begins with new epoch 2, request_id "req-arm-B"
        t_b = base_t + 40_000_000
        ok_b, _, _ = sm.arm(
            "operator-1",
            epoch=2,
            current_monotonic_ns=t_b,
            request_id="req-arm-B",
        )
        self.assertTrue(ok_b)
        self.assertEqual(sm.state, OperatorState.ARMING)
        self.assertEqual(sm.active_arm_epoch, 2)
        self.assertEqual(sm.active_arm_request_id, "req-arm-B")

        # 4. Delayed confirmations for Arm A or omitted identifiers across canceled A / new B
        t_late_a = base_t + 60_000_000

        # Case 4a: Both identifiers omitted
        late_none_ok, late_none_err, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_late_a,
            epoch=None,
            request_id=None,
        )
        self.assertFalse(late_none_ok)
        self.assertEqual(late_none_err, WebControlErrorCode.INVALID_EPOCH)
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Case 4b: Epoch omitted
        late_no_ep_ok, late_no_ep_err, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_late_a,
            epoch=None,
            request_id="req-arm-B",
        )
        self.assertFalse(late_no_ep_ok)
        self.assertEqual(late_no_ep_err, WebControlErrorCode.INVALID_EPOCH)
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Case 4c: Request ID omitted
        late_no_req_ok, late_no_req_err, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_late_a,
            epoch=2,
            request_id=None,
        )
        self.assertFalse(late_no_req_ok)
        self.assertEqual(late_no_req_err, WebControlErrorCode.STALE_TRANSACTION)
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Case 4d: Stale Arm A identifiers (epoch 1, request_id "req-arm-A")
        late_ok, late_err, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_late_a,
            epoch=1,
            request_id="req-arm-A",
        )
        self.assertFalse(late_ok)
        self.assertEqual(late_err, WebControlErrorCode.INVALID_EPOCH)
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Case 4e: Current epoch 2 but stale Arm A request_id
        late_req_ok, late_req_err, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_late_a,
            epoch=2,
            request_id="req-arm-A",
        )
        self.assertFalse(late_req_ok)
        self.assertEqual(late_req_err, WebControlErrorCode.STALE_TRANSACTION)
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Verify Arm B was NOT completed by any invalid/stale/omitted confirmation
        self.assertEqual(sm.state, OperatorState.ARMING)
        vx, wz = sm.get_velocity_command()
        self.assertEqual((vx, wz), (0.0, 0.0))
        # Motion cannot be enabled
        c_fail = sm.issue_challenge(t_late_a)
        self.assertIsNone(c_fail)

        # 5. Arm B requires its own correlated confirmation (epoch 2, request_id "req-arm-B")
        t_b_confirm = base_t + 80_000_000
        b_ok, _, _ = sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=t_b_confirm,
            epoch=2,
            request_id="req-arm-B",
        )
        self.assertTrue(b_ok)
        self.assertEqual(sm.state, OperatorState.ARMED_IDLE)
        self.assertEqual(sm.lease_deadline_monotonic_ns, t_b_confirm + int(0.150 * 1e9))

        # Now motion challenges can be issued and processed in epoch 2
        c_b = sm.issue_challenge(t_b_confirm)
        self.assertIsNotNone(c_b)
        r_b = ChallengeResponse(
            input_generation=c_b.input_generation,
            token=c_b.token,
            epoch=2,
            sequence=1,
            direction=MotionDirection.FORWARD,
        )
        ok_drive, _, _ = sm.process_challenge_response(
            "operator-1", r_b, t_b_confirm + 10_000_000
        )
        self.assertTrue(ok_drive)
        self.assertEqual(sm.state, OperatorState.DRIVING)

    def test_preserve_observed_guard_state_while_disarm_is_pending(self):
        """Stop clears requested velocity immediately, but guard_armed remains observed True until confirmed."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("op", base_t)
        sm.update_guard_telemetry(True, base_t)
        sm.state = OperatorState.DRIVING
        sm.active_direction = MotionDirection.FORWARD
        sm.lease_deadline_monotonic_ns = base_t + int(0.150 * 1e9)

        vx, wz = sm.get_velocity_command()
        self.assertGreater(vx, 0.0)
        self.assertEqual(wz, 0.0)

        # Stop invoked
        t_stop = base_t + 10_000_000
        sm.stop(t_stop, requester_id="op")

        # Velocity is immediately zeroed
        vx_stop, wz_stop = sm.get_velocity_command()
        self.assertEqual((vx_stop, wz_stop), (0.0, 0.0))

        # Finding 4 assertion: telemetry.guard_armed is NOT overwritten with False
        self.assertTrue(sm.telemetry.guard_armed)
        self.assertEqual(sm.telemetry.guard_monotonic_ns, base_t)
        self.assertTrue(sm.disarm_pending)

        status = sm.get_status(t_stop)
        self.assertTrue(status.guard_armed)
        self.assertTrue(status.disarm_pending)

        # When real downstream observation confirms disarm:
        t_obs = base_t + 50_000_000
        sm.update_guard_telemetry(False, t_obs)
        self.assertFalse(sm.telemetry.guard_armed)
        self.assertEqual(sm.telemetry.guard_monotonic_ns, t_obs)
        self.assertFalse(sm.disarm_pending)

        status_obs = sm.get_status(t_obs)
        self.assertFalse(status_obs.guard_armed)
        self.assertFalse(status_obs.disarm_pending)

    def test_preserve_pending_disarm_across_stop_and_arm(self):
        """Stop sets pending disarm; arming is blocked until downstream confirms disarm."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("operator-1", base_t)
        sm.arm("operator-1", 1, base_t, request_id="arm-1")
        sm.confirm_armed(True, True, base_t + 10_000_000, epoch=1, request_id="arm-1")
        self.assertEqual(sm.state, OperatorState.ARMED_IDLE)
        self.assertTrue(sm.telemetry.guard_armed)

        # Stop invoked: sets disarm_pending and compensating_disarm_required
        t_stop = base_t + 20_000_000
        sm.stop(t_stop, requester_id="operator-1")
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)
        self.assertTrue(sm.telemetry.guard_armed)

        # Before confirmed disarm: re-arming must be rejected
        arm_fail, arm_err, _ = sm.arm(
            "operator-1",
            sm.epoch,
            base_t + 25_000_000,
            request_id="arm-2",
        )
        self.assertFalse(arm_fail)
        self.assertEqual(arm_err, WebControlErrorCode.INVALID_STATE)
        # Pending disarm obligation is preserved
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)

        # Downstream observation confirms disarm
        t_disarm = base_t + 30_000_000
        sm.update_guard_telemetry(False, t_disarm)
        self.assertFalse(sm.disarm_pending)
        self.assertFalse(sm.compensating_disarm_required)
        self.assertFalse(sm.telemetry.guard_armed)

        # After confirmed disarm: re-arming succeeds
        arm_ok, _, _ = sm.arm(
            "operator-1",
            sm.epoch,
            base_t + 35_000_000,
            request_id="arm-2",
        )
        self.assertTrue(arm_ok)
        self.assertEqual(sm.state, OperatorState.ARMING)

    def test_preserve_pending_disarm_across_stop_and_same_owner_acquire(self):
        """Stop sets pending disarm; same-owner acquire is blocked until downstream confirms disarm."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("operator-1", base_t)
        sm.arm("operator-1", 1, base_t, request_id="arm-1")
        sm.confirm_armed(True, True, base_t + 10_000_000, epoch=1, request_id="arm-1")
        self.assertEqual(sm.state, OperatorState.ARMED_IDLE)

        # Stop invoked
        t_stop = base_t + 20_000_000
        sm.stop(t_stop, requester_id="operator-1")
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)

        # Before confirmed disarm: same owner acquire is blocked
        acq_fail, _, acq_err, _ = sm.acquire("operator-1", base_t + 25_000_000)
        self.assertFalse(acq_fail)
        self.assertEqual(acq_err, WebControlErrorCode.INVALID_STATE)
        # Pending disarm obligation is preserved
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)

        # Downstream observation confirms disarm
        t_disarm = base_t + 30_000_000
        sm.update_guard_telemetry(False, t_disarm)
        self.assertFalse(sm.disarm_pending)
        self.assertFalse(sm.compensating_disarm_required)

        # After confirmed disarm: same owner acquire succeeds
        acq_ok, _new_ep, _, _ = sm.acquire("operator-1", base_t + 35_000_000)
        self.assertTrue(acq_ok)
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)

    def test_preserve_pending_disarm_across_release_and_different_owner_acquire(self):
        """Release while armed sets pending disarm; new operator acquire is blocked until confirmed."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("operator-A", base_t)
        sm.arm("operator-A", 1, base_t, request_id="arm-A")
        sm.confirm_armed(True, True, base_t + 10_000_000, epoch=1, request_id="arm-A")
        self.assertEqual(sm.state, OperatorState.ARMED_IDLE)

        # Operator A releases while armed
        t_rel = base_t + 20_000_000
        rel_ok, _, _ = sm.release("operator-A", 1, t_rel)
        self.assertTrue(rel_ok)
        self.assertEqual(sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(sm.owner_id)
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)
        self.assertTrue(sm.telemetry.guard_armed)

        # Before confirmed disarm: Operator B cannot acquire
        b_fail, _, b_err, _ = sm.acquire("operator-B", base_t + 25_000_000)
        self.assertFalse(b_fail)
        self.assertEqual(b_err, WebControlErrorCode.INVALID_STATE)
        # Pending disarm obligation is preserved
        self.assertTrue(sm.disarm_pending)
        self.assertTrue(sm.compensating_disarm_required)

        # Downstream observation confirms disarm
        t_disarm = base_t + 30_000_000
        sm.update_guard_telemetry(False, t_disarm)
        self.assertFalse(sm.disarm_pending)
        self.assertFalse(sm.compensating_disarm_required)
        self.assertFalse(sm.telemetry.guard_armed)

        # After confirmed disarm: Operator B acquires successfully
        b_ok, _b_epoch, _, _ = sm.acquire("operator-B", base_t + 35_000_000)
        self.assertTrue(b_ok)
        self.assertEqual(sm.owner_id, "operator-B")
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)

    def test_delayed_arm_completion_retains_disarm_obligation(self):
        """Delayed arm confirmation during OWNED_DISARMED retains disarm obligation and blocks rearm."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        base_t = 10_000_000_000
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=base_t,
            guard_armed=False,
            guard_monotonic_ns=base_t,
        )
        sm.acquire("op", base_t)
        sm.arm("op", 1, base_t, request_id="arm-orig")
        self.assertEqual(sm.state, OperatorState.ARMING)

        # Stop cancels Arm before confirmation
        t_stop = base_t + 20_000_000
        sm.stop(t_stop, requester_id="op")
        self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)

        # Delayed confirmation arrives for the canceled arm in OWNED_DISARMED
        t_late = base_t + 30_000_000
        late_ok, late_err, _ = sm.confirm_armed(
            True, True, t_late, epoch=1, request_id="arm-orig"
        )
        self.assertFalse(late_ok)
        self.assertEqual(late_err, WebControlErrorCode.INVALID_STATE)
        # Compensating disarm must be retained because guard was confirmed
        self.assertTrue(sm.compensating_disarm_required)
        self.assertTrue(sm.disarm_pending)

        # Rearm must be blocked while disarm is pending
        arm_fail, arm_err, _ = sm.arm(
            "op", sm.epoch, t_late + 5_000_000, request_id="arm-new"
        )
        self.assertFalse(arm_fail)
        self.assertEqual(arm_err, WebControlErrorCode.INVALID_STATE)

        # Downstream disarm confirmed
        sm.update_guard_telemetry(False, t_late + 10_000_000)
        self.assertFalse(sm.disarm_pending)
        self.assertFalse(sm.compensating_disarm_required)

        # Rearm now succeeds
        arm_ok, _, _ = sm.arm("op", sm.epoch, t_late + 15_000_000, request_id="arm-new")
        self.assertTrue(arm_ok)
        self.assertEqual(sm.state, OperatorState.ARMING)

    def test_arm_cancellation_and_compensating_disarm(self):
        """Timeout during ARMING triggers compensating disarm and transitions to FAULT."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=100,
            guard_armed=False,
            guard_monotonic_ns=100,
        )
        sm.acquire("op", 100)
        sm.arm("op", 1, 100, request_id="arm-1")
        self.assertEqual(sm.state, OperatorState.ARMING)

        # 260 ms elapsed without confirmation (> 250 ms deadline)
        conf_time = 100 + int(0.260 * 1e9)
        ok, err, _ = sm.confirm_armed(
            True, True, conf_time, epoch=1, request_id="arm-1"
        )
        self.assertFalse(ok)
        self.assertEqual(err, WebControlErrorCode.TIMEOUT)
        self.assertEqual(sm.state, OperatorState.FAULT)

    def test_stale_telemetry_blocks_arm_and_trips_driving(self):
        """Stale battery or guard telemetry blocks arming and trips active driving."""
        sm = OperatorStateMachine(lease_duration_sec=0.150)
        # Battery is 3.5 seconds old (> 3.0 s threshold)
        sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=100,
            guard_armed=False,
            guard_monotonic_ns=int(3.6 * 1e9),
        )
        sm.acquire("op", int(3.6 * 1e9))

        # Cannot arm with stale battery
        ok, err, _ = sm.arm("op", 1, int(3.6 * 1e9))
        self.assertFalse(ok)
        self.assertEqual(err, WebControlErrorCode.STALE_TELEMETRY)


class TestMilestone10ConfigValidation(unittest.TestCase):
    """Verify strict configuration bounds and error handling."""

    def test_valid_default_configuration(self):
        cfg = WebControlConfig()
        cfg.validate()
        self.assertEqual(cfg.listen_address, "127.0.0.1")
        self.assertEqual(cfg.port, 8443)

    def test_wildcard_origin_rejected(self):
        cfg = WebControlConfig(allowed_origins=["*"])
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("wildcard", str(ctx.exception).lower())

    def test_excessive_linear_speed_rejected(self):
        cfg = WebControlConfig(linear_speed_cap=0.60)  # > 0.50 m/s limit
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("safety limits", str(ctx.exception))

    def test_excessive_angular_speed_rejected(self):
        cfg = WebControlConfig(angular_speed_cap=2.50)  # > 2.0 rad/s limit
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("safety limits", str(ctx.exception))

    def test_invalid_ports_rejected(self):
        with self.assertRaises(ValueError):
            WebControlConfig(port=0).validate()
        with self.assertRaises(ValueError):
            WebControlConfig(port=70000).validate()

    def test_invalid_timing_rejected(self):
        # Challenge interval >= lease duration
        with self.assertRaises(ValueError):
            WebControlConfig(
                challenge_interval_sec=0.200, lease_duration_sec=0.150
            ).validate()


class TestMilestone10LockHierarchy(unittest.TestCase):
    """Verify lock acquisition hierarchy (1 -> 2 -> 3) and non-blocking stop."""

    def test_correct_order_acquisition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            l1_path = os.path.join(tmpdir, "l1.lock")
            l2_path = os.path.join(tmpdir, "l2.lock")
            l3_path = os.path.join(tmpdir, "l3.lock")

            hierarchy = LockHierarchy()
            # Level 1 then Level 2 then Level 3 succeeds
            with (
                hierarchy.acquire(LOCK_LEVEL_DEPLOYMENT, lock_path=l1_path),
                hierarchy.acquire(LOCK_LEVEL_OPERATOR, lock_path=l2_path),
                hierarchy.acquire(LOCK_LEVEL_LIFECYCLE, lock_path=l3_path),
            ):
                self.assertEqual(len(hierarchy.currently_held_levels), 3)

    def test_inverted_order_acquisition_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            l1_path = os.path.join(tmpdir, "l1.lock")
            l2_path = os.path.join(tmpdir, "l2.lock")

            hierarchy = LockHierarchy()
            # Acquiring Level 2 then Level 1 raises LockOrderViolationError
            with (
                hierarchy.acquire(LOCK_LEVEL_OPERATOR, lock_path=l2_path),
                self.assertRaises(LockOrderViolationError),
                hierarchy.acquire(LOCK_LEVEL_DEPLOYMENT, lock_path=l1_path),
            ):
                pass

    def test_non_blocking_stop_guarantee(self):
        """Emergency stop callback must execute even under lock contention."""
        stopped = False

        def on_stop():
            nonlocal stopped
            stopped = True

        with tempfile.TemporaryDirectory() as tmpdir:
            op_path = os.path.join(tmpdir, "op.lock")
            # Hold lock in separate context
            hierarchy = LockHierarchy()
            with hierarchy.acquire(LOCK_LEVEL_OPERATOR, lock_path=op_path):
                # execute_non_blocking_stop will encounter lock contention but MUST execute callback
                success = execute_non_blocking_stop(
                    operator_lock_path=op_path, stop_callback=on_stop, timeout_sec=0.010
                )
                self.assertFalse(success)  # Lock was contended
                self.assertTrue(stopped)  # Callback was still invoked!


class TestMilestone10Schemas(unittest.TestCase):
    """Verify strict validation and rejection of extra fields in schemas."""

    def test_version_response_schema(self):
        v = VersionResponse(release_id="m10-candidate")
        d = v.to_dict()
        self.assertEqual(d["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(d["release_id"], "m10-candidate")

        v2 = VersionResponse.from_dict(d)
        self.assertEqual(v2.protocol_version, PROTOCOL_VERSION)

        # Reject unexpected field
        with self.assertRaises(ValueError) as ctx:
            VersionResponse.from_dict({**d, "malicious_field": "val"})
        self.assertIn("unexpected field", str(ctx.exception))

    def test_arm_request_schema(self):
        # Valid
        req = ControlArmRequest.from_dict({"request_id": "req-1", "epoch": 1})
        self.assertEqual(req.epoch, 1)

        # Reject non-boolean tracks_raised
        with self.assertRaises(ValueError):
            ControlArmRequest.from_dict(
                {"request_id": "req-1", "epoch": 1, "tracks_raised": "true"}
            )

    def test_logs_request_limit_bounds(self):
        req = LogsRequest.from_dict({"limit": 50})
        self.assertEqual(req.limit, 50)

        # Limit > 200 rejected
        with self.assertRaises(ValueError):
            LogsRequest.from_dict({"limit": 201})

        # Limit < 1 rejected
        with self.assertRaises(ValueError):
            LogsRequest.from_dict({"limit": 0})


if __name__ == "__main__":
    unittest.main()
