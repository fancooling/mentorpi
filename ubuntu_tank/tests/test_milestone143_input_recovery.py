"""Behavioral coverage for configurable input pause and runtime Take control.

Uses deterministic operator time plus real HTTP/Unix sockets with a runtime
lifecycle fixture. These tests do not install software or certify physical stops.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for package in (
    "ubuntu_tank_protocol",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_operator",
    "ubuntu_tank_web",
    "ubuntu_tank_teleop",
    "ubuntu_tank_bringup",
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / package))

from fastapi.testclient import TestClient
from ubuntu_tank_operator.entrypoint import load_operator_config
from ubuntu_tank_operator.ipc_server import OperatorIpcServer
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.enums import MotionDirection, OperatorState
from ubuntu_tank_protocol.ipc_client import OperatorIpcClient
from ubuntu_tank_protocol.schemas import ChallengeResponse, TelemetrySnapshot
from ubuntu_tank_web.app import create_app


def healthy(machine, now, armed=True):
    """Supply fresh runtime health without claiming hardware observation."""
    machine.telemetry = TelemetrySnapshot(
        battery_voltage=12.2,
        battery_monotonic_ns=now,
        guard_armed=armed,
        guard_monotonic_ns=now,
    )


class TestInputRecovery(unittest.TestCase):
    """Exercise expiry, generation isolation and zero-confirmed recovery."""

    def setUp(self):
        self.now = 1_000_000_000
        self.sm = OperatorStateMachine()
        healthy(self.sm, self.now, False)
        self.sm.acquire("owner", self.now)
        self.sm.arm("owner", self.sm.epoch, self.now, "arm")
        self.sm.confirm_armed(True, True, self.now, self.sm.epoch, "arm")
        self.zeros = []
        self.sm.on_zero_required = lambda: self.zeros.append(True)

    def respond(self, challenge, direction, sequence=1, now=None):
        return self.sm.process_challenge_response(
            "owner",
            ChallengeResponse(
                token=challenge.token,
                epoch=challenge.epoch,
                sequence=sequence,
                direction=direction,
                input_generation=challenge.input_generation,
            ),
            self.now if now is None else now,
        )

    def pause(self):
        self.now += 1_000_000_001
        healthy(self.sm, self.now)
        self.sm.check_deadlines(self.now)
        self.assertEqual(self.sm.state, OperatorState.INPUT_PAUSED)

    def test_config_loader_controls_initial_and_challenge_deadlines(self):
        for duration in (0.050, 0.150, 1.0):
            with (
                self.subTest(duration=duration),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "web.yaml"
                path.write_text(
                    json.dumps(
                        {
                            "lease_duration_sec": duration,
                            "challenge_interval_sec": 0.025,
                        }
                    )
                )
                cfg = load_operator_config(str(path))
                machine = OperatorStateMachine(
                    lease_duration_sec=cfg.lease_duration_sec,
                    challenge_interval_sec=cfg.challenge_interval_sec,
                )
                healthy(machine, self.now, False)
                machine.acquire("o", self.now)
                machine.arm("o", 1, self.now, "a")
                machine.confirm_armed(True, True, self.now, 1, "a")
                deadline = self.now + int(duration * 1e9)
                self.assertEqual(machine.lease_deadline_monotonic_ns, deadline)
                self.assertEqual(
                    machine.issue_challenge(self.now).deadline_monotonic_ns, deadline
                )
                healthy(machine, deadline)
                machine.check_deadlines(deadline)
                self.assertEqual(machine.state, OperatorState.ARMED_IDLE)
                machine.check_deadlines(deadline + 1)
                self.assertEqual(machine.state, OperatorState.INPUT_PAUSED)
                self.assertEqual(
                    machine.get_status(deadline).limits["lease_duration_sec"], duration
                )

    def test_invalid_config_rejected_by_real_loader(self):
        for value in (
            True,
            False,
            "1",
            None,
            float("nan"),
            float("inf"),
            0.05,
            1.001,
            -1,
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "web.yaml"
                path.write_text(json.dumps({"lease_duration_sec": value}))
                with self.assertRaises(ValueError):
                    load_operator_config(str(path))

    def test_pause_keeps_arm_owner_and_epoch_but_clears_velocity(self):
        old = self.sm.issue_challenge(self.now)
        self.assertTrue(self.respond(old, MotionDirection.FORWARD)[0])
        epoch = self.sm.epoch
        self.pause()
        self.assertEqual(self.sm.get_velocity_command(), (0, 0))
        self.assertEqual(self.zeros, [True])
        self.assertEqual(self.sm.epoch, epoch)
        self.assertEqual(self.sm.owner_id, "owner")
        self.assertTrue(self.sm.telemetry.guard_armed)
        self.assertFalse(self.sm.disarm_pending)
        self.assertFalse(self.respond(old, MotionDirection.FORWARD, 2)[0])

    def test_neutral_recovery_requires_zero_then_new_generation(self):
        self.pause()
        c = self.sm.issue_challenge(self.now)
        self.assertFalse(self.respond(c, MotionDirection.NEUTRAL)[0])
        self.sm.confirm_zero_delivery(self.now - 1)
        self.assertIsNone(self.sm.zero_confirmed_ns)
        self.sm.confirm_zero_delivery(self.now)
        held = self.sm.issue_challenge(self.now)
        self.assertFalse(self.respond(held, MotionDirection.FORWARD)[0])
        stale = self.sm.issue_challenge(self.now)
        neutral = self.sm.issue_challenge(self.now)
        self.assertTrue(self.respond(neutral, MotionDirection.NEUTRAL)[0])
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)
        self.assertFalse(self.respond(stale, MotionDirection.FORWARD, 2)[0])
        fresh = self.sm.issue_challenge(self.now)
        self.assertTrue(self.respond(fresh, MotionDirection.FORWARD, 3)[0])

    def test_stop_cancels_recovery_and_invalidates_generation(self):
        self.pause()
        self.sm.confirm_zero_delivery(self.now)
        c = self.sm.issue_challenge(self.now)
        self.sm.stop(self.now)
        self.assertFalse(self.respond(c, MotionDirection.NEUTRAL)[0])
        self.assertTrue(self.sm.disarm_pending)

    def test_pause_zero_failure_disarms(self):
        self.pause()
        self.now += 250_000_001
        healthy(self.sm, self.now)
        self.assertFalse(self.sm.check_deadlines(self.now)[0])
        self.assertTrue(self.sm.disarm_pending)

    def test_zero_observations_must_remain_fresh_while_paused(self):
        self.pause()
        for _ in range(40):
            self.now += 50_000_000
            healthy(self.sm, self.now)
            self.sm.confirm_zero_delivery(self.now)
            self.sm.check_deadlines(self.now)
            self.assertEqual(self.sm.state, OperatorState.INPUT_PAUSED)
            self.assertTrue(self.sm.telemetry.guard_armed)
        self.now += 250_000_001
        healthy(self.sm, self.now)
        self.sm.check_deadlines(self.now)
        self.assertEqual(self.sm.state, OperatorState.FAULT)

    def test_unreleased_hold_budget_survives_input_pause(self):
        self.respond(self.sm.issue_challenge(self.now), MotionDirection.FORWARD)
        started = self.now
        self.pause()
        while self.now <= started + 5_000_000_000:
            healthy(self.sm, self.now)
            self.sm.confirm_zero_delivery(self.now)
            self.sm.check_deadlines(self.now)
            self.assertEqual(self.sm.state, OperatorState.INPUT_PAUSED)
            self.now += 50_000_000
        healthy(self.sm, self.now)
        self.sm.confirm_zero_delivery(self.now)
        self.assertEqual(
            self.sm.check_deadlines(self.now)[1].value, "MAX_HOLD_EXCEEDED"
        )
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

    def test_guard_loss_during_pause_cannot_autoarm(self):
        self.pause()
        self.sm.update_guard_telemetry(False, self.now)
        self.assertFalse(self.sm.check_deadlines(self.now)[0])
        self.assertEqual(self.sm.state, OperatorState.FAULT)

    def test_repeated_pauses_do_not_reset_idle_deadline(self):
        idle_start = self.sm.last_directional_input_monotonic_ns
        self.pause()
        generation = self.sm.input_generation
        self.sm.check_deadlines(self.now)
        self.assertEqual(self.sm.input_generation, generation)
        self.sm.confirm_zero_delivery(self.now)
        self.assertTrue(
            self.respond(self.sm.issue_challenge(self.now), MotionDirection.NEUTRAL)[0]
        )
        self.assertEqual(self.sm.last_directional_input_monotonic_ns, idle_start)
        self.now = idle_start + 30_000_000_001
        healthy(self.sm, self.now)
        self.sm.check_deadlines(self.now)
        self.sm.confirm_zero_delivery(self.now)
        self.sm.check_deadlines(self.now)
        self.assertTrue(self.sm.disarm_pending)

    def test_delayed_response_does_not_extend_challenge_deadline(self):
        c = self.sm.issue_challenge(self.now)
        self.assertTrue(
            self.respond(c, MotionDirection.NEUTRAL, now=self.now + 400_000_000)[0]
        )
        self.assertEqual(self.sm.lease_deadline_monotonic_ns, c.deadline_monotonic_ns)


class TestRuntimeConsumers(unittest.TestCase):
    """Verify the agent publishes zero and terminal/bench producers fail closed."""

    def test_agent_pause_publishes_zero_without_disarm_and_observes_write(self):
        from ubuntu_tank_operator.agent_node import OperatorAgentNode

        with tempfile.TemporaryDirectory() as directory:
            node = OperatorAgentNode(
                socket_path=str(Path(directory) / "op.sock"), lease_duration_sec=0.150
            )
            try:
                sm = node.state_machine
                now = time.monotonic_ns()
                healthy(sm, now, False)
                sm.acquire("owner", now)
                sm.arm("owner", sm.epoch, now, "arm")
                sm.confirm_armed(True, True, now, sm.epoch, "arm")
                expiry = now + 150_000_001
                healthy(sm, expiry)
                with (
                    patch(
                        "ubuntu_tank_operator.agent_node.time.monotonic_ns",
                        return_value=expiry,
                    ),
                    patch.object(node, "_publish_twist") as publish,
                    patch.object(node, "_call_set_arm_async") as arm,
                ):
                    node._timer_tick()
                    self.assertEqual(sm.state, OperatorState.INPUT_PAUSED)
                    self.assertTrue(publish.called)
                    self.assertTrue(
                        all(call.args == (0.0, 0.0) for call in publish.call_args_list)
                    )
                    arm.assert_not_called()
                    node._obs_cb(
                        SimpleNamespace(
                            data=json.dumps(
                                {
                                    "stage": "bridge_write",
                                    "success": True,
                                    "bytes_written": 20,
                                    "bytes_expected": 20,
                                    "stamp_mono": (expiry + 1) / 1e9,
                                    "motors": {str(i): 0 for i in range(1, 5)},
                                }
                            )
                        )
                    )
                    self.assertIsNone(
                        sm.zero_confirmed_ns
                    )  # future observation is rejected
                with patch(
                    "ubuntu_tank_operator.agent_node.time.monotonic_ns",
                    return_value=expiry + 10,
                ):
                    node._obs_cb(
                        SimpleNamespace(
                            data=json.dumps(
                                {
                                    "stage": "bridge_write",
                                    "success": True,
                                    "bytes_written": 20,
                                    "bytes_expected": 20,
                                    "stamp_mono": (expiry + 1) / 1e9,
                                    "motors": {str(i): 0 for i in range(1, 5)},
                                }
                            )
                        )
                    )
                self.assertIsNotNone(sm.zero_confirmed_ns)
                for step in range(1, 41):
                    observed = expiry + step * 50_000_000
                    healthy(sm, observed)
                    with (
                        patch(
                            "ubuntu_tank_operator.agent_node.time.monotonic_ns",
                            return_value=observed,
                        ),
                        patch.object(node, "_call_set_arm_async") as arm,
                    ):
                        node._obs_cb(
                            SimpleNamespace(
                                data=json.dumps(
                                    {
                                        "stage": "bridge_write",
                                        "success": True,
                                        "bytes_written": 20,
                                        "bytes_expected": 20,
                                        "stamp_mono": observed / 1e9,
                                        "motors": {str(i): 0 for i in range(1, 5)},
                                    }
                                )
                            )
                        )
                        node._timer_tick()
                        self.assertEqual(sm.state, OperatorState.INPUT_PAUSED)
                        arm.assert_not_called()
            finally:
                node.destroy_node()

    def test_terminal_stall_stops_and_ignores_buffered_direction(self):
        from ubuntu_tank_teleop.teleop_key_node import main

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "op.sock")
            sm = OperatorStateMachine(lease_duration_sec=0.150)
            healthy(sm, time.monotonic_ns(), False)
            server = OperatorIpcServer(sm, path, [os.getuid()])
            server.start()
            observed = []

            class StalledInput(io.StringIO):
                count = 0

                def read(self, size=-1):
                    self.count += 1
                    if self.count == 2:
                        sm._expire_authority(
                            time.monotonic_ns(), "injected terminal stall"
                        )
                        sm.confirm_zero_delivery(time.monotonic_ns())
                    if self.count == 3:
                        observed.append(sm.state)
                    return super().read(size)

            try:
                with (
                    patch.dict(os.environ, {"UBUNTU_TANK_OPERATOR_SOCKET": path}),
                    patch("sys.stdin", StalledInput("wwwq")),
                ):
                    self.assertEqual(main(["--ack-tracks-raised"]), 0)
                self.assertEqual(observed, [OperatorState.OWNED_DISARMED])
                self.assertNotEqual(sm.state, OperatorState.DRIVING)
            finally:
                server.stop()

    def test_burst_cannot_recover_paused_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "op.sock")
            sm = OperatorStateMachine()
            healthy(sm, time.monotonic_ns(), False)
            server = OperatorIpcServer(sm, path, [os.getuid()])
            server.start()
            try:
                with OperatorIpcClient(path) as client:
                    _, epoch, _, _ = client.acquire("bench")
                    self.assertTrue(client.arm(epoch)[0])
                    sm._expire_authority(time.monotonic_ns(), "injected input stall")
                    sm.confirm_zero_delivery(time.monotonic_ns())
                    self.assertFalse(
                        client.run_motion_burst(epoch, "neutral", duration_sec=0.1)[0]
                    )
                    self.assertEqual(sm.state, OperatorState.OWNED_DISARMED)
            finally:
                server.stop()


class RuntimeLifecycle:
    """Controllable runtime lifecycle boundary, without host provisioning."""

    def __init__(self):
        self.state = "inactive"
        self.starts = 0
        self.stops = 0
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()
        self.fail = False

    def get_status(self):
        return True, self.state, None

    def start_controller(self):
        self.starts += 1
        self.entered.set()
        self.proceed.wait(2)
        self.state = "failed" if self.fail else "active"
        return not self.fail, self.state, "failed" if self.fail else None

    def stop_controller(self):
        self.stops += 1
        self.state = "inactive"
        return True, self.state, None


class TestTakeControl(unittest.TestCase):
    """Exercise production HTTP and IPC with controllable lifecycle completion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sm = OperatorStateMachine()
        healthy(self.sm, time.monotonic_ns(), False)
        self.lifecycle = RuntimeLifecycle()
        self.path = str(Path(self.tmp.name) / "operator.sock")
        self.server = OperatorIpcServer(
            self.sm, self.path, [os.getuid()], lifecycle_client=self.lifecycle
        )
        self.server.start()
        self.app = create_app(config=WebControlConfig(operator_socket_path=self.path))
        self.app.state.lifecycle_client = self.lifecycle
        self.client = TestClient(self.app)
        self.headers = {"Origin": "https://localhost:8443"}

    def tearDown(self):
        self.lifecycle.proceed.set()
        self.app.state.operator_relay.close()
        self.server.stop()
        self.client.close()
        self.tmp.cleanup()

    def acquire(self, request="take", operator="browser"):
        return self.client.post(
            "/api/v1/control/acquire",
            headers=self.headers,
            json={
                "request_id": request,
                "operator_id": operator,
                "protocol_version": "2.0.0",
            },
        ).json()

    def result(self, op):
        return self.client.get(
            "/api/v1/operations/" + op["operation_id"],
            params={"operation_token": op["operation_token"]},
        ).json()

    def wait_result(self, op, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = self.result(op)
            if result["status"] != "pending":
                return result
            time.sleep(0.01)
        self.fail("Operation did not finish")

    def test_packaging_operator_probe_uses_installed_protocol(self):
        import runpy
        import subprocess

        packaging = Path(__file__).resolve().parents[2] / "docker" / "ubuntu_tank"
        with patch.object(sys, "path", [str(packaging), *sys.path]):
            smoke = runpy.run_path(str(packaging / "smoke.py"))
        subprocess.run(
            [sys.executable, "-c", smoke["operator_probe"](self.path)],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            check=True,
            capture_output=True,
            text=True,
        )

    def test_start_and_acquire_without_arm_and_private_result(self):
        op = self.acquire()
        result = self.wait_result(op)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["bind_token"])
        self.assertEqual(self.lifecycle.starts, 1)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        self.assertFalse(self.sm.telemetry.guard_armed)
        self.assertEqual(
            self.client.get("/api/v1/operations/" + op["operation_id"]).status_code, 404
        )
        self.assertNotIn("bind_token", self.client.get("/api/v1/status").json())

    def test_running_controller_not_restarted_and_requests_deduplicated(self):
        self.lifecycle.state = "active"
        op = self.acquire()
        same = self.acquire()
        self.assertEqual(op["operation_id"], same["operation_id"])
        changed = self.client.post(
            "/api/v1/control/acquire",
            headers=self.headers,
            json={
                "request_id": "take",
                "operator_id": "browser",
                "protocol_version": "2.0.0",
                "max_linear_speed": 0.05,
            },
        ).json()
        self.assertEqual(changed["error"], "INVALID_PAYLOAD")
        self.assertEqual(self.wait_result(op)["status"], "completed")
        self.assertEqual(self.lifecycle.starts, 0)
        self.assertEqual(self.acquire("other", "other")["error"], "DEPLOYMENT_BUSY")

    def test_stop_preserves_bound_owner_against_other_acquisition(self):
        op = self.acquire()
        result = self.wait_result(op)
        relay = self.app.state.operator_relay
        ok, _, _ = relay.claim_owner_binding(
            "browser", result["epoch"], result["bind_token"], "websocket"
        )
        self.assertTrue(ok)
        stopped = self.client.post(
            "/api/v1/control/stop",
            headers=self.headers,
            json={"request_id": "stop"},
        ).json()
        self.assertTrue(stopped["success"])
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        self.assertEqual(self.acquire("new", "other")["error"], "DEPLOYMENT_BUSY")
        self.assertEqual(self.sm.owner_id, "browser")
        released = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={"request_id": "release", "epoch": self.sm.epoch},
        ).json()
        self.assertTrue(released["success"])
        healthy(self.sm, time.monotonic_ns(), False)
        self.assertEqual(
            self.wait_result(self.acquire("new", "other"))["status"], "completed"
        )
        self.assertEqual(self.sm.owner_id, "other")

    def test_removed_start_endpoint_and_old_protocol_rejected(self):
        self.assertEqual(
            self.client.post(
                "/api/v1/controller/start",
                headers=self.headers,
                json={"request_id": "old"},
            ).status_code,
            405,
        )
        result = self.client.post(
            "/api/v1/control/acquire",
            headers=self.headers,
            json={
                "request_id": "old",
                "operator_id": "old",
                "protocol_version": "1.0.0",
            },
        )
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.lifecycle.starts, 0)

    def test_start_failure_does_not_grant_ownership(self):
        self.lifecycle.fail = True
        result = self.wait_result(self.acquire())
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.sm.owner_id)

    def test_readiness_timeout_stops_started_controller_without_owner(self):
        self.sm.telemetry = TelemetrySnapshot()
        result = self.wait_result(self.acquire(), timeout=6)
        self.assertEqual(result["status"], "failed")
        self.assertIn("readiness timed out", result["message"])
        self.assertIsNone(self.sm.owner_id)
        end = time.monotonic() + 1
        while self.server.acquisition.pending and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.lifecycle.state, "inactive")
        self.assertEqual(self.lifecycle.stops, 1)

    def test_failed_and_expired_setup_allow_new_request(self):
        self.lifecycle.fail = True
        self.assertEqual(self.wait_result(self.acquire())["status"], "failed")
        self.lifecycle.fail = False
        self.server.acquisition.bind_timeout = 0.05
        self.assertEqual(self.wait_result(self.acquire("retry"))["status"], "completed")
        time.sleep(0.15)
        healthy(self.sm, time.monotonic_ns(), False)
        self.assertEqual(
            self.wait_result(self.acquire("after-expiry"))["status"], "completed"
        )

    def test_stop_during_start_prevents_late_acquisition(self):
        self.lifecycle.proceed.clear()
        self.acquire()
        self.assertTrue(self.lifecycle.entered.wait(1))
        stopped = self.client.post(
            "/api/v1/controller/stop", headers=self.headers, json={"request_id": "stop"}
        )
        self.assertEqual(stopped.json()["status"], "completed")
        self.lifecycle.proceed.set()
        end = time.monotonic() + 1
        while self.server.acquisition.pending and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIsNone(self.sm.owner_id)
        self.assertEqual(self.lifecycle.state, "inactive")

    def test_release_cancels_own_pending_operation(self):
        self.lifecycle.proceed.clear()
        op = self.acquire()
        self.assertTrue(self.lifecycle.entered.wait(1))
        wrong = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "request_id": "release",
                "operation_id": op["operation_id"],
                "operation_token": "wrong",
            },
        )
        self.assertFalse(wrong.json()["success"])
        released = self.client.post(
            "/api/v1/control/release",
            headers=self.headers,
            json={
                "request_id": "release",
                "operation_id": op["operation_id"],
                "operation_token": op["operation_token"],
            },
        )
        self.assertTrue(released.json()["success"])
        self.lifecycle.proceed.set()
        end = time.monotonic() + 1
        while self.server.acquisition.pending and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIsNone(self.sm.owner_id)

    def test_controller_stop_upgrades_cancelled_start_worker(self):
        from ubuntu_tank_protocol.lifecycle_client import LifecycleClient
        from ubuntu_tank_supervisor.lifecycle_service import LifecycleHelperService

        for cancel in ("motion", "release", "disconnect"):
            with self.subTest(cancel=cancel):
                state = ["inactive"]
                entered, proceed = threading.Event(), threading.Event()

                def process(args, state=state):
                    if args[0] in ("start", "stop"):
                        state[0] = "active" if args[0] == "start" else "inactive"
                    return 0, state[0], ""

                helper = LifecycleHelperService(
                    str(Path(self.tmp.name) / "lifecycle.sock"), process_runner=process
                )
                helper.start()

                class DelayedSend(LifecycleClient):
                    def __init__(self, socket_path, entered, proceed):
                        super().__init__(socket_path)
                        self.entered, self.proceed = entered, proceed

                    def start_controller(self, *args, **kwargs):
                        self.entered.set()
                        if not self.proceed.wait(5):
                            raise TimeoutError("Test did not release startup")
                        return super().start_controller(*args, **kwargs)

                self.server.acquisition.lifecycle = DelayedSend(
                    helper.socket_path, entered, proceed
                )
                self.app.state.lifecycle_client = LifecycleClient(helper.socket_path)
                try:
                    healthy(self.sm, time.monotonic_ns(), False)
                    op = self.acquire("start-" + cancel)
                    self.assertTrue(entered.wait(1))
                    if cancel == "motion":
                        self.client.post(
                            "/api/v1/control/stop",
                            headers=self.headers,
                            json={"request_id": "motion-stop"},
                        )
                    elif cancel == "release":
                        self.client.post(
                            "/api/v1/control/release",
                            headers=self.headers,
                            json={
                                "request_id": "release",
                                "operation_id": op["operation_id"],
                                "operation_token": op["operation_token"],
                            },
                        )
                    else:
                        self.app.state.operator_relay.close()
                    deadline = time.monotonic() + 1
                    while (
                        not self.server.acquisition.operations[op["operation_id"]][
                            "cancelled"
                        ]
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertTrue(
                        self.server.acquisition.operations[op["operation_id"]][
                            "cancelled"
                        ]
                    )
                    stopped = self.client.post(
                        "/api/v1/controller/stop",
                        headers=self.headers,
                        json={"request_id": "controller-stop"},
                    ).json()
                    self.assertEqual(stopped["status"], "completed")
                    self.assertEqual(state[0], "inactive")
                    proceed.set()
                    deadline = time.monotonic() + 2
                    while (
                        self.server.acquisition.pending and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertIsNone(self.server.acquisition.pending)
                    self.assertEqual(state[0], "inactive")
                    self.assertIsNone(self.sm.owner_id)
                finally:
                    proceed.set()
                    helper.stop()

    def test_relay_recovers_after_operator_socket_loss(self):
        for action in ("take", "poll", "release"):
            with self.subTest(action=action):
                healthy(self.sm, time.monotonic_ns(), False)
                op = self.acquire("before-" + action)
                self.wait_result(op)
                self.server.stop()
                if action == "take":
                    self.assertEqual(
                        self.acquire("broken")["error"], "CONTROLLER_UNAVAILABLE"
                    )
                elif action == "poll":
                    result = self.result(op)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"], "CONTROLLER_UNAVAILABLE")
                else:
                    result = self.client.post(
                        "/api/v1/control/release",
                        headers=self.headers,
                        json={
                            "request_id": "release",
                            "operation_id": op["operation_id"],
                            "operation_token": op["operation_token"],
                        },
                    ).json()
                    self.assertTrue(result["success"])
                self.assertIsNone(self.app.state.operator_relay._owner_client)
                self.server = OperatorIpcServer(
                    self.sm, self.path, [os.getuid()], lifecycle_client=self.lifecycle
                )
                self.server.start()
                healthy(self.sm, time.monotonic_ns(), False)
                self.assertEqual(
                    self.wait_result(self.acquire("after-" + action))["status"],
                    "completed",
                )
                self.app.state.operator_relay.close()
                deadline = time.monotonic() + 1
                while self.sm.owner_id is not None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNone(self.sm.owner_id)

    def test_unbound_grant_expires_in_runtime(self):
        self.server.acquisition.bind_timeout = 0.05
        self.wait_result(self.acquire())
        time.sleep(0.15)
        self.assertIsNone(self.sm.owner_id)
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)

    def test_ipc_old_client_is_rejected_before_ownership(self):
        with OperatorIpcClient(self.path) as client:
            result = client._send_request(
                {"action": "acquire", "operator_id": "old", "protocol_version": "1.0.0"}
            )
            self.assertEqual(result["error"], "INCOMPATIBLE_PROTOCOL")
            self.assertIsNone(self.sm.owner_id)


if __name__ == "__main__":
    unittest.main()
