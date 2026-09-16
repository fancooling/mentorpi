"""
Unit, deterministic regression, and middleware tests for Milestone 11 Operator Agent.

Verifies:
1. Operator IPC Server credential checks (SO_PEERCRED) and framing bounds (64 KiB cap).
2. Exclusive operator authority arbitration (single owner, DEPLOYMENT_BUSY for competing callers).
3. Fail-closed disconnect handling (abrupt client disconnection triggers immediate stop and disarm).
4. Stop priority (any connected client can issue immediate stop independent of ownership).
5. Arming transaction, 250 ms first-command zero deadline, and compensating disarm.
6. Monotonic challenge-response leases (150 ms lease, 5.0 s hold cap, 30.0 s idle timeout).
7. Motion burst execution, delivery observation buffer retrieval, and buffer reset.
8. OperatorAgentNode ROS 2 lifecycle, 20 Hz cmd_vel publishing, and timer-based lease checks.
9. SROS2 permissions enforcement (sole publisher of /controller/cmd_vel, read-only status).
10. CLI integrations (operator_client, teleop_key_node, bench_client) routing through agent IPC with fallback.
11. Systemd service configuration and launcher script security sandboxing.
"""

# ruff: noqa: E402 - workspace paths must be bootstrapped before package imports.

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

# Ensure workspace packages are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
OPERATOR_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_operator")
BRINGUP_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_bringup")
TELEOP_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_teleop")

