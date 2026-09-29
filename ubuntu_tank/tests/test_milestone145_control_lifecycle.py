"""Tests for Milestone 14.5 unified Take/Release control lifecycle, Start/Stop, and inactivity timeout.

Validates:
1. Unified Take control (starts controller if needed, acquires without arming).
2. Release control: owner-scoped zero/disarm, controller shutdown via lifecycle client,
   confirmed inactive before handoff; blocks new acquisitions (DEPLOYMENT_BUSY) during release
   or after shutdown failure.
3. Start (POST /api/v1/control/start): owner-only explicit idempotent arming with preflight/neutral checks;
   no tracks-raised field; duplicate request_id deduplicates; neutral and preflight gates enforced.
4. Stop (POST /api/v1/control/stop): immediate zero/disarm, retains ownership and running controller;
   independent of ownership.
5. Inactivity timeout: configurable control_idle_timeout_sec in web.yaml, enforced in deadline loop,
   resets only on fresh accepted owner action (Start, Stop, direction changes; held intent, challenges,
   and status polling do NOT reset); automatic release and controller shutdown on expiry.
6. Status reporting: status_revision, session_id, control_idle_timeout_sec, remaining_inactivity_sec,
   release_progress, last_release_reason, last_released_session_id.
7. Removed endpoints: POST /api/v1/controller/stop and POST /api/v1/control/arm reject with 404/405.
8. Protocol compatibility: Protocol 3.0.0, Schema 3; older clients (protocol 2.0.0 or missing) rejected with 422.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

for package in (
    "ubuntu_tank_protocol",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_operator",
    "ubuntu_tank_web",
    "ubuntu_tank_teleop",
    "ubuntu_tank_bringup",
):
    p = str(Path(__file__).resolve().parents[1] / "src" / package)
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
from ubuntu_tank_operator.ipc_server import OperatorIpcServer
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.constants import PROTOCOL_VERSION
from ubuntu_tank_protocol.enums import (
    MotionDirection,
    OperatorState,
    ReleaseReason,
    WebControlErrorCode,
)
from ubuntu_tank_protocol.lifecycle_client import LifecycleClient
from ubuntu_tank_protocol.schemas import ChallengeResponse, TelemetrySnapshot
from ubuntu_tank_supervisor.lifecycle_service import LifecycleHelperService
from ubuntu_tank_web.app import create_app


def complete_acquisition(client: TestClient, path: str, **kwargs) -> dict:
    """Helper to poll asynchronous take_control / acquire to completion."""
    kwargs["json"] = {"protocol_version": PROTOCOL_VERSION, **kwargs.get("json", {})}
    res = client.post(path, **kwargs)
    if res.status_code != 200:
        return res.json()
    data = res.json()
    if not data.get("success") or not data.get("operation_id"):
        return data

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        op_res = client.get(
            f"/api/v1/operations/{data['operation_id']}",
            params={"operation_token": data.get("operation_token")},
        ).json()
        if op_res.get("status") != "pending":
            return {**data, **op_res}
        time.sleep(0.02)
    raise TimeoutError("Acquisition did not complete within timeout")


class TestMilestone145ControlLifecycle(unittest.TestCase):
    """Test M14.5 unified Take/Release control lifecycle and Start/Stop operations."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.op_sock = os.path.join(self.tmp_dir.name, "operator.sock")
        self.lc_sock = os.path.join(self.tmp_dir.name, "lifecycle.sock")

        self.service_state = "inactive"

        def fake_runner(args):
            if "stop" in args:
                self.service_state = "inactive"
                return (0, "", "")
            if "start" in args:
                self.service_state = "active"
                return (0, "", "")
            if "is-active" in args:
                return (0, self.service_state, "")
            return (0, "", "")

        self.lc_service = LifecycleHelperService(
            socket_path=self.lc_sock,
            allowed_uids={os.getuid()},
            process_runner=fake_runner,
            log_runner=lambda limit: [],
        )
        self.lc_service.start()

        self.sm = OperatorStateMachine(
            release_id="m14-5-test",
            control_idle_timeout_sec=300.0,
        )
        self.now_ns = time.monotonic_ns()
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.4,
            battery_monotonic_ns=self.now_ns,
            guard_armed=False,
            guard_monotonic_ns=self.now_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=self.now_ns,
        )

        self.op_server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.op_sock,
            allowed_uids=[os.getuid()],
            lifecycle_client=LifecycleClient(self.lc_sock),
        )
        self.op_server.start()

        self.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
            control_idle_timeout_sec=300.0,
        )
        self.app = create_app(
            config=self.config,
            operator_socket_path=self.op_sock,
            lifecycle_socket_path=self.lc_sock,
        )
        self.client = TestClient(self.app)
        self.headers = {"Origin": "https://127.0.0.1:8443"}

    def tearDown(self):
        self.client.close()
        self.op_server.stop()
        self.lc_service.stop()
        self.tmp_dir.cleanup()

    def _bound_owner(self):
        result = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "alice", "request_id": "acquire-review"},
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            self.app.state.operator_relay.claim_owner_binding(
                "alice", result["epoch"], result["bind_token"], "review-websocket"
            )[0]
        )
        return result

    def _post_control(self, action, **body):
        return self.client.post(
            "/api/v1/control/" + action,
            headers=self.headers,
            json=body
            if action == "stop"
            else {"protocol_version": PROTOCOL_VERSION, **body},
        ).json()

    def _wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_expired_session_cannot_rearm_during_or_after_failed_shutdown(self):
        self._assert_release_blocks_start(expiry=True)

    def test_explicit_release_cannot_rearm_during_or_after_failed_shutdown(self):
        self._assert_release_blocks_start(expiry=False)

    def _assert_release_blocks_start(self, expiry):
        self._bound_owner()
        gate = threading.Event()
        entered = threading.Event()

        def stalled_stop():
            entered.set()
            gate.wait(3)
            return False, "active", "transient stop failure"

        lifecycle = self.op_server.acquisition.lifecycle
        with patch.object(lifecycle, "stop_controller", stalled_stop):
            try:
                if expiry:
                    self.sm.check_deadlines(self.sm.inactivity_deadline_monotonic_ns)
                else:
                    client = self.app.state.operator_relay.get_owner_client()
                    result = client._send_request(
                        {"action": "release_control", "request_id": "release"}
                    )
                    self.assertEqual(result["status"], "pending")
                self.assertTrue(entered.wait(1))
                for state in ("stopping_controller", "shutdown_failed"):
                    if state == "shutdown_failed":
                        gate.set()
                        self._wait_until(
                            lambda state=state: self.sm.release_progress == state
                        )
                    now = time.monotonic_ns()
                    self.sm.update_battery_telemetry(12.4, now)
                    self.sm.update_guard_telemetry(False, now)
                    self.sm.update_odom_telemetry(0.0, 0.0, now)
                    result = self._post_control(
                        "start", epoch=self.sm.epoch, request_id=state
                    )
                    self.assertFalse(result["success"], result)
                    self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
                    self.assertIsNone(self.sm.issue_challenge(now))
                    self.assertEqual(self.sm.get_velocity_command(), (0.0, 0.0))
            finally:
                gate.set()

    def test_release_failure_can_be_retried_without_restarting_services(self):
        self._assert_release_retry(async_result=False)

    def test_polled_release_failure_can_be_retried_without_restarting_services(self):
        self._assert_release_retry(async_result=True)

    def _assert_release_retry(self, async_result):
        self._bound_owner()
        relay = self.app.state.operator_relay
        client = relay.get_owner_client()
        original_send = client._send_request

        def send(req, **kwargs):
            if async_result and req.get("action") == "release":
                req = {**req, "action": "release_control"}
            return original_send(req, **kwargs)

        lifecycle = self.op_server.acquisition.lifecycle
        with (
            patch.object(client, "_send_request", send),
            patch.object(
                lifecycle,
                "stop_controller",
                return_value=(False, "active", "transient failure"),
            ),
        ):
            result = self._post_control("release", request_id="failed-release")
            if result["status"] == "pending":
                self._wait_until(lambda: self.sm.release_progress == "shutdown_failed")
                result = self.client.get(
                    "/api/v1/operations/" + result["operation_id"],
                    params={"operation_token": result["operation_token"]},
                ).json()
            self.assertEqual(result["status"], "failed")
        self.assertIs(relay.get_owner_client(), client)
        retry = self._post_control("release", request_id="retry-release")
        self.assertEqual(retry["status"], "completed", retry)
        self.assertEqual(self.service_state, "inactive")
        self.assertIsNone(self.sm.owner_id)
        self.assertIsNone(self.sm.release_progress)
        self.sm.update_guard_telemetry(False, time.monotonic_ns())
        self.sm.update_battery_telemetry(12.4, time.monotonic_ns())
        result = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "bob", "request_id": "after-retry"},
        )
        self.assertEqual(result["status"], "completed", result)

    def test_idle_release_allows_reacquisition_with_original_socket_open(self):
        self._bound_owner()
        relay = self.app.state.operator_relay
        original_client = relay.get_owner_client()
        self.sm.check_deadlines(self.sm.inactivity_deadline_monotonic_ns)
        self._wait_until(lambda: self.sm.owner_id is None)
        self.assertEqual(self.service_state, "inactive")
        self.assertIs(relay.get_owner_client(), original_client)
        self.sm.update_guard_telemetry(False, time.monotonic_ns())
        self.sm.update_battery_telemetry(12.4, time.monotonic_ns())
        result = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "bob", "request_id": "after-expiry"},
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.sm.owner_id, "bob")

    def test_runtime_entrypoint_applies_configured_ownership_deadline(self):
        from ubuntu_tank_operator import entrypoint
        from ubuntu_tank_operator.agent_node import OperatorAgentNode

        for timeout in (0.5, 30.0):
            with self.subTest(timeout=timeout):
                path = Path(self.tmp_dir.name) / "runtime-web.yaml"
                path.write_text(f"control_idle_timeout_sec: {timeout}\n")
                checked = []

                def check_tick(node, timeout=timeout, checked=checked):
                    sm = node.state_machine
                    now = time.monotonic_ns()
                    sm.telemetry = TelemetrySnapshot(
                        battery_voltage=12.4,
                        battery_monotonic_ns=now,
                        guard_armed=False,
                        guard_monotonic_ns=now,
                    )
                    self.assertTrue(sm.acquire("configured-owner", now)[0])
                    status = sm.get_status(now)
                    self.assertEqual(status.control_idle_timeout_sec, timeout)
                    self.assertEqual(status.remaining_inactivity_sec, timeout)
                    sm.check_deadlines(now + int(timeout * 1e9) - 1)
                    self.assertIsNone(sm.release_progress)
                    sm.check_deadlines(now + int(timeout * 1e9))
                    self.assertEqual(sm.release_progress, "stopping_controller")
                    checked.append(True)
                    raise KeyboardInterrupt

                with (
                    patch.dict(os.environ, {"UBUNTU_TANK_WEB_CONFIG": str(path)}),
                    patch.object(entrypoint, "rclpy", None),
                    patch.object(entrypoint, "configure_loopback_dds"),
                    patch.object(entrypoint.signal, "signal"),
                    patch.object(OperatorAgentNode, "_timer_tick", check_tick),
                ):
                    self.assertEqual(
                        entrypoint.main(
                            [
                                "--simulation",
                                "--socket-path",
                                str(Path(self.tmp_dir.name) / "runtime.sock"),
                            ]
                        ),
                        0,
                    )
                self.assertEqual(checked, [True])

    def test_stalled_release_does_not_block_independent_stop(self):
        from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

        self._bound_owner()
        gate = threading.Event()

        def stop():
            gate.wait(3)
            self.service_state = "inactive"
            return True, "inactive", None

        with patch.object(
            self.op_server.acquisition.lifecycle, "stop_controller", stop
        ):
            try:
                result = self._post_control("release", request_id="slow-release")
                self.assertEqual(result["status"], "pending")
                with OperatorIpcClient(self.op_sock) as observer:
                    began = time.monotonic()
                    self.assertTrue(observer.stop(timeout_sec=0.4)[0])
                    self.assertLess(time.monotonic() - began, 0.4)
            finally:
                gate.set()
                self._wait_until(lambda: self.sm.owner_id is None)

    def test_disconnected_failed_release_recovers_through_new_take(self):
        self._bound_owner()
        with patch.object(
            self.op_server.acquisition.lifecycle,
            "stop_controller",
            return_value=(False, "active", "temporary failure"),
        ):
            self.assertEqual(
                self._post_control("release", request_id="fail")["status"], "failed"
            )
        self.app.state.operator_relay.close()
        self._wait_until(lambda: self.sm.owner_id is None)
        lifecycle = self.op_server.acquisition.lifecycle
        calls = []
        original_stop = lifecycle.stop_controller
        original_start = lifecycle.start_controller

        def stop():
            calls.append("stop")
            return original_stop()

        def start():
            self.assertEqual(self.service_state, "inactive")
            calls.append("start")
            return original_start()

        with (
            patch.object(lifecycle, "stop_controller", stop),
            patch.object(lifecycle, "start_controller", start),
        ):
            self.sm.update_battery_telemetry(12.4, time.monotonic_ns())
            self.sm.update_guard_telemetry(False, time.monotonic_ns())
            result = complete_acquisition(
                self.client,
                "/api/v1/control/acquire",
                headers=self.headers,
                json={"operator_id": "bob", "request_id": "recover"},
            )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(calls, ["stop", "start"])

    def test_cancel_pending_startup_waits_for_confirmed_shutdown(self):
        gate = threading.Event()
        entered = threading.Event()

        def start():
            entered.set()
            gate.wait(3)
            self.service_state = "active"
            return True, "active", None

        with patch.object(
            self.op_server.acquisition.lifecycle, "start_controller", start
        ):
            op = self._post_control("acquire", request_id="slow", operator_id="alice")
            self.assertTrue(entered.wait(1))
            try:
                release = self._post_control(
                    "release",
                    request_id="cancel",
                    operation_id=op["operation_id"],
                    operation_token=op["operation_token"],
                )
                self.assertEqual(release["status"], "pending")
                self.assertIsNotNone(self.op_server.acquisition.pending_release)
                pending = self.client.get(
                    "/api/v1/operations/" + release["operation_id"],
                    params={"operation_token": release["operation_token"]},
                ).json()
                self.assertEqual(pending["status"], "pending")
            finally:
                gate.set()
            self._wait_until(lambda: self.op_server.acquisition.pending_release is None)
            completed = self.client.get(
                "/api/v1/operations/" + release["operation_id"],
                params={"operation_token": release["operation_token"]},
            ).json()
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(self.service_state, "inactive")
            self.assertIsNone(self.sm.owner_id)

    def test_cancel_setup_stops_an_already_running_controller(self):
        self.service_state = "active"
        self.sm.telemetry.guard_armed = None
        op = self._post_control("acquire", request_id="unready", operator_id="alice")
        release = self._post_control(
            "release",
            request_id="cancel-running",
            operation_id=op["operation_id"],
            operation_token=op["operation_token"],
        )
        self._wait_until(lambda: self.op_server.acquisition.pending_release is None)
        self.assertEqual(self.service_state, "inactive", release)
        self.assertIsNone(self.sm.owner_id)

    def test_observer_stop_does_not_renew_owner_inactivity(self):
        from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

        self._bound_owner()
        deadline = self.sm.inactivity_deadline_monotonic_ns
        with OperatorIpcClient(self.op_sock) as observer:
            self.assertTrue(observer.stop(request_id="observer-stop")[0])
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, deadline)
        self.assertTrue(
            self._post_control(
                "stop",
                request_id="observer-http",
                operator_id="bob",
                epoch=self.sm.epoch,
            )["success"]
        )
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, deadline)
        self.assertTrue(
            self._post_control(
                "stop",
                request_id="owner-http",
                operator_id="alice",
                epoch=self.sm.epoch,
            )["success"]
        )
        self.assertGreater(self.sm.inactivity_deadline_monotonic_ns, deadline)

    def test_version_endpoint_reports_protocol_3(self):
        """Version endpoint reports PROTOCOL_VERSION='3.0.0' and SCHEMA_VERSION=3."""
        res = self.client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["protocol_version"], "3.0.0")
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["api_version"], "v1")

    def test_removed_endpoints_rejected(self):
        """POST /api/v1/controller/stop and POST /api/v1/control/arm are rejected with 404/405."""
        res_stop = self.client.post(
            "/api/v1/controller/stop",
            headers=self.headers,
            json={"request_id": "stop-1"},
        )
        self.assertIn(res_stop.status_code, (404, 405))

        res_arm = self.client.post(
            "/api/v1/control/arm",
            headers=self.headers,
            json={"epoch": 1, "request_id": "arm-1"},
        )
        self.assertIn(res_arm.status_code, (404, 405))

    def test_incompatible_protocol_rejected_before_mutation(self):
        """Requests with missing or older protocol version (e.g. '2.0.0') are rejected with 422."""
        # 1. Acquire with old protocol
        res_old_acq = self.client.post(
            "/api/v1/control/acquire",
            headers=self.headers,
            json={
                "protocol_version": "2.0.0",
                "operator_id": "user1",
                "request_id": "acq-1",
            },
        )
        self.assertEqual(res_old_acq.status_code, 422)

        # 2. Acquire with missing protocol
        res_no_proto = self.client.post(
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "user1", "request_id": "acq-1"},
        )
        self.assertEqual(res_no_proto.status_code, 422)

        # 3. Start with old protocol
        res_old_start = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={"protocol_version": "2.0.0", "epoch": 1, "request_id": "st-1"},
        )
        self.assertEqual(res_old_start.status_code, 422)

        # 4. Release with old protocol
        res_old_rel = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={"protocol_version": "2.0.0", "epoch": 1, "request_id": "rel-1"},
        )
        self.assertEqual(res_old_rel.status_code, 422)

    def test_take_control_starts_controller_and_acquires_without_arming(self):
        """Take control starts the controller if needed, acquires ownership, but never arms."""
        self.assertEqual(self.service_state, "inactive")
        acq = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-alice-1"},
        )
        self.assertTrue(acq.get("success"))
        self.assertEqual(acq.get("status"), "completed")
        epoch = acq.get("epoch")
        self.assertIsNotNone(epoch)
        self.assertIsNotNone(acq.get("bind_token"))

        # Controller was started
        self.assertEqual(self.service_state, "active")

        # State machine is OWNED_DISARMED (never moves automatically)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        self.assertEqual(self.sm.owner_id, "operator-alice")
        self.assertFalse(self.sm.telemetry.guard_armed)

        # Status endpoint reflects new M14.5 fields
        status_res = self.client.get("/api/v1/status").json()
        self.assertEqual(status_res["operator_state"], "OWNED_DISARMED")
        self.assertEqual(status_res["active_owner"], "operator-alice")
        self.assertEqual(status_res["session_id"], "operator-alice")
        self.assertEqual(status_res["control_idle_timeout_sec"], 300.0)
        self.assertIsNotNone(status_res["remaining_inactivity_sec"])
        self.assertGreater(status_res["remaining_inactivity_sec"], 290.0)
        self.assertGreaterEqual(status_res["status_revision"], 1)

    def test_competing_acquisition_rejected_with_deployment_busy(self):
        """Competing acquire is rejected with DEPLOYMENT_BUSY while another owner holds control."""
        complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-alice-1"},
        )
        comp = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-bob", "request_id": "acq-bob-1"},
        )
        self.assertFalse(comp.get("success"))
        self.assertEqual(comp.get("error"), WebControlErrorCode.DEPLOYMENT_BUSY.value)

    def test_start_chassis_operation_and_deduplication(self):
        """POST /api/v1/control/start explicitly arms chassis; duplicate request_id deduplicates."""
        acq = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-1"},
        )
        epoch = acq["epoch"]

        # Non-owner cannot start
        non_owner = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-bad",
                "operator_id": "stranger",
            },
        ).json()
        self.assertFalse(non_owner["success"])
        self.assertEqual(non_owner["error"], WebControlErrorCode.NOT_OWNER.value)

        # Epoch mismatch cannot start
        wrong_epoch = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch + 99,
                "request_id": "st-bad-epoch",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertFalse(wrong_epoch["success"])
        self.assertEqual(wrong_epoch["error"], WebControlErrorCode.INVALID_EPOCH.value)

        # Successful start
        st1 = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-good-1",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(st1["success"])
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Duplicate start with same request_id is idempotent success
        st_dup = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-good-1",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(st_dup["success"])

        # Second start with new request_id when already armed is also idempotent success
        st_already_armed = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-good-2",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(st_already_armed["success"])

    def test_stop_operation_halts_motion_and_retains_owner_and_controller(self):
        """POST /api/v1/control/stop immediately zeroes and disarms, retaining owner and running controller."""
        acq = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-1"},
        )
        epoch = acq["epoch"]

        bound, _, _ = self.app.state.operator_relay.claim_owner_binding(
            "operator-alice", epoch, acq["bind_token"], "ws-session-1"
        )
        self.assertTrue(bound)

        # Arm chassis
        self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-1",
            },
        )
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Issue Stop
        stop_res = self.client.post(
            "/api/v1/control/stop",
            headers=self.headers,
            json={"request_id": "stop-1"},
        ).json()
        self.assertTrue(stop_res["success"])
        self.assertTrue(stop_res["disarmed"])

        # Guard telemetry confirms disarmed
        self.sm.update_guard_telemetry(False, time.monotonic_ns())

        # Operator state retains owner!
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        self.assertEqual(self.sm.owner_id, "operator-alice")

        # Controller service is still running!
        self.assertEqual(self.service_state, "active")

        # Epoch was incremented on stop to prevent replayed motion
        status = self.client.get("/api/v1/status").json()
        new_epoch = status["current_epoch"]
        self.assertEqual(new_epoch, epoch + 1)

        # Can re-arm under new epoch
        rearm = self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": new_epoch,
                "request_id": "st-2",
            },
        ).json()
        self.assertTrue(rearm["success"])
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

    def test_release_control_shuts_down_controller_and_releases_ownership(self):
        """POST /api/v1/control/release zeroes/disarms, shuts down controller, and clears owner."""
        acq = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-1"},
        )
        epoch = acq["epoch"]

        # Non-owner cannot release
        non_owner = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "rel-bad",
                "operator_id": "stranger",
            },
        ).json()
        self.assertFalse(non_owner["success"])
        self.assertEqual(non_owner["error"], WebControlErrorCode.NOT_OWNER.value)

        # Owner release
        rel = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "rel-good",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(rel["success"])
        self.assertEqual(rel["status"], "completed")

        # Controller is confirmed inactive
        self.assertEqual(self.service_state, "inactive")

        # Ownership is cleared
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.sm.owner_id)

        # Status reports release progress and last release reason
        status = self.client.get("/api/v1/status").json()
        self.assertEqual(status["operator_state"], "NO_OWNER")
        self.assertEqual(status["service_state"], "inactive")
        self.assertEqual(
            status["last_release_reason"], ReleaseReason.EXPLICIT_RELEASE.value
        )
        self.assertEqual(status["last_released_session_id"], "operator-alice")

        # Now a new operator can take control cleanly
        acq_bob = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-bob", "request_id": "acq-bob-1"},
        )
        self.assertTrue(acq_bob.get("success"))
        self.assertEqual(self.sm.owner_id, "operator-bob")

    def test_inactivity_timeout_resets_on_fresh_action_only(self):
        """Inactivity timeout resets only on Start, Stop, and direction changes, not polling or neutral."""
        acq = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-1"},
        )
        epoch = acq["epoch"]
        bound, _, _ = self.app.state.operator_relay.claim_owner_binding(
            "operator-alice", epoch, acq["bind_token"], "ws-session-1"
        )
        self.assertTrue(bound)

        initial_deadline = self.sm.inactivity_deadline_monotonic_ns
        self.assertGreater(initial_deadline, 0)

        # 1. Status polling does NOT reset inactivity deadline
        time.sleep(0.01)
        self.client.get("/api/v1/status")
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, initial_deadline)

        # 2. Challenge issuance does NOT reset deadline
        ch = self.sm.issue_challenge(time.monotonic_ns())
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, initial_deadline)

        # 3. Neutral motion does NOT reset deadline
        if ch:
            self.sm.process_challenge_response(
                "operator-alice",
                ChallengeResponse(
                    token=ch.token,
                    sequence=1,
                    direction=MotionDirection.NEUTRAL,
                    epoch=epoch,
                ),
                time.monotonic_ns(),
            )
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, initial_deadline)

        # 4. Fresh Start DOES reset inactivity deadline
        time.sleep(0.01)
        self.client.post(
            "/api/v1/control/start",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "epoch": epoch,
                "request_id": "st-fresh-1",
            },
        )
        new_deadline = self.sm.inactivity_deadline_monotonic_ns
        self.assertGreater(new_deadline, initial_deadline)

        # 5. Non-neutral direction change DOES reset deadline
        ch2 = self.sm.issue_challenge(time.monotonic_ns())
        self.assertIsNotNone(ch2)
        time.sleep(0.01)
        self.sm.process_challenge_response(
            "operator-alice",
            ChallengeResponse(
                token=ch2.token,
                sequence=2,
                direction=MotionDirection.FORWARD,
                epoch=epoch,
            ),
            time.monotonic_ns(),
        )
        dir_deadline = self.sm.inactivity_deadline_monotonic_ns
        self.assertGreater(dir_deadline, new_deadline)

        # 6. Fresh Stop DOES reset inactivity deadline
        time.sleep(0.01)
        self.client.post(
            "/api/v1/control/stop",
            headers=self.headers,
            json={
                "request_id": "stop-fresh-1",
                "operator_id": "operator-alice",
                "epoch": self.sm.epoch,
            },
        )
        stop_deadline = self.sm.inactivity_deadline_monotonic_ns
        self.assertGreater(stop_deadline, dir_deadline)

    def test_inactivity_timeout_triggers_automatic_release_and_shutdown(self):
        """When inactivity timeout expires, operator is released and controller is stopped."""
        # Set a short idle timeout of 0.2s for testing
        self.sm.control_idle_timeout_sec = 0.2
        complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-1"},
        )
        self.assertEqual(self.service_state, "active")
        self.assertEqual(self.sm.owner_id, "operator-alice")

        # Wait for deadline to pass
        deadline_ns = self.sm.inactivity_deadline_monotonic_ns
        future_ns = deadline_ns + int(0.1 * 1e9)

        # Check deadlines
        ok, err, _ = self.sm.check_deadlines(future_ns)
        self.assertFalse(ok)
        self.assertEqual(err, WebControlErrorCode.TIMEOUT)

        # Wait for background idle release thread to complete
        timeout = time.monotonic() + 3.0
        while time.monotonic() < timeout:
            if (
                self.sm.state == OperatorState.NO_OWNER
                and self.service_state == "inactive"
            ):
                break
            time.sleep(0.05)

        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.sm.owner_id)
        self.assertEqual(self.service_state, "inactive")
        self.assertEqual(
            self.sm.last_release_reason, ReleaseReason.CONTROL_IDLE_TIMEOUT.value
        )
        self.assertEqual(self.sm.last_released_session_id, "operator-alice")

    def test_web_control_config_idle_timeout_validation(self):
        """Regression for P1: WebControlConfig handles and validates control_idle_timeout_sec."""
        cfg = WebControlConfig(control_idle_timeout_sec=45.0)
        self.assertEqual(cfg.control_idle_timeout_sec, 45.0)
        cfg.validate()

        cfg_from_dict = WebControlConfig.from_dict({"control_idle_timeout_sec": 60.0})
        self.assertEqual(cfg_from_dict.control_idle_timeout_sec, 60.0)

        for invalid_val in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                WebControlConfig(control_idle_timeout_sec=invalid_val).validate()

    def test_operator_ipc_client_start_method(self):
        """Regression for P1: OperatorIpcClient defines start method to arm the chassis."""
        from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

        client = OperatorIpcClient(socket_path=self.op_sock)
        self.assertTrue(hasattr(client, "start"))
        self.assertTrue(callable(client.start))

    def test_inactivity_deadline_cleared_on_expiry_no_loop_spam(self):
        """Regression for P1: Inactivity expiry resets deadline to 0 and avoids epoch churn."""
        self.sm.control_idle_timeout_sec = 1.0
        now_ns = time.monotonic_ns()
        self.sm.acquire("operator-bob", now_ns)
        self.assertGreater(self.sm.inactivity_deadline_monotonic_ns, 0)

        # Trigger inactivity expiry
        expired_ns = self.sm.inactivity_deadline_monotonic_ns + int(1e8)
        ok1, err1, _ = self.sm.check_deadlines(expired_ns)
        self.assertFalse(ok1)
        self.assertEqual(err1, WebControlErrorCode.TIMEOUT)
        # Verify deadline is immediately zeroed
        self.assertEqual(self.sm.inactivity_deadline_monotonic_ns, 0)

        epoch_after_first = self.sm.epoch

        # Subsequent tick past the original deadline must NOT re-trigger release or churn epoch
        ok2, err2, _ = self.sm.check_deadlines(expired_ns + int(2e7))
        self.assertTrue(ok2)
        self.assertIsNone(err2)
        self.assertEqual(self.sm.epoch, epoch_after_first)

    def test_release_with_completed_operation_id_triggers_full_shutdown(self):
        """Regression for P1: Release with completed operation_id executes full shutdown."""
        res = complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-rel-1"},
        )
        op_id = res["operation_id"]
        op_tok = res["operation_token"]
        self.assertEqual(self.service_state, "active")
        self.assertEqual(self.sm.owner_id, "operator-alice")

        # Release referencing the completed operation_id
        rel_res = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "request_id": "rel-with-op-1",
                "operation_id": op_id,
                "operation_token": op_tok,
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(rel_res.get("success"))

        # Wait for release to complete and controller service to shut down
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.service_state == "inactive" and self.sm.owner_id is None:
                break
            time.sleep(0.05)

        self.assertEqual(self.service_state, "inactive")
        self.assertIsNone(self.sm.owner_id)

    def test_poll_release_operation_via_get_operation(self):
        """Regression for P2: Polling release operation via GET /api/v1/operations/{id}."""
        complete_acquisition(
            self.client,
            "/api/v1/control/acquire",
            headers=self.headers,
            json={"operator_id": "operator-alice", "request_id": "acq-poll-1"},
        )
        self.assertEqual(self.service_state, "active")

        # Release control
        rel_res = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "protocol_version": "3.0.0",
                "request_id": "rel-poll-1",
                "operator_id": "operator-alice",
            },
        ).json()
        self.assertTrue(rel_res.get("success"))
        rel_op_id = rel_res.get("operation_id")
        rel_token = rel_res.get("operation_token")

        if rel_op_id:
            # Poll status with valid token
            poll_res = self.client.get(
                f"/api/v1/operations/{rel_op_id}",
                params={"operation_token": rel_token},
            )
            self.assertEqual(poll_res.status_code, 200)
            data = poll_res.json()
            self.assertIn(data["status"], ("pending", "completed"))

            # Poll with bad token -> 404 (NOT_OWNER)
            bad_token_res = self.client.get(
                f"/api/v1/operations/{rel_op_id}",
                params={"operation_token": "wrong-token"},
            )
            self.assertEqual(bad_token_res.status_code, 404)

            # Poll without token -> 200 public status
            no_token_res = self.client.get(f"/api/v1/operations/{rel_op_id}")
            self.assertEqual(no_token_res.status_code, 200)
            self.assertIsNone(no_token_res.json().get("bind_token"))


if __name__ == "__main__":
    unittest.main()
