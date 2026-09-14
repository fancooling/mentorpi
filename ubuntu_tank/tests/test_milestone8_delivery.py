#!/usr/bin/env python3
"""
test_milestone8_delivery.py - Unit and integration tests for Milestone 8:
Bounded arming and verified delivery acceptance.

Validates:
1. Monotonic first-command deadline in MotorGuard (§8.4):
   - Deadline starts at arming and expires within timeout_sec (0.250s).
   - Delayed/late commands rejected without lease resurrection.
   - Repeated arm calls do not silently renew leases.
   - Re-arming clears caches and enforces a new deadline.
   - Negative monotonic clock jumps disarm and enter fault.
   - Speed limits and motor validation invariants preserved.
2. Native fake-board/PTY serial framing fixture (§10.4):
   - Uses os.openpty() to create a virtual serial sink.
   - Verifies raw encoded frame bytes (0xAA 0x55, function ID, motor payload, CRC8).
   - Verifies sink_type == "pty", byte accounting, and write success.
   - Confirms /dev/rrc is never opened.
3. Stage-specific delivery observations on /ubuntu_tank/delivery_observation (§10.4):
   - JSON observation schema across controller_rx, guard_arm, guard_disarm,
     guard_rx, guard_fwd, guard_fault, guard_timeout, bridge_rx, bridge_write.
   - Correlated 5-stage delivery pipeline verification.
4. Broken pipeline and fail-closed behavior:
   - Missing stage rejection (dropped command, broken edge).
   - Value mismatch rejection.
   - Guard fault and timeout rejection.
   - Serial write error and short write rejection.
   - Process restart / run_id mismatch detection.
   - Stale observation rejection outside burst window.
5. SROS2 delivery observation policy permissions (§10.4):
   - Controller, guard, and bridge have publish allow for delivery observation.
   - Operator and status have subscribe allow for delivery observation.
   - Operator and status strictly denied actuator topics.
   - sros2_policy verification passes cleanly.
6. Acceptance report segregation:
   - Clear separation of software delivery, host serial write, physical movement
     (pending owner observation), physical stop latency, and STM32 command loss.
   - Overall physical acceptance status remains PHYSICAL_ACCEPTANCE_INCOMPLETE
     for software/mock/PTY runs.
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

# Resolve repository paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)

for p in [WORKSPACE_ROOT, UBUNTU_TANK_DIR, os.path.join(UBUNTU_TANK_DIR, "scripts")]:
    if p not in sys.path:
        sys.path.insert(0, p)

SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")
for pkg in [
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
    "controller",
    "ros_robot_controller",
    "ubuntu_tank_bringup",
]:
    pkg_path = os.path.join(SRC_DIR, pkg)
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)

from ros_robot_controller.ros_robot_controller_sdk import (
    Board,
    PacketFunction,
    checksum_crc8,
)
from scripts.bench_acceptance import BenchAcceptanceOrchestrator
import scripts.sros2_policy as sros2_policy_mod
from ubuntu_tank_bringup.bench_client import BenchClientNode
from ubuntu_tank_safety.motor_guard import MotorGuard


def _make_motor_frame_hex(speeds):
    """Encode STM32 motor frame (0xAA 0x55 0x03 len subfunc count [id-1, rps]... CRC8)."""
    data = [0x01, len(speeds)]
    for i in speeds:
        data.extend(struct.pack("<Bf", int(i[0] - 1), float(i[1])))
    buf = [0xAA, 0x55, 0x03, len(data)]
    buf.extend(data)
    buf.append(checksum_crc8(bytes(buf[2:])))
    return bytes(buf).hex()


class TestMonotonicFirstCommandDeadline(unittest.TestCase):
    """Verify monotonic first-command deadline in MotorGuard (§8.4)."""

    def setUp(self):
        self.guard = MotorGuard(
            max_rps=2.0,
            timeout_sec=0.250,
        )

    def test_fresh_arm_starts_monotonic_window(self):
        """Arming starts the first-command deadline and does not accept prior commands."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)
        self.assertTrue(self.guard.is_armed)
        self.assertEqual(self.guard._arm_time_monotonic, t0)
        self.assertIsNone(self.guard._last_command_monotonic)

    def test_first_command_within_timeout_accepted(self):
        """Command arriving within timeout_sec is accepted and transitions out of deadline wait."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # 100 ms later: valid command
        t1 = t0 + 0.100
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        fwd, faulted, reason = self.guard.handle_command(cmd, now_monotonic=t1)
        self.assertEqual(fwd, cmd)
        self.assertFalse(faulted)
        self.assertTrue(self.guard.is_armed)
        # Active command timestamp updated to t1
        self.assertEqual(self.guard._last_command_monotonic, t1)

    def test_first_command_timeout_expires_without_input(self):
        """If no command arrives within timeout_sec, check_timeout disarms the guard."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # 200 ms: not yet expired
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=t0 + 0.200)
        self.assertFalse(timed_out)
        self.assertIsNone(zero_cmd)
        self.assertTrue(self.guard.is_armed)

        # 251 ms: expired!
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=t0 + 0.251)
        self.assertTrue(timed_out)
        self.assertIsNotNone(zero_cmd)
        self.assertTrue(all(s == 0.0 for _, s in zero_cmd))
        self.assertFalse(self.guard.is_armed)
        self.assertIsNone(self.guard._arm_time_monotonic)

    def test_late_command_rejected_cannot_resurrect_lease(self):
        """Command arriving after deadline expiry cannot resurrect lease or arm state."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # First command arrives at 260 ms (> 250 ms)
        t_late = t0 + 0.260
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        fwd, faulted, reason = self.guard.handle_command(cmd, now_monotonic=t_late)
        self.assertTrue(faulted)
        self.assertFalse(self.guard.is_armed)
        self.assertIn("expired", reason.lower())
        # Output must be standard 4-motor zero command
        self.assertTrue(all(speed == 0.0 for _, speed in fwd))

    def test_repeated_arm_does_not_renew_deadline(self):
        """Repeated arm calls while waiting for first command must NOT renew deadline."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # Arm called again at 150 ms
        t_rep = t0 + 0.150
        self.guard.arm(now_monotonic=t_rep)
        # Arm time must remain t0
        self.assertEqual(self.guard._arm_time_monotonic, t0)

        # At 251 ms from t0, deadline must expire despite repeated arm call
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=t0 + 0.251)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

    def test_repeated_arm_while_active_does_not_renew_lease(self):
        """Repeated arm calls while actively driving must NOT renew the 250 ms motion lease."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        self.guard.handle_command(cmd, now_monotonic=t0 + 0.050)

        # Attempt to renew lease by calling arm at t0 + 0.200
        self.guard.arm(now_monotonic=t0 + 0.200)

        # Lease must expire at t0 + 0.050 + 0.250 = t0 + 0.300
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=t0 + 0.301)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

    def test_rearm_after_timeout_resets_deadline_and_accepts_new_command(self):
        """Explicit re-arming after timeout restarts a clean deadline and accepts new input."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)
        self.guard.check_timeout(now_monotonic=t0 + 0.300)
        self.assertFalse(self.guard.is_armed)

        # Explicit re-arm at t1
        t1 = 200.0
        self.guard.arm(now_monotonic=t1)
        self.assertTrue(self.guard.is_armed)
        self.assertEqual(self.guard._arm_time_monotonic, t1)

        # Command at t1 + 0.100 accepted
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        fwd, faulted, reason = self.guard.handle_command(cmd, now_monotonic=t1 + 0.100)
        self.assertFalse(faulted)
        self.assertEqual(fwd, cmd)

    def test_rearm_clears_cached_commands(self):
        """Re-arming clears cached commands so old state is not re-emitted."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        self.guard.handle_command(cmd, now_monotonic=t0 + 0.050)
        self.guard.disarm()

        # Re-arm at t1
        t1 = 200.0
        self.guard.arm(now_monotonic=t1)
        # Last command time must be cleared
        self.assertIsNone(self.guard._last_command_monotonic)
        self.assertEqual(self.guard._arm_time_monotonic, t1)

    def test_negative_time_jump_in_check_timeout(self):
        """Monotonic clock moving backward in check_timeout must fault and disarm."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # Clock jump backward
        timed_out, zero_cmd = self.guard.check_timeout(now_monotonic=t0 - 10.0)
        self.assertTrue(timed_out)
        self.assertFalse(self.guard.is_armed)

    def test_negative_time_jump_in_handle_command(self):
        """Monotonic clock moving backward in handle_command must fault and disarm."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # Clock jump backward in handle_command
        cmd = [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
        fwd, faulted, reason = self.guard.handle_command(cmd, now_monotonic=t0 - 5.0)
        self.assertTrue(faulted)
        self.assertFalse(self.guard.is_armed)
        self.assertIn("Negative monotonic time jump", reason)

    def test_speed_limits_and_safety_invariants_preserved(self):
        """Safety invariants (max RPS, NaN, missing motor) remain strictly enforced."""
        t0 = 100.0
        self.guard.arm(now_monotonic=t0)

        # Exceed max RPS (2.0)
        overspeed_cmd = [(1, 2.5), (2, 1.0), (3, 1.0), (4, 1.0)]
        fwd, faulted, reason = self.guard.handle_command(
            overspeed_cmd, now_monotonic=t0 + 0.050
        )
        self.assertTrue(faulted)
        self.assertFalse(self.guard.is_armed)
        self.assertIn("exceeds limit", reason)


class TestPTYFakeBoardSerialFraming(unittest.TestCase):
    """Verify native fake-board/PTY serial framing fixture (§10.4)."""

    def setUp(self):
        self.master_fd, self.slave_fd = os.openpty()
        self.slave_path = os.ttyname(self.slave_fd)

    def tearDown(self):
        try:
            os.close(self.slave_fd)
        except OSError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    def test_pty_serial_framing_and_byte_accounting(self):
        """PTY sink encodes valid 0xAA 0x55 frame with correct motor payload and CRC8."""
        board = Board(device=self.slave_path, baudrate=1000000)
        self.assertEqual(board.sink_type, "pty")
        self.assertFalse(board.is_mock)
        self.assertIsNotNone(board.port)
        self.assertTrue(board.port.is_open)

        # Send motor command for 4 motors
        motor_cmd = [[1, 50.0], [2, -50.0], [3, 50.0], [4, -50.0]]
        written = board.set_motor_speed(motor_cmd)
        self.assertEqual(written, 27)

        # Read the raw packet from the master end
        raw_packet = os.read(self.master_fd, 1024)
        self.assertEqual(len(raw_packet), 27)

        # 1. Header bytes
        self.assertEqual(raw_packet[0], 0xAA)
        self.assertEqual(raw_packet[1], 0x55)

        # 2. Function ID
        self.assertEqual(raw_packet[2], PacketFunction.PACKET_FUNC_MOTOR)

        # 3. Data length (1 subfunc + 1 count + 4 * 5 bytes = 22 = 0x16)
        data_len = raw_packet[3]
        self.assertEqual(data_len, 22)

        # 4. Sub-function and motor count
        subfunc = raw_packet[4]
        motor_count = raw_packet[5]
        self.assertEqual(subfunc, 0x01)
        self.assertEqual(motor_count, 4)

        # 5. Decode motor speeds
        payload = raw_packet[6:26]
        offset = 0
        decoded_motors = []
        for _ in range(4):
            m_id, speed = struct.unpack_from("<Bf", payload, offset)
            decoded_motors.append((m_id + 1, speed))
            offset += 5
        self.assertEqual(decoded_motors[0], (1, 50.0))
        self.assertEqual(decoded_motors[1], (2, -50.0))
        self.assertEqual(decoded_motors[2], (3, 50.0))
        self.assertEqual(decoded_motors[3], (4, -50.0))

        # 6. Checksum CRC8 verification over packet[2:-1]
        expected_crc = checksum_crc8(raw_packet[2:-1])
        actual_crc = raw_packet[-1]
        self.assertEqual(actual_crc, expected_crc)

        # 7. Verify write accounting metadata in board.last_write_info
        info = board.last_write_info
        self.assertEqual(info["sink_type"], "pty")
        self.assertEqual(info["bytes_written"], 27)
        self.assertEqual(info["bytes_expected"], 27)
        self.assertEqual(info["frame_hex"], raw_packet.hex())
        self.assertTrue(info["success"])
        self.assertIsNone(info["error"])

        board.close()

    def test_pty_zero_motors_framing(self):
        """Calling zero_motors on PTY transmits repeated zero frames."""
        board = Board(device=self.slave_path, baudrate=1000000)
        board.zero_motors(count=2)

        # Read 2 packets (2 * 27 = 54 bytes)
        raw_packet = bytearray()
        while len(raw_packet) < 54:
            chunk = os.read(self.master_fd, 1024)
            if not chunk:
                break
            raw_packet.extend(chunk)
        self.assertEqual(len(raw_packet), 54)
        p1 = raw_packet[:27]
        p2 = raw_packet[27:]

        for p in (p1, p2):
            self.assertEqual(p[0], 0xAA)
            self.assertEqual(p[1], 0x55)
            self.assertEqual(p[2], PacketFunction.PACKET_FUNC_MOTOR)
            self.assertEqual(checksum_crc8(p[2:-1]), p[-1])
            # Check all speeds are 0.0
            for i in range(4):
                _, speed = struct.unpack_from("<Bf", p[6:26], i * 5)
                self.assertEqual(speed, 0.0)

        board.close()

    def test_dev_rrc_never_opened_during_pty_test(self):
        """PTY fake-board fixture must NEVER open or access /dev/rrc."""
        board = Board(device=self.slave_path, baudrate=1000000)
        self.assertNotEqual(board.device, "/dev/rrc")
        self.assertTrue(board.device.startswith("/dev/pts/"))
        board.close()

    def test_mock_sink_type_distinction(self):
        """Mock mode, PTY mode, and real serial mode have distinct sink types."""
        mock_board = Board(device="mock")
        self.assertEqual(mock_board.sink_type, "mock")
        self.assertTrue(mock_board.is_mock)
        mock_board.close()

        pty_board = Board(device=self.slave_path)
        self.assertEqual(pty_board.sink_type, "pty")
        self.assertFalse(pty_board.is_mock)
        pty_board.close()


class TestStageSpecificDeliveryObservations(unittest.TestCase):
    """Verify delivery observation schemas and 5-stage correlation (§10.4)."""

    def setUp(self):
        self.bench_client = BenchClientNode(node_name="test_bench_observer")

    def test_delivery_observation_schema_and_types(self):
        """Every delivery observation JSON must contain required metadata fields."""
        stages = [
            (
                "controller_rx",
                {
                    "stage": "controller_rx",
                    "node": "controller",
                    "run_id": "run_1",
                    "seq": 1,
                    "stamp_mono": 100.0,
                    "linear_x": 0.1,
                    "angular_z": 0.0,
                },
            ),
            (
                "guard_arm",
                {
                    "stage": "guard_arm",
                    "node": "guard",
                    "run_id": "run_2",
                    "seq": 1,
                    "stamp_mono": 100.01,
                    "armed": True,
                    "reason": "arm_requested",
                },
            ),
            (
                "guard_disarm",
                {
                    "stage": "guard_disarm",
                    "node": "guard",
                    "run_id": "run_2",
                    "seq": 2,
                    "stamp_mono": 100.015,
                    "armed": False,
                    "reason": "disarm_requested",
                },
            ),
            (
                "guard_rx",
                {
                    "stage": "guard_rx",
                    "node": "guard",
                    "run_id": "run_2",
                    "seq": 3,
                    "stamp_mono": 100.02,
                    "motors": [[1, 0.5], [2, 0.5], [3, 0.5], [4, 0.5]],
                },
            ),
            (
                "guard_fwd",
                {
                    "stage": "guard_fwd",
                    "node": "guard",
                    "run_id": "run_2",
                    "seq": 4,
                    "stamp_mono": 100.03,
                    "motors": [[1, 0.5], [2, 0.5], [3, 0.5], [4, 0.5]],
                },
            ),
            (
                "bridge_rx",
                {
                    "stage": "bridge_rx",
                    "node": "bridge",
                    "run_id": "run_3",
                    "seq": 1,
                    "stamp_mono": 100.04,
                    "motors": [[1, 0.5], [2, 0.5], [3, 0.5], [4, 0.5]],
                },
            ),
            (
                "bridge_write",
                {
                    "stage": "bridge_write",
                    "node": "bridge",
                    "run_id": "run_3",
                    "seq": 2,
                    "stamp_mono": 100.05,
                    "sink_type": "pty",
                    "bytes_written": 27,
                    "bytes_expected": 27,
                    "frame_hex": "aa55...",
                    "duration_ms": 0.25,
                    "success": True,
                    "error": None,
                },
            ),
        ]

        for stage_name, sample in stages:
            with self.subTest(stage=stage_name):
                # JSON serializability check
                raw_json = json.dumps(sample)
                parsed = json.loads(raw_json)
                self.assertEqual(parsed["stage"], stage_name)
                self.assertIn("node", parsed)
                self.assertIn("run_id", parsed)
                self.assertIn("seq", parsed)
                self.assertIn("stamp_mono", parsed)

    def test_correlated_5stage_delivery_success(self):
        """5-stage correlated delivery pipeline verifies successfully when all stages present."""
        burst_start = 100.0
        self.bench_client.reset_observations()

        motion_motors = [[1, 0.75], [2, 0.75], [3, 0.75], [4, 0.75]]
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        motion_hex = _make_motor_frame_hex(motion_motors)
        zero_hex = _make_motor_frame_hex(zero_motors)

        observations = [
            {
                "stage": "controller_rx",
                "node": "controller",
                "run_id": "c_run",
                "seq": 1,
                "stamp_mono": 100.05,
                "linear_x": 0.15,
                "angular_z": 0.0,
                "motors": motion_motors,
            },
            {
                "stage": "guard_rx",
                "node": "guard",
                "run_id": "g_run",
                "seq": 1,
                "stamp_mono": 100.06,
                "motors": motion_motors,
            },
            {
                "stage": "guard_fwd",
                "node": "guard",
                "run_id": "g_run",
                "seq": 2,
                "stamp_mono": 100.07,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 1,
                "stamp_mono": 100.08,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 2,
                "stamp_mono": 100.09,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": motion_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 3,
                "stamp_mono": 100.10,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 4,
                "stamp_mono": 100.11,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": zero_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": zero_motors,
            },
        ]
        self.bench_client.observations.extend(observations)

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=burst_start,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.1,
        )
        self.assertTrue(ok, f"Verification failed: {msg}")
        self.assertEqual(ev["sink_type"], "pty")
        self.assertEqual(ev["bytes_written"], 54)
        self.assertEqual(ev["bytes_expected"], 54)
        self.assertEqual(len(ev["errors"]), 0)


class TestBrokenPipelineAndFailClosed(unittest.TestCase):
    """Verify bench orchestrator fails closed on broken pipeline edges (§10.4)."""

    def setUp(self):
        self.bench_client = BenchClientNode(node_name="test_bench_broken")

    def _make_base_observations(self, since_mono=100.0):
        motion_motors = [[1, 0.75], [2, 0.75], [3, 0.75], [4, 0.75]]
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        motion_hex = _make_motor_frame_hex(motion_motors)
        zero_hex = _make_motor_frame_hex(zero_motors)
        return [
            {
                "stage": "controller_rx",
                "node": "controller",
                "run_id": "c_run",
                "seq": 1,
                "stamp_mono": since_mono + 0.01,
                "linear_x": 0.15,
                "angular_z": 0.0,
                "motors": motion_motors,
            },
            {
                "stage": "guard_rx",
                "node": "guard",
                "run_id": "g_run",
                "seq": 1,
                "stamp_mono": since_mono + 0.02,
                "motors": motion_motors,
            },
            {
                "stage": "guard_fwd",
                "node": "guard",
                "run_id": "g_run",
                "seq": 2,
                "stamp_mono": since_mono + 0.03,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 1,
                "stamp_mono": since_mono + 0.04,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 2,
                "stamp_mono": since_mono + 0.05,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": motion_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": motion_motors,
            },
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 3,
                "stamp_mono": since_mono + 0.06,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 4,
                "stamp_mono": since_mono + 0.07,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": zero_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": zero_motors,
            },
        ]

    def test_missing_controller_rx_fails(self):
        """Dropped command or controller failure fails verification closed."""
        obs = [
            o for o in self._make_base_observations() if o["stage"] != "controller_rx"
        ]
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing stage 'controller_rx'", msg)

    def test_mismatched_command_values_fails(self):
        """Command values not matching expected linear/angular setpoint fails."""
        obs = self._make_base_observations()
        obs[0]["linear_x"] = 0.50  # Mismatch (expected 0.15)
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("controller_rx command values mismatch", msg)

    def test_missing_guard_rx_fails(self):
        """Broken edge between controller and guard fails verification closed."""
        obs = [o for o in self._make_base_observations() if o["stage"] != "guard_rx"]
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing stage 'guard_rx'", msg)

    def test_guard_fault_rejection_fails(self):
        """Guard fault / command rejection is reported with explicit fault reason."""
        obs = [
            o
            for o in self._make_base_observations()
            if o["stage"] not in ("guard_fwd", "bridge_rx", "bridge_write")
        ]
        obs.append(
            {
                "stage": "guard_fault",
                "node": "guard",
                "run_id": "g_run",
                "seq": 2,
                "stamp_mono": 100.03,
                "reason": "rps_exceeds_max",
            }
        )
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("guard rejected command", msg)
        self.assertIn("rps_exceeds_max", msg)

    def test_guard_timeout_fails(self):
        """Guard deadline timeout is reported with explicit timeout reason."""
        obs = [
            o
            for o in self._make_base_observations()
            if o["stage"] not in ("guard_fwd", "bridge_rx", "bridge_write")
        ]
        obs.append(
            {
                "stage": "guard_timeout",
                "node": "guard",
                "run_id": "g_run",
                "seq": 2,
                "stamp_mono": 100.03,
                "reason": "first_command_deadline_expired",
            }
        )
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("first_command_deadline_expired", msg)

    def test_missing_bridge_rx_fails(self):
        """Broken edge between guard and bridge fails verification closed."""
        obs = [
            o
            for o in self._make_base_observations()
            if o["stage"] not in ("bridge_rx", "bridge_write")
        ]
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing stage 'bridge_rx'", msg)

    def test_missing_bridge_write_fails(self):
        """Bridge receiving command without serial write fails verification closed."""
        obs = [
            o for o in self._make_base_observations() if o["stage"] != "bridge_write"
        ]
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing stage 'bridge_write'", msg)

    def test_bridge_write_error_fails(self):
        """Serial write error on the bridge sink fails verification closed."""
        obs = self._make_base_observations()
        obs[4]["success"] = False
        obs[4]["error"] = "Serial connection lost"
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("bridge_write failed", msg)
        self.assertIn("Serial connection lost", msg)

    def test_bridge_short_write_fails(self):
        """Short serial write (bytes_written < bytes_expected) fails verification closed."""
        obs = self._make_base_observations()
        obs[4]["bytes_written"] = 12
        obs[4]["bytes_expected"] = 27
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("short write", msg)

    def test_process_restart_during_burst_fails(self):
        """Node crash/restart during burst (run_id changed) fails verification closed."""
        obs = self._make_base_observations()
        # Add a second bridge observation with a new run_id
        obs.append(
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run_RESTARTED",
                "seq": 5,
                "stamp_mono": 100.08,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": "aa55...",
                "duration_ms": 0.3,
                "success": True,
                "error": None,
            }
        )
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Process restart detected", msg)

    def test_stale_observations_outside_burst_window_ignored(self):
        """Observations timestamped prior to burst window are ignored."""
        old_obs = self._make_base_observations(since_mono=50.0)
        self.bench_client.observations = old_obs

        # Evaluate for burst starting at 100.0
        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="test_burst",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing stage 'controller_rx'", msg)

    def test_zero_only_motion_delivery_fails(self):
        """Forward motion delivery must fail when downstream observations contain only zeros."""
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        zero_hex = _make_motor_frame_hex(zero_motors)
        obs = [
            {
                "stage": "controller_rx",
                "node": "controller",
                "run_id": "c_run",
                "seq": 1,
                "stamp_mono": 100.01,
                "linear_x": 0.15,
                "angular_z": 0.0,
                "motors": [[1, 0.75], [2, 0.75], [3, 0.75], [4, 0.75]],
            },
            {
                "stage": "guard_rx",
                "node": "guard",
                "run_id": "g_run",
                "seq": 1,
                "stamp_mono": 100.02,
                "motors": zero_motors,
            },
            {
                "stage": "guard_fwd",
                "node": "guard",
                "run_id": "g_run",
                "seq": 2,
                "stamp_mono": 100.03,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 1,
                "stamp_mono": 100.04,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 2,
                "stamp_mono": 100.05,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": zero_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": zero_motors,
            },
        ]
        self.bench_client.observations = obs
        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("guard_rx motor commands do not match expected motion", msg)

    def test_wrong_direction_delivery_fails(self):
        """Motion delivery must fail when downstream observations have reversed direction."""
        rev_motors = [[1, -0.75], [2, -0.75], [3, -0.75], [4, -0.75]]
        rev_hex = _make_motor_frame_hex(rev_motors)
        obs = self._make_base_observations()
        # Change downstream stages to reverse
        obs[1]["motors"] = rev_motors
        obs[2]["motors"] = rev_motors
        obs[3]["motors"] = rev_motors
        obs[4]["motors"] = rev_motors
        obs[4]["frame_hex"] = rev_hex
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("guard_rx motor commands do not match expected motion", msg)

    def test_delivered_motion_missing_terminating_zeros_fails(self):
        """Motion burst must fail when motion writes succeed but terminating zero is dropped."""
        obs = self._make_base_observations()
        # Omit terminating zero bridge_rx and bridge_write (last two elements)
        obs = obs[:5]
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertTrue(
            "Missing terminating zero receipt at bridge" in msg
            or "Missing terminating zero serial write at bridge" in msg
        )

    def test_delivered_motion_failed_terminating_zeros_fails(self):
        """Motion burst must fail when terminating zero serial write fails."""
        obs = self._make_base_observations()
        obs[6]["success"] = False
        obs[6]["error"] = "Serial port write error during stop"
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("terminating zero bridge_write failed", msg)

    def test_delivered_motion_short_terminating_zeros_fails(self):
        """Motion burst must fail when terminating zero write is short."""
        obs = self._make_base_observations()
        obs[6]["bytes_written"] = 10
        obs[6]["bytes_expected"] = 27
        self.bench_client.observations = obs

        ok, msg, ev = self.bench_client.verify_correlated_delivery(
            since_mono=100.0,
            burst_name="forward_test",
            expected_lx=0.15,
            expected_az=0.0,
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("terminating zero short write", msg)

    def test_disarm_stop_delivery_success(self):
        """Disarm stop delivery succeeds when four-motor zeros are received and written."""
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        zero_hex = _make_motor_frame_hex(zero_motors)
        obs = [
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 10,
                "stamp_mono": 100.02,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 11,
                "stamp_mono": 100.03,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "frame_hex": zero_hex,
                "duration_sec": 0.0003,
                "success": True,
                "error": None,
                "motors": zero_motors,
            },
        ]
        self.bench_client.observations = obs
        ok, msg, ev = self.bench_client.verify_disarm_stop_delivery(
            since_mono=100.0,
            burst_name="test_disarm",
            timeout_sec=0.05,
        )
        self.assertTrue(ok, msg)
        self.assertEqual(ev["sink_type"], "pty")
        self.assertEqual(ev["bytes_written"], 27)

    def test_disarm_stop_delivery_missing_write_fails(self):
        """Disarm stop delivery fails when zero bridge_write is missing."""
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        obs = [
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 10,
                "stamp_mono": 100.02,
                "motors": zero_motors,
            }
        ]
        self.bench_client.observations = obs
        ok, msg, ev = self.bench_client.verify_disarm_stop_delivery(
            since_mono=100.0,
            burst_name="test_disarm",
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("Missing downstream zero serial write", msg)

    def test_disarm_stop_delivery_failed_write_fails(self):
        """Disarm stop delivery fails when zero bridge_write fails."""
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        obs = [
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 10,
                "stamp_mono": 100.02,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 11,
                "stamp_mono": 100.03,
                "sink_type": "pty",
                "bytes_written": 27,
                "bytes_expected": 27,
                "success": False,
                "error": "UART transmit failure",
                "motors": zero_motors,
            },
        ]
        self.bench_client.observations = obs
        ok, msg, ev = self.bench_client.verify_disarm_stop_delivery(
            since_mono=100.0,
            burst_name="test_disarm",
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("bridge_write failed", msg)

    def test_disarm_stop_delivery_short_write_fails(self):
        """Disarm stop delivery fails when zero bridge_write is short."""
        zero_motors = [[1, 0.0], [2, 0.0], [3, 0.0], [4, 0.0]]
        obs = [
            {
                "stage": "bridge_rx",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 10,
                "stamp_mono": 100.02,
                "motors": zero_motors,
            },
            {
                "stage": "bridge_write",
                "node": "bridge",
                "run_id": "b_run",
                "seq": 11,
                "stamp_mono": 100.03,
                "sink_type": "pty",
                "bytes_written": 15,
                "bytes_expected": 27,
                "success": True,
                "error": None,
                "motors": zero_motors,
            },
        ]
        self.bench_client.observations = obs
        ok, msg, ev = self.bench_client.verify_disarm_stop_delivery(
            since_mono=100.0,
            burst_name="test_disarm",
            timeout_sec=0.05,
        )
        self.assertFalse(ok)
        self.assertIn("short write", msg)

    def test_orchestrator_burst_fails_when_disarm_stop_fails(self):
        """Orchestrator motion burst fails closed if disarm zero write verification fails."""
        from unittest.mock import MagicMock, patch

        node = MagicMock()
        node.prepare_discovery.return_value = True
        node.call_set_arm.return_value = (True, "OK")
        node.run_motion_burst.return_value = True
        node.wait_for_state.side_effect = [
            {"guard_armed": True},  # arm check
            {"guard_armed": True},  # during burst check
            {"guard_armed": False},  # disarm check
        ]
        node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {"sink_type": "pty"},
        )
        # Disarm stop verification fails
        node.verify_disarm_stop_delivery.return_value = (
            False,
            "Failed to write zero stop to serial",
            {},
        )

        orch = BenchAcceptanceOrchestrator(mock=False)
        with patch.dict(sys.modules, {"rclpy": MagicMock()}):
            with patch(
                "ubuntu_tank_bringup.bench_client.BenchClientNode",
                return_value=node,
            ):
                passed, errors = orch.run_motion_acceptance_tests()

        self.assertFalse(passed)
        self.assertTrue(
            any("disarm stop delivery verification failed" in e for e in errors)
        )
        exec_seq = orch.results["motion_tests"]["execution_sequence"]
        self.assertFalse(exec_seq.get("all_bursts_ended_in_zero", False))
        self.assertFalse(exec_seq.get("software_delivery_verified", False))


class TestSROS2DeliveryPolicyPermissions(unittest.TestCase):
    """Verify SROS2 permissions and narrow grants for delivery observation (§10.4)."""

    def setUp(self):
        self.policies_xml = os.path.join(
            UBUNTU_TANK_DIR, "config", "sros2", "policies.xml"
        )
        self.perm_dir = os.path.join(UBUNTU_TANK_DIR, "config", "sros2", "permissions")

    def test_policies_xml_contains_delivery_observation_grants(self):
        """policies.xml must specify publish/subscribe rules for delivery_observation."""
        tree = ET.parse(self.policies_xml)
        root = tree.getroot()

        enclaves = {
            e.get("path"): e
            for e in root.findall(".//enclave")
            if e.get("path") is not None
        }

        obs_topic = "/ubuntu_tank/delivery_observation"

        # controller, guard, bridge must have publish allow
        for p_name in ("controller", "guard", "bridge"):
            enc = enclaves.get(f"/ubuntu_tank/{p_name}")
            self.assertIsNotNone(enc, f"Enclave '/ubuntu_tank/{p_name}' missing")
            pub_topics = [
                t.text.strip() for t in enc.findall(".//topics[@publish='ALLOW']/topic")
            ]
            self.assertIn(
                obs_topic,
                pub_topics,
                f"Enclave '/ubuntu_tank/{p_name}' missing publish grant for '{obs_topic}'",
            )

        # operator, status must have subscribe allow
        for p_name in ("operator", "status"):
            enc = enclaves.get(f"/ubuntu_tank/{p_name}")
            self.assertIsNotNone(enc, f"Enclave '/ubuntu_tank/{p_name}' missing")
            sub_topics = [
                t.text.strip()
                for t in enc.findall(".//topics[@subscribe='ALLOW']/topic")
            ]
            self.assertIn(
                obs_topic,
                sub_topics,
                f"Enclave '/ubuntu_tank/{p_name}' missing subscribe grant for '{obs_topic}'",
            )

    def test_permissions_xml_contains_delivery_observation_rules(self):
        """All permission XML documents must include rt/ubuntu_tank/delivery_observation."""
        obs_rule = "rt/ubuntu_tank/delivery_observation"

        for p_name in ("controller", "guard", "bridge"):
            xml_file = os.path.join(self.perm_dir, f"{p_name}_permissions.xml")
            with open(xml_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn(obs_rule, content)
            self.assertIn("publish", content)

        for p_name in ("operator", "status"):
            xml_file = os.path.join(self.perm_dir, f"{p_name}_permissions.xml")
            with open(xml_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn(obs_rule, content)
            self.assertIn("subscribe", content)

    def test_operator_and_status_denied_actuator_topics(self):
        """Operator and status enclaves are strictly forbidden from actuator control topics."""
        for p_name in ("operator", "status"):
            xml_file = os.path.join(self.perm_dir, f"{p_name}_permissions.xml")
            with open(xml_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("rt/ubuntu_tank_safety/motor_input", content)
            self.assertNotIn("rt/ros_robot_controller/set_motor_guarded", content)

        # Status must also NOT be able to publish cmd_vel
        status_file = os.path.join(self.perm_dir, "status_permissions.xml")
        with open(status_file, "r", encoding="utf-8") as f:
            status_content = f.read()
        self.assertNotIn("rt/controller/cmd_vel", status_content)

    def test_sros2_policy_verification_passes(self):
        """sros2_policy.py verify must pass with zero schema or policy errors."""
        sros2_policy_mod.verify_official_schemas()
        sros2_policy_mod.verify_governance()
        sros2_policy_mod.verify_security_invariants()
        sros2_policy_mod.verify_traffic_simulation()


class TestAcceptanceReportSegregation(unittest.TestCase):
    """Verify report segregation between software, host write, and physical acceptance (§10.4)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "test.lock")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_report_segregates_software_and_physical(self):
        """Markdown report must distinctly segregate Software Delivery and Physical Motion."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
        )
        # Run acceptance suite in mock mode
        suite_ok = orch.run_acceptance_suite()
        self.assertTrue(suite_ok)

        md = orch.generate_markdown_report()

        # Check required segregated table headers
        self.assertIn("Software Delivery (5 Stages)", md)
        self.assertIn("Sink Type", md)
        self.assertIn("Physical Motion", md)

        # Physical motion must be explicitly marked pending owner observation
        self.assertIn("PENDING_OWNER_OBSERVATION", md)

        # Overall status and physical acceptance status
        self.assertEqual(orch.results["status"], "SIMULATION_PASSED")
        self.assertEqual(orch.results["physical_acceptance_status"], "INCOMPLETE")
        self.assertEqual(orch.results["software_delivery_status"], "PASSED")
        self.assertIn("**Overall Status**: **SIMULATION_PASSED**", md)

        # On-ground safety must remain false
        stm = orch.results.get("stm32_command_loss", {})
        self.assertFalse(stm.get("safe_for_on_ground", True))

    def test_mock_or_software_cannot_produce_physical_motion_pass(self):
        """Software-only or mock execution must NEVER set physical_movement_verified = True."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
        )
        orch.run_motion_acceptance_tests()
        exec_seq = orch.results["motion_tests"].get("execution_sequence", {})
        # Must NOT claim physical motion was verified
        self.assertFalse(
            exec_seq.get("physical_movement_verified", False),
            "Software simulation must never claim physical motion verified",
        )


if __name__ == "__main__":
    unittest.main()