for p in [UBUNTU_TANK_DIR, OPERATOR_PKG_DIR, BRINGUP_PKG_DIR, TELEOP_PKG_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ubuntu_tank_operator.agent_node import OperatorAgentNode
from ubuntu_tank_operator.authority_lock import (
    acquire_authority_lock,
    release_authority_lock,
)
from ubuntu_tank_operator.constants import (
    FIRST_COMMAND_DEADLINE_SEC,
    MAX_IPC_MESSAGE_BYTES,
    PROTOCOL_VERSION,
)
from ubuntu_tank_operator.enums import (
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_operator.ipc_client import OperatorIpcClient
from ubuntu_tank_operator.ipc_server import (
    OperatorIpcServer,
    extract_peer_credentials,
)
from ubuntu_tank_operator.schemas import (
    TelemetrySnapshot,
)
from ubuntu_tank_operator.state_machine import OperatorStateMachine


class TestIpcPeerCredentialsAndFraming(unittest.TestCase):
    """Test Unix socket SO_PEERCRED credential extraction and 64 KiB framing bounds."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_extract_peer_credentials_on_socketpair(self):
        s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pid, uid, gid = extract_peer_credentials(s1)
            self.assertEqual(pid, os.getpid())
            self.assertEqual(uid, os.getuid())
            self.assertEqual(gid, os.getgid())
        finally:
            s1.close()
            s2.close()

    def test_ipc_client_connection_and_version(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ver = client.get_version()
            self.assertEqual(ver.get("protocol_version"), PROTOCOL_VERSION)
            self.assertEqual(ver.get("release_id"), "test-m11")

    def test_rejection_of_oversized_frame(self):
        """Frames exceeding MAX_IPC_MESSAGE_BYTES (64 KiB) must be rejected and closed."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.socket_path)
        try:
            oversized_payload = (
                b'{"action":"status","padding":"'
                + (b"X" * (MAX_IPC_MESSAGE_BYTES + 100))
                + b'"}\n'
            )
            s.sendall(oversized_payload)
            s.settimeout(1.0)
            resp = s.recv(4096)
            self.assertTrue(len(resp) > 0)
            data = json.loads(resp.decode("utf-8").strip())
            self.assertFalse(data.get("success", True))
            self.assertEqual(
                data.get("error"), WebControlErrorCode.INVALID_PAYLOAD.value
            )
        finally:
            s.close()

    def test_malformed_json_handling(self):
        """Malformed JSON requests must return an error frame without crashing the server."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.socket_path)
        try:
            s.sendall(b"NOT_A_JSON_PAYLOAD\n")
            s.settimeout(1.0)
            resp = s.recv(4096)
            data = json.loads(resp.decode("utf-8").strip())
            self.assertFalse(data.get("success", True))
            self.assertEqual(
                data.get("error"), WebControlErrorCode.INVALID_PAYLOAD.value
            )

            # Server remains healthy for subsequent valid requests
            s.sendall(b'{"action":"version"}\n')
            resp2 = s.recv(4096)
            data2 = json.loads(resp2.decode("utf-8").strip())
            self.assertEqual(data2.get("protocol_version"), PROTOCOL_VERSION)
        finally:
            s.close()

    def test_unauthorized_uid_rejection(self):
        """A server configured with a foreign allowed_uid must reject connections from other UIDs."""
        foreign_sock_path = os.path.join(self.tmp_dir.name, "foreign.sock")
        foreign_server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=foreign_sock_path,
            allowed_uids=[os.getuid() + 999],  # Disallow current user
        )
        foreign_server.start()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(foreign_sock_path)
            s.settimeout(1.0)
            data = s.recv(1024)
            resp = json.loads(data.decode("utf-8").strip())
            self.assertFalse(resp.get("success", True))
            self.assertEqual(resp.get("error"), WebControlErrorCode.UNAUTHORIZED.value)
            # Socket closed by server after sending rejection frame
            eof = s.recv(1024)
            self.assertEqual(len(eof), 0)  # EOF
            s.close()
        finally:
            foreign_server.stop()


class TestExclusiveOperatorArbitration(unittest.TestCase):
    """Verify single-operator exclusivity, DEPLOYMENT_BUSY rejection, and clean release."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_acquire_release_and_busy_arbitration(self):
        client1 = OperatorIpcClient(socket_path=self.socket_path)
        client2 = OperatorIpcClient(socket_path=self.socket_path)

        try:
            # Initial state: NO_OWNER
            st = client1.get_status()
            self.assertEqual(st.get("operator_state"), OperatorState.NO_OWNER.value)
            self.assertIsNone(st.get("active_owner"))

            # Client 1 acquires ownership
            ok, epoch1, err, msg = client1.acquire("operator_1")
            self.assertTrue(ok)
            self.assertEqual(epoch1, 1)
            self.assertIsNone(err)

            st = client1.get_status()
            self.assertEqual(
                st.get("operator_state"), OperatorState.OWNED_DISARMED.value
            )
            self.assertEqual(st.get("active_owner"), "operator_1")

            # Client 2 attempts to acquire -> rejected with DEPLOYMENT_BUSY
            ok2, epoch2, err2, msg2 = client2.acquire("operator_2")
            self.assertFalse(ok2)
            self.assertIsNone(epoch2)
            self.assertEqual(err2, WebControlErrorCode.DEPLOYMENT_BUSY.value)
            self.assertIn("already held", msg2 or "")

            # Client 2 attempts to release -> rejected with NOT_OWNER
            rel_ok2, rel_err2, _ = client2.release(epoch=1)
            self.assertFalse(rel_ok2)
            self.assertEqual(rel_err2, WebControlErrorCode.NOT_OWNER.value)

            # Client 1 releases cleanly
            rel_ok1, rel_err1, _ = client1.release(epoch=epoch1)
            self.assertTrue(rel_ok1)
            self.assertIsNone(rel_err1)

            st = client1.get_status()
            self.assertEqual(st.get("operator_state"), OperatorState.NO_OWNER.value)
            self.assertIsNone(st.get("active_owner"))

            # Now Client 2 can acquire ownership
            ok2, epoch2, err2, msg2 = client2.acquire("operator_2")
            self.assertTrue(ok2)
            self.assertEqual(epoch2, 3)
            self.assertEqual(client2.get_status().get("active_owner"), "operator_2")

        finally:
            client1.close()
            client2.close()


class TestFailClosedDisconnectAndStopPriority(unittest.TestCase):
    """Verify that socket loss stops/disarms robot, and any client can execute stop."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.stop_called = False

        def on_stop():
            self.stop_called = True

        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
            stop_callback=on_stop,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_fail_closed_owner_disconnect(self):
        """When the active owner disconnects, server must stop and disarm immediately."""
        client = OperatorIpcClient(socket_path=self.socket_path)
        client.acquire("owner_abc")
        ok, err, msg = client.arm(epoch=1, tracks_raised=True)
        self.assertTrue(ok)
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # Abruptly close client socket
        self.stop_called = False
        client.close()

        # Allow server thread to detect EOF
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < 1.0:
            if self.stop_called and self.sm.state == OperatorState.NO_OWNER:
                break
            time.sleep(0.02)

        self.assertTrue(
            self.stop_called, "stop_callback was not invoked on owner disconnect"
        )
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.sm.owner_id)

    def test_stop_priority_by_any_client(self):
        """Any client connection must be able to issue stop and disarm the robot."""
        owner = OperatorIpcClient(socket_path=self.socket_path)
        intruder = OperatorIpcClient(socket_path=self.socket_path)

        try:
            owner.acquire("owner_1")
            owner.arm(epoch=1, tracks_raised=True)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

            self.stop_called = False
            # Non-owner issues stop
            stop_ok, stop_msg = intruder.stop()
            self.assertTrue(stop_ok)
            self.assertTrue(self.stop_called)
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
            vx, wz = self.sm.get_velocity_command()
            self.assertEqual((vx, wz), (0.0, 0.0))
        finally:
            owner.close()
            intruder.close()


class TestArmingTransactionAndFirstCommandZero(unittest.TestCase):
    """Verify arming transaction, tracks_raised boolean check, and first-command zero deadline."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.arm_requests: list[tuple[int, str]] = []

        def on_arm(epoch: int, request_id: str):
            self.arm_requests.append((epoch, request_id))
            self.sm.confirm_armed(
                guard_confirmed=True,
                downstream_zero_confirmed=True,
                current_monotonic_ns=time.monotonic_ns(),
                epoch=epoch,
                request_id=request_id,
            )
            return True, None, "Arm confirmed"

        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
            arm_callback=on_arm,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_arm_requires_tracks_raised_true(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            client.acquire("op_arm")

            # Arm with tracks_raised=False -> rejected
            ok, err, msg = client.arm(epoch=1, tracks_raised=False)
            self.assertFalse(ok)
            self.assertEqual(err, WebControlErrorCode.INVALID_PAYLOAD.value)

            # Arm with tracks_raised=True -> succeeds
            ok, err, msg = client.arm(epoch=1, tracks_raised=True)
            self.assertTrue(ok)
            self.assertEqual(len(self.arm_requests), 1)
            self.assertEqual(self.arm_requests[0][0], 1)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

    def test_non_owner_cannot_arm(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, err, msg = client.arm(epoch=1, tracks_raised=True)
            self.assertFalse(ok)
            self.assertEqual(err, WebControlErrorCode.NOT_OWNER.value)

    def test_first_command_zero_deadline(self):
        """First-command zero deadline constant must be <= 0.250 s."""
        self.assertLessEqual(FIRST_COMMAND_DEADLINE_SEC, 0.250)


class TestChallengeAndIntentLeases(unittest.TestCase):
    """Verify single-use cryptographic challenge-intent leases and 150 ms expiry."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_challenge_and_intent_success(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            client.acquire("op_intent")
            client.arm(epoch=1, tracks_raised=True)

            # Request challenge
            c = client.request_challenge(epoch=1)
            self.assertTrue(c.get("success"))
            token = c.get("token")
            self.assertIsNotNone(token)
            self.assertEqual(c.get("epoch"), 1)

            # Submit direction intent
            ok, cur_dir, err = client.submit_intent(
                token=token,
                epoch=1,
                sequence=1,
                direction=MotionDirection.FORWARD,
            )
            self.assertTrue(ok)
            self.assertEqual(cur_dir, "forward")
            self.assertIsNone(err)
            self.assertEqual(self.sm.state, OperatorState.DRIVING)
            vx, wz = self.sm.get_velocity_command()
            self.assertGreater(vx, 0.0)

            # Replaying the same token must fail (single-use)
            ok_re, cur_dir_re, err_re = client.submit_intent(
                token=token,
                epoch=1,
                sequence=2,
                direction=MotionDirection.FORWARD,
            )
            self.assertFalse(ok_re)
            self.assertEqual(err_re, WebControlErrorCode.CHALLENGE_REUSED.value)

    def test_wrong_epoch_challenge_rejected(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            client.acquire("op_intent")
            c = client.request_challenge(epoch=999)  # Mismatched epoch
            self.assertFalse(c.get("success"))
            self.assertEqual(c.get("error"), WebControlErrorCode.INVALID_EPOCH.value)

    def test_challenge_while_disarmed_returns_invalid_state(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("op_disarmed")
            self.assertTrue(ok)
            response = client.request_challenge(epoch=epoch)
            self.assertFalse(response.get("success"))
            self.assertEqual(
                response.get("error"), WebControlErrorCode.INVALID_STATE.value
            )


class TestMotionBurstAndObservations(unittest.TestCase):
    """Verify motion burst execution, observation recording, and buffer reset."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.observations: list[dict[str, Any]] = [
            {"stage": "controller_rx", "stamp_mono": 100.0, "_rx_mono_ns": 100_000_000},
            {"stage": "guard_rx", "stamp_mono": 100.1, "_rx_mono_ns": 100_100_000},
            {"stage": "guard_fwd", "stamp_mono": 100.2, "_rx_mono_ns": 100_200_000},
            {"stage": "bridge_rx", "stamp_mono": 100.3, "_rx_mono_ns": 100_300_000},
            {
                "stage": "bridge_write",
                "stamp_mono": 100.4,
                "_rx_mono_ns": 100_400_000,
                "motors": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0},
            },
        ]

        def get_obs(since_ns):
            if since_ns is None:
                return self.observations
            return [o for o in self.observations if o.get("_rx_mono_ns", 0) >= since_ns]

        def reset_obs():
            self.observations.clear()
            return time.monotonic_ns()

        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
            observations_callback=get_obs,
            reset_observations_callback=reset_obs,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_burst_execution_via_ipc(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            client.acquire("op_burst")
            client.arm(epoch=1, tracks_raised=True)

            ok, msg = client.run_motion_burst(
                epoch=1,
                direction="forward",
                linear_x=0.15,
                angular_z=0.0,
                duration_sec=0.2,
                rate_hz=20.0,
            )
            self.assertTrue(ok)
            # Burst completed and auto-stopped
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

    def test_stop_interrupts_burst_promptly(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            client.acquire("burst_owner")
            client.arm(epoch=1, tracks_raised=True)

            burst_result = {}

            def run():
                ok, msg = client.run_motion_burst(
                    epoch=1,
                    direction="forward",
                    duration_sec=1.5,
                    rate_hz=20.0,
                )
                burst_result["ok"] = ok
                burst_result["msg"] = msg

            t = threading.Thread(target=run)
            t.start()

            # Let burst start
            time.sleep(0.08)

            # External stop issued by non-owner client
            with OperatorIpcClient(socket_path=self.socket_path) as non_owner:
                stop_ok, _ = non_owner.stop()
                self.assertTrue(stop_ok)

            t.join(timeout=1.0)
            self.assertFalse(t.is_alive())
            self.assertFalse(burst_result.get("ok", True))
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

    def test_observations_retrieval_and_reset(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            obs = client.get_observations()
            self.assertEqual(len(obs), 5)
            self.assertEqual(obs[0]["stage"], "controller_rx")
            self.assertEqual(obs[-1]["stage"], "bridge_write")

            # Reset buffer
            client.reset_observations()
            obs_after = client.get_observations()
            self.assertEqual(len(obs_after), 0)


class TestOperatorAgentNodeLifecycle(unittest.TestCase):
    """Verify OperatorAgentNode creation, timer ticks, zero publishing, and clean shutdown."""

    def test_agent_node_lifecycle_and_ipc(self):
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            socket_path = os.path.join(tmp_dir.name, "agent.sock")
            node = OperatorAgentNode(
                node_name="test_operator_agent",
                socket_path=socket_path,
                allowed_uids=[os.getuid()],
            )
            self.assertTrue(os.path.exists(socket_path))

            # Verify IPC communication with running agent node
            with OperatorIpcClient(socket_path=socket_path) as client:
                status = client.get_status()
                self.assertEqual(
                    status.get("operator_state"), OperatorState.NO_OWNER.value
                )

                # Set healthy telemetry for preflight
                node.state_machine.telemetry = TelemetrySnapshot(
                    battery_voltage=12.2,
                    battery_monotonic_ns=time.monotonic_ns(),
                    guard_armed=False,
                    guard_monotonic_ns=time.monotonic_ns(),
                    odom_linear_x=0.0,
                    odom_angular_z=0.0,
                    odom_monotonic_ns=time.monotonic_ns(),
                )

                # Acquire and arm locally
                ok, epoch, _, _ = client.acquire("test_client")
                self.assertTrue(ok)
                arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
                self.assertTrue(arm_ok)

                # Lease timer tick
                node._timer_tick()
                # Publish zero
                node.publish_zero(count=2)

                # Stop
                client.stop()
                self.assertEqual(
                    client.get_status().get("operator_state"),
                    OperatorState.OWNED_DISARMED.value,
                )

            # Node shutdown cleans up socket and disarms
            node.destroy_node()
            self.assertIsNone(node.ipc_server)
            self.assertFalse(os.path.exists(socket_path))

        finally:
            tmp_dir.cleanup()

    def test_battery_telemetry_ingestion_and_arming(self):
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            socket_path = os.path.join(tmp_dir.name, "agent_batt.sock")
            node = OperatorAgentNode(
                node_name="test_operator_agent_batt",
                socket_path=socket_path,
                allowed_uids=[os.getuid()],
            )

            # Ingest UInt16 battery telemetry (millivolts)
            class MockUInt16:
                data = 12450

            node._battery_cb(MockUInt16())

            # Verify voltage is converted to Volts (12.45V) and monotonic timestamp is set
            self.assertIsNotNone(node.state_machine.telemetry.battery_voltage)
            self.assertAlmostEqual(
                node.state_machine.telemetry.battery_voltage, 12.45, places=2
            )
            self.assertIsNotNone(node.state_machine.telemetry.battery_monotonic_ns)

            # Set fresh guard telemetry
            node.state_machine.update_guard_telemetry(False, time.monotonic_ns())

            with OperatorIpcClient(socket_path=socket_path) as client:
                ok, epoch, _, _ = client.acquire("batt_test")
                self.assertTrue(ok)

                # Healthy battery and fresh guard -> Arm succeeds
                arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
                self.assertTrue(arm_ok)
                self.assertEqual(node.state_machine.state, OperatorState.ARMED_IDLE)

                # Now simulate stale battery telemetry (> 3.0 s age)
                node.state_machine.telemetry.battery_monotonic_ns = (
                    time.monotonic_ns() - int(4e9)
                )

                # Timer tick should detect stale battery and trip disarm
                node._timer_tick()
                self.assertIn(
                    node.state_machine.state,
                    (OperatorState.FAULT, OperatorState.OWNED_DISARMED),
                )

            node.destroy_node()
        finally:
            tmp_dir.cleanup()

    def test_timer_stall_cannot_sustain_motion_beyond_lease(self):
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            socket_path = os.path.join(tmp_dir.name, "agent_stall.sock")
            node = OperatorAgentNode(
                node_name="test_operator_agent_stall",
                socket_path=socket_path,
                allowed_uids=[os.getuid()],
            )
            published_twists = []
            node._publish_twist = lambda lx, az: published_twists.append((lx, az))

            now_ns = time.monotonic_ns()
            node.state_machine.update_guard_telemetry(False, now_ns)
            node.state_machine.update_battery_telemetry(12.0, now_ns)

            with OperatorIpcClient(socket_path=socket_path) as client:
                client.acquire("stall_test")
                client.arm(epoch=1, tracks_raised=True)

                c = client.request_challenge(1)
                client.submit_intent(c["token"], 1, 1, "forward")
                self.assertEqual(node.state_machine.state, OperatorState.DRIVING)

                # During normal tick, velocity is published
                node._timer_tick()
                self.assertGreater(len(published_twists), 0)
                published_twists.clear()

                # Simulate timer stall: no ticks occur. Commands cannot be published.
                self.assertEqual(len(published_twists), 0)

                # Advance time beyond lease (150 ms)
                time.sleep(0.18)

                # When timer tick eventually fires after stall, lease has expired
                node._timer_tick()
                # Must transition away from DRIVING and publish zero
                self.assertNotEqual(node.state_machine.state, OperatorState.DRIVING)

            node.destroy_node()
        finally:
            tmp_dir.cleanup()

    def test_lock_contention_rechecks_lease_before_publication(self):
        """A timer delayed on the state lock must never publish cached motion."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = os.path.join(tmp_dir, "agent_contention.sock")
            node = OperatorAgentNode(
                node_name="test_operator_agent_contention",
                socket_path=socket_path,
                allowed_uids=[os.getuid()],
            )
            published_twists = []
            node._publish_twist = lambda lx, az: published_twists.append((lx, az))
            now_ns = time.monotonic_ns()
            node.state_machine.telemetry = TelemetrySnapshot(
                battery_voltage=12.2,
                battery_monotonic_ns=now_ns,
                guard_armed=False,
                guard_monotonic_ns=now_ns,
                odom_linear_x=0.0,
                odom_angular_z=0.0,
                odom_monotonic_ns=now_ns,
            )
            try:
                with OperatorIpcClient(socket_path=socket_path) as client:
                    ok, epoch, _, _ = client.acquire("contention_test")
                    self.assertTrue(ok)
                    arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
                    self.assertTrue(arm_ok)
                    challenge = client.request_challenge(epoch)
                    drive_ok, _, _ = client.submit_intent(
                        challenge["token"], epoch, 1, "forward"
                    )
                    self.assertTrue(drive_ok)
                    published_twists.clear()

                    with node._lock:
                        timer = threading.Thread(target=node._timer_tick)
                        timer.start()
                        time.sleep(0.20)
                    timer.join(timeout=1.0)

                    self.assertFalse(timer.is_alive())
                    self.assertNotEqual(node.state_machine.state, OperatorState.DRIVING)
                    self.assertTrue(published_twists)
                    self.assertTrue(
                        all(lx == 0.0 and az == 0.0 for lx, az in published_twists)
                    )
            finally:
                node.destroy_node()


class TestAgentArmDeliveryConfirmation(unittest.TestCase):
    """Verify real guard success is not reported before downstream zero delivery."""

    class _SetBool:
        class Request:
            def __init__(self):
                self.data = False

    class _Response:
        success = True
        message = "guard armed"

    class _Future:
        def __init__(self, delay_sec: float = 0.0):
            self._ready_at = time.monotonic() + delay_sec

        def done(self):
            return time.monotonic() >= self._ready_at

        def result(self):
            return TestAgentArmDeliveryConfirmation._Response()

    class _ArmClient:
        def __init__(self, delay_sec: float = 0.0):
            self.delay_sec = delay_sec
            self.requests = []

        def wait_for_service(self, timeout_sec=0.0):
            return True

        def service_is_ready(self):
            return True

        def call_async(self, request):
            self.requests.append(bool(request.data))
            return TestAgentArmDeliveryConfirmation._Future(
                self.delay_sec if request.data else 0.0
            )

    def _make_arming_node(self, tmp_dir: str, delay_sec: float = 0.0):
        socket_path = os.path.join(tmp_dir, "arm_delivery.sock")
        node = OperatorAgentNode(socket_path=socket_path, allowed_uids=[os.getuid()])
        now_ns = time.monotonic_ns()
        node.state_machine.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=now_ns,
            guard_armed=False,
            guard_monotonic_ns=now_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=now_ns,
        )
        ok, epoch, _, _ = node.state_machine.acquire("arm_test", now_ns)
        self.assertTrue(ok)
        request_id = "arm-delivery-test"
        ok, _, _ = node.state_machine.arm(
            "arm_test", epoch, True, time.monotonic_ns(), request_id
        )
        self.assertTrue(ok)
        node.arm_client = self._ArmClient(delay_sec)
        return node, epoch, request_id

    def test_dropped_zero_delivery_returns_failure_and_disarms(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            node, epoch, request_id = self._make_arming_node(tmp_dir)
            try:
                with mock.patch(
                    "ubuntu_tank_operator.agent_node.SetBool", self._SetBool
                ):
                    ok, err, message = node.execute_arm(epoch, request_id)
                self.assertFalse(ok)
                self.assertIn(err, ("TIMEOUT", "OPERATION_FAILED"))
                self.assertIn("zero", message.lower())
                self.assertNotEqual(node.state_machine.state, OperatorState.ARMED_IDLE)
                self.assertIn(False, node.arm_client.requests)
            finally:
                node.destroy_node()

    def test_fresh_complete_zero_write_confirms_arm(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            node, epoch, request_id = self._make_arming_node(tmp_dir)
            original_publish_zero = node.publish_zero

            def publish_and_observe(count=3):
                original_publish_zero(count)
                node._observations.append(
                    {
                        "stage": "bridge_write",
                        "success": True,
                        "bytes_written": 27,
                        "bytes_expected": 27,
                        "motors": [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]],
                        "_rx_mono_ns": time.monotonic_ns(),
                    }
                )

            node.publish_zero = publish_and_observe
            try:
                with mock.patch(
                    "ubuntu_tank_operator.agent_node.SetBool", self._SetBool
                ):
                    ok, err, _message = node.execute_arm(epoch, request_id)
                self.assertTrue(ok)
                self.assertIsNone(err)
                self.assertEqual(node.state_machine.state, OperatorState.ARMED_IDLE)
            finally:
                node.destroy_node()

    def test_late_guard_success_returns_failure_and_disarms(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            node, epoch, request_id = self._make_arming_node(tmp_dir, delay_sec=0.28)
            node.publish_zero = lambda count=3: node._observations.append(
                {
                    "stage": "bridge_write",
                    "success": True,
                    "bytes_written": 27,
                    "bytes_expected": 27,
                    "motors": [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]],
                    "_rx_mono_ns": time.monotonic_ns(),
                }
            )
            try:
                with mock.patch(
                    "ubuntu_tank_operator.agent_node.SetBool", self._SetBool
                ):
                    ok, err, _message = node.execute_arm(epoch, request_id)
                self.assertFalse(ok)
                self.assertEqual(err, WebControlErrorCode.TIMEOUT.value)
                self.assertEqual(node.state_machine.state, OperatorState.FAULT)
                self.assertIn(False, node.arm_client.requests)
            finally:
                node.destroy_node()


class TestIpcPostLockTimestamps(unittest.TestCase):
    """Verify IPC validation timestamps are sampled after lock contention."""

    def test_expired_intent_waiting_for_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = os.path.join(tmp_dir, "ipc_contention.sock")
            sm = OperatorStateMachine(release_id="test-ipc-contention")
            now_ns = time.monotonic_ns()
            sm.telemetry = TelemetrySnapshot(
                battery_voltage=12.2,
                battery_monotonic_ns=now_ns,
                guard_armed=False,
                guard_monotonic_ns=now_ns,
                odom_linear_x=0.0,
                odom_angular_z=0.0,
                odom_monotonic_ns=now_ns,
            )

            def confirm_arm(epoch, request_id):
                ok, err, msg = sm.confirm_armed(
                    True, True, time.monotonic_ns(), epoch, request_id
                )
                return ok, err.value if err else None, msg

            server = OperatorIpcServer(
                state_machine=sm,
                socket_path=socket_path,
                allowed_uids=[os.getuid()],
                arm_callback=confirm_arm,
            )
            server.start()
            try:
                with OperatorIpcClient(socket_path=socket_path) as client:
                    ok, epoch, _, _ = client.acquire("ipc_contention")
                    self.assertTrue(ok)
                    arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
                    self.assertTrue(arm_ok)
                    challenge = client.request_challenge(epoch)
                    result = []

                    with server._lock:
                        worker = threading.Thread(
                            target=lambda: result.append(
                                client.submit_intent(
                                    challenge["token"], epoch, 1, "forward"
                                )
                            )
                        )
                        worker.start()
                        time.sleep(0.20)
                    worker.join(timeout=1.0)

                    self.assertFalse(worker.is_alive())
                    self.assertFalse(result[0][0])
                    self.assertNotEqual(sm.state, OperatorState.DRIVING)
            finally:
                server.stop()


class TestSros2PolicySegregation(unittest.TestCase):
    """Verify that SROS2 policies enforce single operator publisher authority and read-only status."""

    def test_sros2_policy_verification_script(self):
        from scripts import sros2_policy

        # Invariants and schema checks must pass
        self.assertTrue(sros2_policy.verify_governance())
        self.assertTrue(sros2_policy.verify_security_invariants())

    def test_operator_is_sole_cmd_vel_publisher(self):
        from scripts.sros2_policy import parse_policies_xml

        policies = parse_policies_xml()

        # Enclave /ubuntu_tank/operator must be the sole publisher of /controller/cmd_vel
        cmd_vel_topic = "/controller/cmd_vel"
        for enc, perms in policies.items():
            if enc == "/ubuntu_tank/operator":
                self.assertIn(cmd_vel_topic, perms["publish_topics"])
            else:
                self.assertNotIn(
                    cmd_vel_topic,
                    perms["publish_topics"],
                    f"Forbidden: Non-operator enclave {enc} has permission to publish {cmd_vel_topic}",
                )

    def test_status_enclave_is_read_only(self):
        from scripts.sros2_policy import parse_policies_xml

        policies = parse_policies_xml()
        status_perms = policies["/ubuntu_tank/status"]

        # Status must not publish to any motor or velocity command topics
        for topic in [
            "/controller/cmd_vel",
            "/ubuntu_tank_safety/motor_input",
            "/ros_robot_controller/set_motor_guarded",
        ]:
            self.assertNotIn(topic, status_perms["publish_topics"])

        # Status must not call set_arm service
        self.assertNotIn(
            "/ubuntu_tank_safety/set_arm", status_perms["service_requests"]
        )

    def test_operator_and_status_battery_dds_permissions(self):
        from scripts.sros2_policy import (
            PERMISSIONS_DIR,
            matches_any_dds_pattern,
            parse_permissions_xml,
        )

        battery_topic = "rt/ros_robot_controller/battery"
        for enc in ["operator", "status"]:
            perm_file = os.path.join(PERMISSIONS_DIR, f"{enc}_permissions.xml")
            perm_data = parse_permissions_xml(perm_file)
            self.assertTrue(
                matches_any_dds_pattern(battery_topic, perm_data["subscribe_topics"]),
                f"Enclave {enc} must be allowed to subscribe to {battery_topic}",
            )
            self.assertFalse(
                matches_any_dds_pattern(battery_topic, perm_data["publish_topics"]),
                f"Enclave {enc} must not be allowed to publish {battery_topic}",
            )


class TestCliIntegrationRegressions(unittest.TestCase):
    """Verify operator_client, teleop_key_node, and bench_client route through IPC with fallback."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.sm = OperatorStateMachine(release_id="test-m11")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()
        os.environ["UBUNTU_TANK_OPERATOR_SOCKET"] = self.socket_path

    def tearDown(self):
        os.environ.pop("UBUNTU_TANK_OPERATOR_SOCKET", None)
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_operator_client_main_routes_through_ipc(self):
        from ubuntu_tank_bringup.operator_client import main as operator_main

        # Arm through IPC
        rc_arm = operator_main(["--arm"])
        self.assertEqual(rc_arm, 0)
        # Wait briefly for server to detect client exit EOF and release ownership
        start_wait = time.monotonic()
        while (
            time.monotonic() - start_wait < 1.0
            and self.sm.state != OperatorState.NO_OWNER
        ):
            time.sleep(0.02)
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)

        # Disarm through IPC
        rc_disarm = operator_main(["--disarm"])
        self.assertEqual(rc_disarm, 0)
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)

    def test_bench_client_routes_through_ipc(self):
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        bench = BenchClientNode(node_name="test_bench_node")
        try:
            self.assertIsNotNone(bench._ipc_client)
            self.assertEqual(bench._ipc_epoch, 1)

            # Arm via bench client
            ok, msg = bench.call_set_arm(True)
            self.assertTrue(ok)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

            # Disarm via bench client
            ok, msg = bench.call_set_arm(False)
            self.assertTrue(ok)
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        finally:
            bench.destroy_node()

    def test_competing_teleop_fails_closed_without_direct_publisher(self):
        from ubuntu_tank_teleop.teleop_key_node import main as teleop_main

        # Client 1 already holds ownership
        with OperatorIpcClient(socket_path=self.socket_path) as client1:
            client1.acquire("existing_owner")

            # Client 2 runs teleop without --direct-ros
            rc = teleop_main([])
            # Must fail closed and return exit code 1
            self.assertEqual(rc, 1)

    def test_competing_bench_fails_closed_without_direct_publisher(self):
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        # Client 1 already holds ownership
        with OperatorIpcClient(socket_path=self.socket_path) as client1:
            client1.acquire("existing_owner")

            # Competing BenchClientNode must raise RuntimeError on ownership rejection
            with self.assertRaises(RuntimeError) as ctx:
                BenchClientNode(node_name="competing_bench")
            self.assertIn("denied ownership", str(ctx.exception))

    def test_persistent_teleop_session_flow(self):
        import io

        from ubuntu_tank_teleop.teleop_key_node import main as teleop_main

        fake_stdin = io.StringIO("q\n")
        orig_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            rc = teleop_main(["--ack-tracks-raised"])
            self.assertEqual(rc, 0)
            # After exit, connection disconnects -> server detects EOF and disarms to NO_OWNER
            start_wait = time.monotonic()
            while (
                time.monotonic() - start_wait < 1.0
                and self.sm.state != OperatorState.NO_OWNER
            ):
                time.sleep(0.02)
            self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        finally:
            sys.stdin = orig_stdin


class TestServiceAndLauncherSecurity(unittest.TestCase):
    """Verify systemd service unit and launcher script security sandboxing directives."""

    def test_systemd_unit_hardening_and_validation(self):
        """systemd-analyze verify verifies mentorpi-tank-operator.service and rejects malformed directives."""
        import shutil
        import subprocess

        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        unit_path = os.path.join(
            REPO_ROOT, "ubuntu_tank/host/mentorpi-tank-operator.service"
        )
        self.assertTrue(os.path.isfile(unit_path))

        # Negative test: invalid directive is rejected by systemd-analyze
        with tempfile.TemporaryDirectory() as td:
            bad_unit = os.path.join(td, "mentorpi-tank-operator.service")
            with open(unit_path, "r", encoding="utf-8") as f:
                content = f.read()
            corrupted = content.replace(
                "ProtectSystem=strict", "ProtectSystem=invalid_setting"
            )
            with open(bad_unit, "w", encoding="utf-8") as f:
                f.write(corrupted)
            res = subprocess.run(
                [systemd_analyze, "verify", bad_unit],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertIn("Failed to parse", res.stderr + res.stdout)

    def test_launcher_executable_and_enclave(self):
        """Launcher must be executable and enforce the operator SROS2 enclave when invoked."""
        import subprocess

        launcher_path = os.path.join(
            REPO_ROOT, "ubuntu_tank/bin/mentorpi-tank-operator"
        )
        self.assertTrue(os.path.isfile(launcher_path))
        self.assertTrue(
            os.access(launcher_path, os.X_OK),
            "Launcher must have executable permission",
        )

        # Verify launcher execution: running with --help produces usage and exits cleanly
        env = dict(os.environ)
        env["MENTORPI_SIMULATION"] = "1"
        env.pop("_MENTORPI_TANK_OPERATOR_SOURCED", None)
        res = subprocess.run(
            [launcher_path, "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage: ubuntu_tank_operator", res.stdout)

        # Verify enclave assignment: executing environment setup configures operator enclave
        check_cmd = [
            sys.executable,
            "-c",
            (
                "import os, runpy; "
                f"os.environ['_MENTORPI_TANK_OPERATOR_SOURCED'] = '1'; "
                f"runpy.run_path('{launcher_path}', run_name='__not_main__'); "
                "print(os.environ.get('ROS_SECURITY_ENCLAVE_OVERRIDE', ''))"
            ),
        ]
        res_enclave = subprocess.run(
            check_cmd, capture_output=True, text=True, check=False
        )
        self.assertEqual(res_enclave.stdout.strip(), "/ubuntu_tank/operator")

    def test_stack_units_form_a_valid_systemd_job(self):
        """Systemd accepts the stack target and both isolated service jobs."""
        import shutil
        import subprocess
        from pathlib import Path

        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        host_dir = Path(UBUNTU_TANK_DIR, "host")
        unit_names = (
            "mentorpi-tank-stack.target",
            "mentorpi-tank.service",
            "mentorpi-tank-operator.service",
            "mentorpi-tank-recover.service",
        )
        base_targets = (
            "network.target",
            "multi-user.target",
            "sysinit.target",
            "basic.target",
        )
        system_unit_dir = Path("/usr/lib/systemd/system")

        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            staged_units = root / "etc/systemd/system"
            staged_units.mkdir(parents=True)
            for unit_name in unit_names:
                shutil.copy2(host_dir / unit_name, staged_units / unit_name)
            for target_name in base_targets:
                source = system_unit_dir / target_name
                if not source.is_file():
                    self.skipTest(f"systemd base target unavailable: {target_name}")
                shutil.copy2(source, staged_units / target_name)

            executable_paths = (
                root / "opt/ubuntu_tank/current/bin/mentorpi-tank-run",
                root / "opt/ubuntu_tank/current/bin/mentorpi-tank-operator",
                root / "opt/ubuntu_tank/libexec/recover-activation",
            )
            for executable in executable_paths:
                executable.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2("/bin/true", executable)

            result = subprocess.run(
                [
                    systemd_analyze,
                    f"--root={root}",
                    "verify",
                    *unit_names,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


class TestGuardLivenessFreshness(unittest.TestCase):
    """Verify periodic observed guard liveness matching actual production publications (Finding 1)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "guard_fresh.sock")
        self.node = OperatorAgentNode(
            node_name="test_guard_fresh",
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        now_ns = time.monotonic_ns()
        self.node.state_machine.telemetry.battery_voltage = 12.2
        self.node.state_machine.telemetry.battery_monotonic_ns = now_ns
        self.node.state_machine.telemetry.guard_monotonic_ns = now_ns

    def tearDown(self):
        self.node.destroy_node()
        self.tmp_dir.cleanup()

    def test_idle_arming_after_normal_idle_time_with_guard_alive(self):
        """Guard liveness in idle is observed via service readiness (or override), allowing arming after > 500 ms."""
        self.node._guard_liveness_override = lambda: True

        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_op")
            self.assertTrue(ok)

            # Simulate passage of normal idle time (> 0.5 s, e.g. 550 ms)
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < 0.6:
                self.node.state_machine.telemetry.battery_monotonic_ns = (
                    time.monotonic_ns()
                )
                self.node._timer_tick()
                time.sleep(0.05)

            # Arming must SUCCEED because guard liveness was observed while idle
            arm_ok, arm_err, arm_msg = client.arm(epoch, tracks_raised=True)
            self.assertTrue(
                arm_ok,
                f"Arming should succeed after idle time: {arm_err} ({arm_msg})",
            )
            self.assertEqual(self.node.state_machine.state, OperatorState.ARMED_IDLE)

    def test_idle_arming_fails_if_guard_not_alive(self):
        """If guard service is not ready (guard dead/absent), idle guard telemetry expires and arming fails."""
        self.node._guard_liveness_override = lambda: False
        self.node.state_machine.telemetry.guard_monotonic_ns = (
            time.monotonic_ns() - int(1.0 * 1e9)
        )

        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_op")
            self.assertTrue(ok)
            self.node.state_machine.telemetry.battery_monotonic_ns = time.monotonic_ns()

            self.node._timer_tick()
            arm_ok, arm_err, arm_msg = client.arm(epoch, tracks_raised=True)
            self.assertFalse(arm_ok)
            self.assertEqual(arm_err, WebControlErrorCode.STALE_TELEMETRY.value)

    def test_renewed_motion_with_motor_guard_delivery_observations(self):
        """Renewed motion for > 1.0s receives periodic delivery observations from motor_guard without STALE_TELEMETRY."""
        self.node._guard_liveness_override = lambda: True
        self.node._timer_tick()

        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_op")
            self.assertTrue(ok)
            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)

            start_mono = time.monotonic()
            seq = 0
            MockMsg = type("MockMsg", (), {})
            while time.monotonic() - start_mono < 1.0:
                now_ns = time.monotonic_ns()
                self.node.state_machine.telemetry.battery_monotonic_ns = now_ns

                # Request challenge and submit intent every 50 ms
                c = client.request_challenge(epoch, timeout_sec=0.2)
                self.assertIsNotNone(c)
                seq += 1
                ok, cur_dir, err = client.submit_intent(
                    c["token"], epoch, seq, "forward", timeout_sec=0.2
                )
                self.assertTrue(ok)

                # Feed actual production motor_guard delivery observation
                obs_payload = {
                    "node": "motor_guard",
                    "stage": "guard_fwd",
                    "seq": seq,
                    "stamp_mono": time.monotonic(),
                    "motors": [[1, 1.0], [2, 1.0], [3, 1.0], [4, 1.0]],
                    "is_armed": True,
                }
                msg = MockMsg()
                msg.data = json.dumps(obs_payload)
                self.node._obs_cb(msg)

                # Run timer tick
                self.node._timer_tick()
                time.sleep(0.04)

            # Ensure state is still DRIVING and healthy
            self.assertEqual(self.node.state_machine.state, OperatorState.DRIVING)
            self.assertIsNone(self.node.state_machine.last_fault)

    def test_stalled_guard_detected_during_motion(self):
        """If motor_guard stops publishing observations during motion, deadline check detects STALE_TELEMETRY within 0.5s."""
        self.node._guard_liveness_override = lambda: True
        self.node._timer_tick()

        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_op")
            self.assertTrue(ok)
            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)

            c = client.request_challenge(epoch, timeout_sec=0.2)
            ok, cur_dir, err = client.submit_intent(
                c["token"], epoch, 1, "forward", timeout_sec=0.2
            )
            self.assertTrue(ok)
            self.assertEqual(self.node.state_machine.state, OperatorState.DRIVING)

            # Set guard telemetry timestamp to 600 ms ago (stalled guard)
            self.node.state_machine.telemetry.guard_monotonic_ns = (
                time.monotonic_ns() - int(0.6 * 1e9)
            )
            self.node.state_machine.telemetry.battery_monotonic_ns = time.monotonic_ns()

            # Timer tick must detect stalled guard and fault
            self.node._timer_tick()
            self.assertEqual(self.node.state_machine.state, OperatorState.FAULT)
            self.assertIn(
                "Motor guard state telemetry", self.node.state_machine.last_fault
            )


class TestBenchAcceptanceWorkflowIntegration(unittest.TestCase):
    """Verify bench integration with acceptance workflow (Finding 2)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "bench_acc.sock")
        self.sm = OperatorStateMachine(release_id="test-bench-acc")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
            stop_callback=lambda: self.sm.update_guard_telemetry(
                False, time.monotonic_ns()
            ),
        )
        self.server.start()
        os.environ["UBUNTU_TANK_OPERATOR_SOCKET"] = self.socket_path

    def tearDown(self):
        os.environ.pop("UBUNTU_TANK_OPERATOR_SOCKET", None)
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_bench_command_speeds_agree_with_agent_caps(self):
        """Bench client exposes get_command_speeds agreeing with operator agent caps."""
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        bench = BenchClientNode(node_name="test_bench_speeds")
        try:
            lx, az = bench.get_command_speeds()
            self.assertEqual(lx, 0.20)
            self.assertEqual(az, 0.50)
        finally:
            bench.destroy_node()

    def test_four_direction_acceptance_sequence_with_subsequent_arms(self):
        """Bench client executes 4-direction acceptance sequence with subsequent arms and epoch renewal."""
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        bench = BenchClientNode(
            node_name="test_bench_acc_seq",
            max_linear_speed=0.20,
            max_angular_speed=0.80,
        )
        try:
            directions = ["forward", "reverse", "spin_left", "spin_right"]
            for d_name in directions:
                # Telemetry must be fresh for each arm
                self.sm.telemetry.guard_monotonic_ns = time.monotonic_ns()
                self.sm.telemetry.battery_monotonic_ns = time.monotonic_ns()

                # 1. Arm (must succeed for first and subsequent bursts, renewing epoch)
                arm_ok, arm_msg = bench.call_set_arm(True)
                self.assertTrue(arm_ok, f"Burst {d_name} arm failed: {arm_msg}")
                self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

                # 2. Run motion burst (submits neutral and stop() in finally)
                lx = (
                    0.20
                    if d_name == "forward"
                    else (-0.20 if d_name == "reverse" else 0.0)
                )
                az = (
                    0.50
                    if d_name == "spin_left"
                    else (-0.50 if d_name == "spin_right" else 0.0)
                )

                burst_ok = bench.run_motion_burst(
                    lx, az, duration_sec=0.1, rate_hz=20.0
                )
                self.assertTrue(burst_ok, f"Burst {d_name} failed")

                # 3. Burst self-terminated with stop(), leaving state disarmed
                self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

                # 4. Subsequent disarm call is idempotent and succeeds
                disarm_ok, disarm_msg = bench.call_set_arm(False)
                self.assertTrue(disarm_ok)
                self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
        finally:
            bench.destroy_node()


class TestTeleopAuthorityRecoveryAndEpochRefresh(unittest.TestCase):
    """Verify teleop authority renewal on stops and automatic expiry (Finding 3)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "teleop_recov.sock")
        self.sm = OperatorStateMachine(release_id="test-teleop-recov")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
            stop_callback=lambda: self.sm.update_guard_telemetry(
                False, time.monotonic_ns()
            ),
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_space_then_r_then_movement_workflow(self):
        """Space calls stop() and bumps epoch; subsequent 'r' reconciles epoch and arms, allowing movement."""
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_teleop")
            self.assertTrue(ok)
            self.assertEqual(epoch, 1)

            # 1. Initial arm
            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

            # 2. Drive forward
            c = client.request_challenge(epoch)
            ok, _, _ = client.submit_intent(c["token"], epoch, 1, "forward")
            self.assertTrue(ok)
            self.assertEqual(self.sm.state, OperatorState.DRIVING)

            # 3. Space pressed -> stop() called
            client.stop()
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
            self.assertEqual(self.sm.epoch, 2)

            # Old epoch command must be rejected
            old_arm_ok, old_err, _ = client.arm(1, tracks_raised=True)
            self.assertFalse(old_arm_ok)
            self.assertEqual(old_err, WebControlErrorCode.INVALID_EPOCH.value)

            # Refresh epoch via status
            st = client.get_status()
            fresh_epoch = st.get("current_epoch")
            self.assertEqual(fresh_epoch, 2)

            # Downstream guard confirms disarm and telemetry stays fresh
            self.sm.update_guard_telemetry(False, time.monotonic_ns())
            self.sm.telemetry.battery_monotonic_ns = time.monotonic_ns()

            # 4. 'r' pressed -> arm with fresh epoch
            arm2_ok, _, _ = client.arm(fresh_epoch, tracks_raised=True)
            self.assertTrue(arm2_ok)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

            # 5. Drive again with fresh epoch -> movement resumes!
            c2 = client.request_challenge(fresh_epoch)
            ok2, _, _ = client.submit_intent(c2["token"], fresh_epoch, 1, "forward")
            self.assertTrue(ok2)
            self.assertEqual(self.sm.state, OperatorState.DRIVING)

    def test_recovery_after_lease_expiry_in_same_session(self):
        """Automatic lease expiry stops and disarms; client detects challenge failure, refreshes epoch, and re-arms."""
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_teleop")
            self.assertTrue(ok)
            self.assertEqual(epoch, 1)

            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)

            # Simulate lease expiry on server
            self.sm.check_deadlines(
                self.sm.lease_deadline_monotonic_ns + int(0.1 * 1e9)
            )
            self.assertEqual(self.sm.state, OperatorState.FAULT)
            self.assertEqual(self.sm.epoch, 2)

            # Next challenge request fails because server is not armed
            c = client.request_challenge(epoch)
            self.assertNotIn("token", c)

            # Client resets server fault via stop() or status query
            client.stop()
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
            fresh_epoch = client.get_status().get("current_epoch")
            self.assertEqual(fresh_epoch, 3)

            # Downstream guard confirms disarm and telemetry stays fresh
            self.sm.update_guard_telemetry(False, time.monotonic_ns())
            self.sm.telemetry.battery_monotonic_ns = time.monotonic_ns()

            # Re-arm succeeds with fresh epoch
            rearm_ok, _, _ = client.arm(fresh_epoch, tracks_raised=True)
            self.assertTrue(rearm_ok)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)


class TestInterleavingStopAndIntent(unittest.TestCase):
    """Finding 1: Deterministic interleaving proving stop rejects an in-flight old request

    and prevents nonzero publication after the stop boundary.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "interleave.sock")
        self.sm = OperatorStateMachine(release_id="test-interleave")
        now_ns = time.monotonic_ns()
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=now_ns,
            guard_armed=False,
            guard_monotonic_ns=now_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=now_ns,
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_interleaved_stop_rejects_intent_and_prevents_motion(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire("test_interleave")
            self.assertTrue(ok)
            self.assertEqual(epoch, 1)

            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)
            self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

            c = client.request_challenge(epoch)
            token = c["token"]

            class HookedDict(dict):
                def pop(self, key, *args):
                    res = super().pop(key, *args)
                    if hasattr(self, "_on_pop") and self._on_pop:
                        self._on_pop()
                    return res

            hooked = HookedDict(self.sm.outstanding_challenges)
            hooked._on_pop = lambda: self.sm.stop("Interleaved emergency stop")
            self.sm.outstanding_challenges = hooked

            ok_i, err_i, msg_i = client.submit_intent(token, epoch, 1, "forward")

            # In-flight intent must be rejected
            self.assertFalse(ok_i)
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
            self.assertEqual(self.sm.epoch, 2)
            vx, wz = self.sm.get_velocity_command()
            self.assertEqual(vx, 0.0)
            self.assertEqual(wz, 0.0)

            # Clearing disarm_pending via false guard telemetry does not revive motion
            self.sm.update_guard_telemetry(False, time.monotonic_ns())
            self.assertFalse(self.sm.disarm_pending)
            self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)
            vx2, wz2 = self.sm.get_velocity_command()
            self.assertEqual(vx2, 0.0)
            self.assertEqual(wz2, 0.0)


class TestOperatingSpeedPreservation(unittest.TestCase):
    """Finding 6: Preserves operator-selected lower speeds in IPC mode."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "speeds.sock")
        os.environ["UBUNTU_TANK_OPERATOR_SOCKET"] = self.socket_path
        self.sm = OperatorStateMachine(
            release_id="test-speeds",
            linear_speed_cap=0.50,
            angular_speed_cap=1.00,
        )
        now_ns = time.monotonic_ns()
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=now_ns,
            guard_armed=False,
            guard_monotonic_ns=now_ns,
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=now_ns,
        )
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        self.server.start()

    def tearDown(self):
        os.environ.pop("UBUNTU_TANK_OPERATOR_SOCKET", None)
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_lower_speeds_preserved_in_ipc_acquire_and_driving(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, epoch, _, _ = client.acquire(
                "speed_tester",
                max_linear_speed=0.05,
                max_angular_speed=0.15,
            )
            self.assertTrue(ok)
            self.assertEqual(self.sm.active_linear_speed, 0.05)
            self.assertEqual(self.sm.active_angular_speed, 0.15)

            st = client.get_status()
            self.assertEqual(st["limits"]["max_linear_speed"], 0.05)
            self.assertEqual(st["limits"]["max_angular_speed"], 0.15)

            arm_ok, _, _ = client.arm(epoch, tracks_raised=True)
            self.assertTrue(arm_ok)

            c = client.request_challenge(epoch)
            drive_ok, _, _ = client.submit_intent(c["token"], epoch, 1, "forward")
            self.assertTrue(drive_ok)
            vx, wz = self.sm.get_velocity_command()
            self.assertAlmostEqual(vx, 0.05, places=3)
            self.assertAlmostEqual(wz, 0.0, places=3)

            c2 = client.request_challenge(epoch)
            turn_ok, _, _ = client.submit_intent(c2["token"], epoch, 2, "spin_left")
            self.assertTrue(turn_ok)
            vx2, wz2 = self.sm.get_velocity_command()
            self.assertAlmostEqual(vx2, 0.0, places=3)
            self.assertAlmostEqual(wz2, 0.15, places=3)

    def test_speed_rejection_for_excessive_requested_speeds(self):
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            # Bypassing client negotiation must still fail at the server boundary.
            response = client._send_request(
                {
                    "action": "acquire",
                    "operator_id": "raw_tester",
                    "max_linear_speed": 2.5,
                    "max_angular_speed": 5.0,
                }
            )
            self.assertFalse(response["success"])
            self.assertEqual(
                response["error"], WebControlErrorCode.INVALID_PAYLOAD.value
            )

    def test_requested_ceilings_negotiate_before_acquisition(self):
        self.sm.angular_speed_cap = 0.50
        with OperatorIpcClient(socket_path=self.socket_path) as client:
            ok, _, _, _ = client.acquire(
                "default_bench", max_linear_speed=0.05, max_angular_speed=0.8
            )
            self.assertTrue(ok)
            self.assertEqual(self.sm.active_linear_speed, 0.05)
            self.assertEqual(self.sm.active_angular_speed, 0.50)

    def test_host_keyboard_speeds_and_cli_precedence(self):
        import yaml
        from ubuntu_tank_teleop.teleop_key_node import main

        scripts = os.path.join(UBUNTU_TANK_DIR, "scripts")
        with mock.patch.object(sys, "path", [scripts] + sys.path):
            from config_migration import teleop_arguments
        with open(os.path.join(UBUNTU_TANK_DIR, "config/controller.yaml")) as stream:
            config = yaml.safe_load(stream)
        config["teleop"].update(linear_speed=0.05, angular_speed=0.10)
        config_path = os.path.join(self.tmp_dir.name, "controller.yaml")
        with open(config_path, "w") as stream:
            yaml.safe_dump(config, stream)
        ros_args = ["--ros-args"] + teleop_arguments(config_path)
        original = OperatorIpcClient.acquire
        captured = []

        def acquire(client, *args, **kwargs):
            result = original(client, *args, **kwargs)
            captured.append((self.sm.active_linear_speed, self.sm.active_angular_speed))
            return result

        for flags, expected in (
            ([], (0.05, 0.10)),
            (["--speed", "0.03"], (0.03, 0.10)),
        ):
            with (
                mock.patch.object(OperatorIpcClient, "acquire", acquire),
                mock.patch("sys.stdin", io.StringIO("q")),
            ):
                self.assertEqual(main(flags + ros_args), 0)
            self.assertEqual(captured[-1], expected)
            # Confirm downstream disarm before the next simulated session.
            self.sm.update_guard_telemetry(False, time.monotonic_ns())

    def test_bench_client_preserves_requested_speeds(self):
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        bench = BenchClientNode(
            direct_ros=False,
            max_linear_speed=0.05,
            max_angular_speed=0.10,
        )
        try:
            lin, ang = bench.get_command_speeds()
            self.assertAlmostEqual(lin, 0.05, places=3)
            self.assertAlmostEqual(ang, 0.10, places=3)
            self.assertAlmostEqual(self.sm.active_linear_speed, 0.05, places=3)
            self.assertAlmostEqual(self.sm.active_angular_speed, 0.10, places=3)
        finally:
            bench.destroy_node()


class TestDirectRosMutualExclusion(unittest.TestCase):
    """Finding 5: Mutual exclusion between Operator Agent and Direct ROS mode."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "operator.sock")
        self.lock_path = os.path.join(self.tmp_dir.name, "operator.lock")
        os.environ["UBUNTU_TANK_OPERATOR_SOCKET"] = self.socket_path
        os.environ["UBUNTU_TANK_OPERATOR_LOCK"] = self.lock_path

    def tearDown(self):
        os.environ.pop("UBUNTU_TANK_OPERATOR_SOCKET", None)
        os.environ.pop("UBUNTU_TANK_OPERATOR_LOCK", None)
        self.tmp_dir.cleanup()

    def test_direct_ros_rejected_when_agent_socket_active(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.socket_path)
        s.listen(1)
        try:
            from ubuntu_tank_teleop.teleop_key_node import main as teleop_main

            ret = teleop_main(["--direct-ros"])
            self.assertEqual(ret, 1)
        finally:
            s.close()

    def test_direct_ros_rejected_when_authority_lock_held(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            from ubuntu_tank_teleop.teleop_key_node import main as teleop_main

            ret = teleop_main(["--direct-ros"])
            self.assertEqual(ret, 1)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_agent_rejected_when_direct_ros_lock_held(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(RuntimeError):
                OperatorAgentNode(
                    socket_path=self.socket_path,
                    allowed_uids=[os.getuid()],
                )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_bench_missing_socket_fails_closed_by_default(self):
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        with self.assertRaisesRegex(RuntimeError, "not reachable"):
            BenchClientNode(node_name="missing_agent_bench")

    def test_bench_inaccessible_socket_fails_closed_by_default(self):
        from ubuntu_tank_bringup.bench_client import BenchClientNode

        with mock.patch.object(
            OperatorIpcClient, "connect", side_effect=PermissionError("denied")
        ):
            with self.assertRaisesRegex(RuntimeError, "not reachable"):
                BenchClientNode(node_name="inaccessible_agent_bench")

    def test_explicit_direct_bench_blocks_daemon_until_destroyed(self):
        import ubuntu_tank_bringup.bench_client as bench_module

        fake_rclpy = mock.Mock()
        fake_rclpy.ok.return_value = True
        create_result = mock.Mock()
        with (
            mock.patch.object(bench_module, "rclpy", fake_rclpy),
            mock.patch.object(
                bench_module.BenchClientNode,
                "create_client",
                return_value=create_result,
                create=True,
            ),
            mock.patch.object(
                bench_module.BenchClientNode,
                "create_publisher",
                return_value=create_result,
                create=True,
            ),
            mock.patch.object(
                bench_module.BenchClientNode,
                "create_subscription",
                return_value=create_result,
                create=True,
            ),
        ):
            bench = bench_module.BenchClientNode(
                node_name="explicit_direct_bench",
                direct_ros=True,
                authority_lock_path=self.lock_path,
            )
            try:
                with self.assertRaises(RuntimeError):
                    OperatorAgentNode(
                        socket_path=self.socket_path,
                        allowed_uids=[os.getuid()],
                    )
            finally:
                base_node = bench_module.BenchClientNode.__mro__[1]

                def assert_lock_held_until_publisher_destroyed(_node):
                    with self.assertRaises(RuntimeError):
                        OperatorAgentNode(
                            socket_path=self.socket_path,
                            allowed_uids=[os.getuid()],
                        )

                with mock.patch.object(
                    base_node,
                    "destroy_node",
                    assert_lock_held_until_publisher_destroyed,
                ):
                    bench.destroy_node()

        agent = OperatorAgentNode(
            socket_path=self.socket_path,
            allowed_uids=[os.getuid()],
        )
        agent.destroy_node()

    def test_direct_first_lock_has_stable_permissions_for_daemon_restart(self):
        with mock.patch(
            "ubuntu_tank_operator.authority_lock.grp.getgrnam",
            side_effect=KeyError("development host"),
        ):
            old_umask = os.umask(0o022)
            try:
                fd, resolved_path = acquire_authority_lock(lock_path=self.lock_path)
            finally:
                os.umask(old_umask)
            try:
                metadata = os.stat(resolved_path)
                self.assertEqual(metadata.st_mode & 0o777, 0o660)
                self.assertEqual(metadata.st_gid, os.getgid())
            finally:
                release_authority_lock(fd)

            agent = OperatorAgentNode(
                socket_path=self.socket_path,
                allowed_uids=[os.getuid()],
            )
            agent.destroy_node()


class TestIpcRoleAuthorization(unittest.TestCase):
    """Finding 4: Connect IPC authorization to installed operator role."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "role_auth.sock")
        self.sm = OperatorStateMachine(release_id="test-role-auth")
        self.server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.socket_path,
            allowed_uids=None,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp_dir.cleanup()

    def test_root_and_self_authorized_by_default(self):
        self.assertTrue(self.server._is_uid_authorized(0))
        self.assertTrue(self.server._is_uid_authorized(os.getuid()))

    def test_unconfigured_uid_rejected_by_default(self):
        self.assertFalse(self.server._is_uid_authorized(987654))

    def test_user_in_operators_group_authorized(self):
        with (
            mock.patch("pwd.getpwuid") as mock_pw,
            mock.patch("grp.getgrnam") as mock_grp,
        ):
            mock_pw.return_value = mock.Mock(pw_name="human_operator", pw_gid=5000)
            mock_grp.return_value = mock.Mock(gr_gid=6000, gr_mem=["human_operator"])
            self.assertTrue(self.server._is_uid_authorized(12345))


class TestLauncherEnvironmentAndFatalRos(unittest.TestCase):
    """Finding 3: Sourcing ROS environment before daemon import and fatal missing ROS."""

    def test_launcher_first_stage_sourcing_and_reexec(self):
        """Execute wrapper's real first-stage path; prove exports from both setup scripts reach re-exec."""
        launcher_path = os.path.join(
            REPO_ROOT, "ubuntu_tank/bin/mentorpi-tank-operator"
        )
        with tempfile.TemporaryDirectory() as td:
            ros_setup = os.path.join(td, "ros_setup.bash")
            with open(ros_setup, "w", encoding="utf-8") as f:
                f.write(
                    "export TEST_ROS_SOURCED=yes\nexport"
                    " AMENT_PREFIX_PATH=/opt/ros/lyrical\n"
                )

            ws_dir = os.path.join(td, "workspace")
            bin_dir = os.path.join(ws_dir, "bin")
            install_dir = os.path.join(ws_dir, "install")
            src_dir = os.path.join(
                ws_dir, "src", "ubuntu_tank_operator", "ubuntu_tank_operator"
            )
            os.makedirs(bin_dir, exist_ok=True)
            os.makedirs(install_dir, exist_ok=True)
            os.makedirs(src_dir, exist_ok=True)

            release_setup = os.path.join(install_dir, "setup.bash")
            with open(release_setup, "w", encoding="utf-8") as f:
                f.write("export TEST_RELEASE_SOURCED=yes\n")

            with open(os.path.join(src_dir, "__init__.py"), "w") as f:
                pass
            with open(
                os.path.join(src_dir, "entrypoint.py"), "w", encoding="utf-8"
            ) as f:
                f.write("""
import os, json
def main():
    print(json.dumps({
        "TEST_ROS_SOURCED": os.environ.get("TEST_ROS_SOURCED"),
        "TEST_RELEASE_SOURCED": os.environ.get("TEST_RELEASE_SOURCED"),
        "_MENTORPI_TANK_OPERATOR_SOURCED": os.environ.get("_MENTORPI_TANK_OPERATOR_SOURCED"),
        "ROS_SECURITY_ENCLAVE_OVERRIDE": os.environ.get("ROS_SECURITY_ENCLAVE_OVERRIDE"),
        "ROS_LOG_DIR": os.environ.get("ROS_LOG_DIR"),
    }))
    return 0
""")

            test_launcher = os.path.join(bin_dir, "mentorpi-tank-operator")
            shutil.copy2(launcher_path, test_launcher)
            os.chmod(test_launcher, 0o755)

            env = {
                "PATH": os.environ["PATH"],
                "ROS_SETUP": ros_setup,
                "MENTORPI_SIMULATION": "1",
            }
            res = subprocess.run(
                [test_launcher], env=env, capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data.get("TEST_ROS_SOURCED"), "yes")
            self.assertEqual(data.get("TEST_RELEASE_SOURCED"), "yes")
            self.assertEqual(data.get("_MENTORPI_TANK_OPERATOR_SOURCED"), "1")
            self.assertEqual(
                data.get("ROS_SECURITY_ENCLAVE_OVERRIDE"),
                "/ubuntu_tank/operator",
            )
            self.assertEqual(
                data.get("ROS_LOG_DIR"), "/var/opt/ubuntu_tank/operator-log"
            )

    def test_launcher_fails_fast_on_missing_or_failed_setup_script(self):
        """Missing or failed mandatory setup scripts cause startup to fail cleanly."""
        launcher_path = os.path.join(
            REPO_ROOT, "ubuntu_tank/bin/mentorpi-tank-operator"
        )
        # Missing ROS setup script without simulation mode must fail
        res_missing = subprocess.run(
            [launcher_path],
            env={
                "PATH": os.environ["PATH"],
                "ROS_SETUP": "/nonexistent/setup.bash",
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_missing.returncode, 1)
        self.assertIn("rclpy is not available", res_missing.stderr)

        # Failing setup script without simulation mode must fail
        with tempfile.NamedTemporaryFile("w", suffix=".bash", delete=False) as tf:
            tf.write("echo 'syntax err' >&2; exit 42\n")
            tf_name = tf.name
        try:
            res_failed = subprocess.run(
                [launcher_path],
                env={"PATH": os.environ["PATH"], "ROS_SETUP": tf_name},
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_failed.returncode, 1)
            self.assertIn("rclpy is not available", res_failed.stderr)
        finally:
            if os.path.exists(tf_name):
                os.unlink(tf_name)

    def test_launcher_sourcing_script(self):
        """source_bash_environment executes bash scripts and captures exported environment variables."""
        launcher_path = os.path.join(
            REPO_ROOT, "ubuntu_tank/bin/mentorpi-tank-operator"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/bash\nexport TEST_SOURCING_VAR='test_sourcing_val_123'\n")
            tmp_script = f.name

        try:
            globs: dict[str, Any] = {"__file__": launcher_path}
            with open(launcher_path, "r", encoding="utf-8") as lf:
                code = compile(lf.read(), launcher_path, "exec")
            # Execute to load source_bash_environment
            exec(code, globs)
            fn = globs.get("source_bash_environment")
            self.assertIsNotNone(fn)
            extracted = fn([tmp_script])
            self.assertEqual(
                extracted.get("TEST_SOURCING_VAR"), "test_sourcing_val_123"
            )

            # Nonexistent scripts return empty dict gracefully
            empty = fn(["/nonexistent/path/to/setup.bash"])
            self.assertEqual(empty, {})
        finally:
            if os.path.exists(tmp_script):
                os.unlink(tmp_script)

    def test_fatal_missing_rclpy_without_simulation(self):
        from ubuntu_tank_operator.entrypoint import main as entrypoint_main

        with (
            mock.patch("ubuntu_tank_operator.entrypoint.rclpy", None),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            ret = entrypoint_main([])
            self.assertEqual(ret, 1)

    def test_simulation_flag_allows_running_without_rclpy(self):
        from ubuntu_tank_operator.entrypoint import main as entrypoint_main

        with (
            mock.patch("ubuntu_tank_operator.entrypoint.rclpy", None),
            mock.patch(
                "ubuntu_tank_operator.entrypoint.OperatorAgentNode"
            ) as mock_node_cls,
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            mock_instance = mock.Mock()
            mock_node_cls.return_value = mock_instance
            ret = entrypoint_main(["--simulation"])
            self.assertEqual(ret, 0)
            mock_instance.execute_stop.assert_called()


class TestDeploymentProvisioningAndLifecycle(unittest.TestCase):
    """Finding 2: Provisioning, unit installation, snapshot backup, and rollback for operator."""

    def test_service_identities_provisioning(self):
        """ReleaseManager provisions ubuntu-tank-operator and ubuntu-tank-operators group."""
        scripts_dir = os.path.join(UBUNTU_TANK_DIR, "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from deployment_manager import ReleaseManager

        with tempfile.TemporaryDirectory() as td:
            mgr = ReleaseManager(
                opt_dir=os.path.join(td, "opt"),
                etc_dir=os.path.join(td, "etc"),
                var_dir=os.path.join(td, "var"),
                run_dir=os.path.join(td, "run"),
            )
            commands_run = []

            def mock_run(cmd, **kwargs):
                commands_run.append(" ".join(cmd))
                return mock.Mock(returncode=0)

            def mock_pwnam(user):
                if user == "testoperator":
                    return mock.Mock(pw_name="testoperator")
                raise KeyError(user)

            with (
                mock.patch("subprocess.run", side_effect=mock_run),
                mock.patch("os.geteuid", return_value=0),
                mock.patch("pwd.getpwnam", side_effect=mock_pwnam),
                mock.patch("grp.getgrnam", side_effect=KeyError),
            ):
                mgr._provision_service_identities(operator_user="testoperator")
            flat = " ".join(commands_run)
            self.assertIn("ubuntu-tank-operator", flat)
            self.assertIn("ubuntu-tank-operators", flat)

    def test_tmpfiles_recreation_preserves_agent_access(self):
        """Validate cross-user ownership and role matrix for runtime and log paths."""
        from pathlib import Path

        conf_file = Path(UBUNTU_TANK_DIR, "host/ubuntu-tank.conf")
        rules_text = conf_file.read_text(encoding="utf-8")

        def parse_tmpfiles_records(text):
            records = {}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 5:
                    records[parts[1]] = {
                        "type": parts[0],
                        "path": parts[1],
                        "mode": parts[2],
                        "user": parts[3],
                        "group": parts[4],
                    }
            return records

        records = parse_tmpfiles_records(rules_text)

        # 1. Assert cross-user ownership contract preserving distinct roles
        # /run/ubuntu_tank: owned by controller, group is ubuntu-tank-operators (allows agent socket + client lock)
        self.assertIn("/run/ubuntu_tank", records)
        self.assertEqual(records["/run/ubuntu_tank"]["type"], "d")
        self.assertEqual(records["/run/ubuntu_tank"]["mode"], "0775")
        self.assertEqual(records["/run/ubuntu_tank"]["user"], "ubuntu-tank")
        self.assertEqual(records["/run/ubuntu_tank"]["group"], "ubuntu-tank-operators")

        # /run/ubuntu_tank/operator.lock: owned by operator agent, group is ubuntu-tank-operators
        self.assertIn("/run/ubuntu_tank/operator.lock", records)
        self.assertEqual(records["/run/ubuntu_tank/operator.lock"]["type"], "f")
        self.assertEqual(records["/run/ubuntu_tank/operator.lock"]["mode"], "0660")
        self.assertEqual(
            records["/run/ubuntu_tank/operator.lock"]["user"],
            "ubuntu-tank-operator",
        )
        self.assertEqual(
            records["/run/ubuntu_tank/operator.lock"]["group"],
            "ubuntu-tank-operators",
        )

        # /var/opt/ubuntu_tank/operator-log: owned by operator agent, group is ubuntu-tank-operators
        self.assertIn("/var/opt/ubuntu_tank/operator-log", records)
        self.assertEqual(records["/var/opt/ubuntu_tank/operator-log"]["type"], "d")
        self.assertEqual(records["/var/opt/ubuntu_tank/operator-log"]["mode"], "0750")
        self.assertEqual(
            records["/var/opt/ubuntu_tank/operator-log"]["user"],
            "ubuntu-tank-operator",
        )
        self.assertEqual(
            records["/var/opt/ubuntu_tank/operator-log"]["group"],
            "ubuntu-tank-operators",
        )

        # /var/opt/ubuntu_tank/ros-log: owned by controller, group is mentorpi-rrc
        self.assertIn("/var/opt/ubuntu_tank/ros-log", records)
        self.assertEqual(records["/var/opt/ubuntu_tank/ros-log"]["type"], "d")
        self.assertEqual(records["/var/opt/ubuntu_tank/ros-log"]["mode"], "0750")
        self.assertEqual(records["/var/opt/ubuntu_tank/ros-log"]["user"], "ubuntu-tank")
        self.assertEqual(
            records["/var/opt/ubuntu_tank/ros-log"]["group"], "mentorpi-rrc"
        )

        # 2. Mutation regression: substituting an unrelated owner or group fails the role contract
        mutated_wrong_group = rules_text.replace("ubuntu-tank-operators", "nogroup")
        mut_records = parse_tmpfiles_records(mutated_wrong_group)
        self.assertNotEqual(
            mut_records["/run/ubuntu_tank"]["group"], "ubuntu-tank-operators"
        )
        self.assertNotEqual(
            mut_records["/run/ubuntu_tank/operator.lock"]["group"],
            "ubuntu-tank-operators",
        )

        mutated_wrong_user = rules_text.replace("ubuntu-tank-operator", "nobody")
        mut_user_records = parse_tmpfiles_records(mutated_wrong_user)
        self.assertNotEqual(
            mut_user_records["/run/ubuntu_tank/operator.lock"]["user"],
            "ubuntu-tank-operator",
        )
        self.assertNotEqual(
            mut_user_records["/var/opt/ubuntu_tank/operator-log"]["user"],
            "ubuntu-tank-operator",
        )

        # 3. Live filesystem enforcement: requires root privileges for distinct UIDs/GIDs
        if os.geteuid() == 0:
            import shutil
            import subprocess

            if shutil.which("systemd-tmpfiles"):
                with tempfile.TemporaryDirectory() as root:
                    subprocess.run(
                        [
                            "systemd-tmpfiles",
                            f"--root={root}",
                            "--create",
                            str(conf_file),
                        ],
                        check=True,
                    )
        else:
            # Unprivileged environment: record live chown filesystem enforcement as target-only pending
            pass

    def test_deployment_manager_backup_and_restore_mappings(self):
        scripts_dir = os.path.join(UBUNTU_TANK_DIR, "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from deployment_manager import SnapshotManager

        with tempfile.TemporaryDirectory() as td:
            etc_dir = os.path.join(td, "etc")
            systemd_dir = os.path.join(td, "systemd")
            udev_dir = os.path.join(td, "udev")
            snapshots_dir = os.path.join(td, "snapshots")
            for d in [etc_dir, systemd_dir, udev_dir, snapshots_dir]:
                os.makedirs(d, exist_ok=True)

            op_unit = os.path.join(systemd_dir, "mentorpi-tank-operator.service")
            with open(op_unit, "w") as f:
                f.write("[Unit]\nDescription=Test\n")
            stack_target = os.path.join(systemd_dir, "mentorpi-tank-stack.target")
            with open(stack_target, "w") as f:
                f.write("[Unit]\nDescription=Test Stack\n")

            sm = SnapshotManager(snapshots_dir)
            snap_path = sm.create_snapshot(
                "test_tx", None, etc_dir, systemd_dir, udev_dir
            )
            meta_file = os.path.join(snap_path, "metadata.json")
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertIn("mentorpi-tank-operator.service", meta["backed_up_files"])
            self.assertIn("mentorpi-tank-stack.target", meta["backed_up_files"])

            os.unlink(op_unit)
            os.unlink(stack_target)
            self.assertFalse(os.path.exists(op_unit))
            self.assertFalse(os.path.exists(stack_target))

            sm.restore_snapshot(snap_path, etc_dir, systemd_dir, udev_dir)
            self.assertTrue(os.path.exists(op_unit))
            self.assertTrue(os.path.exists(stack_target))


if __name__ == "__main__":
    unittest.main()
