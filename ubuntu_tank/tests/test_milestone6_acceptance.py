"""
test_milestone6_acceptance.py - Unit and integration tests for Milestone 6:
Raised-track controller acceptance, kinematic polarities, conservative limits,
stop latencies across 6 failure conditions, and STM32 command-loss characterization.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")
SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")

for p in [REPO_ROOT, UBUNTU_TANK_DIR, SCRIPTS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

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

from scripts.bench_acceptance import (
    ACCEPTED_GEOMETRY,
    ACCEPTED_LATENCY_BOUNDS_MS,
    ACCEPTED_LIMITS,
    BenchAcceptanceOrchestrator,
    check_motor_polarity,
    compute_kinematic_motor_speeds,
)
from scripts.deployment_manager import DeploymentLock
from ubuntu_tank_bringup.bench_client import BenchClientNode
from ubuntu_tank_bringup.status_client import StatusClientNode
from ubuntu_tank_safety.motor_guard import MotorGuard
from ubuntu_tank_teleop.lease import TeleopLeaseManager


class TestMilestone6Acceptance(unittest.TestCase):
    """Test suite validating all requirements of Milestone 6."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "test_deploy.lock")
        self.config_path = os.path.join(UBUNTU_TANK_DIR, "config", "controller.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. Physical Safety Acknowledgment
    def test_physical_safety_acknowledgment_enforced(self):
        """Orchestrator must fail closed if --ack-tracks-raised is not provided."""
        res = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS_DIR, "bench_acceptance.py"),
                "--mock",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            res.returncode, 0, "Must exit with failure code when ack is missing"
        )
        self.assertIn(
            "Physical safety acknowledgment required: --ack-tracks-raised", res.stderr
        )
        self.assertIn(
            "Under NO circumstances does Milestone 6 authorize on-ground motion",
            res.stderr,
        )

    # 2. Deployment Lock Exclusivity
    def test_deployment_lock_held_fails_preflight(self):
        """If another process holds the deployment lock, preflight checks must fail."""
        with DeploymentLock(self.lock_path, timeout_sec=0.2):
            orch = BenchAcceptanceOrchestrator(
                lock_path=self.lock_path, mock=True, mock_containers="EMPTY"
            )
            ok, errors = orch.run_preflight_checks()
            self.assertFalse(ok)
            self.assertTrue(any("held by another process" in e for e in errors))
            self.assertEqual(orch.results["preflight"]["deployment_lock"], "HELD")

    # 3. Container & Service Mutual Exclusion
    def test_conflicting_containers_fail_preflight(self):
        """Conflicting factory/replacement containers must fail preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=True, mock_containers="MentorPi"
        )
        ok, errors = orch.run_preflight_checks()
        self.assertFalse(ok)
        self.assertTrue(any("Conflicting container" in e for e in errors))
        self.assertEqual(orch.results["preflight"]["mutual_exclusion"], "FAILED")

    # 4. USB Identity Check
    def test_usb_identity_verification(self):
        """USB identity 1a86:55d4 must pass in mock mode and report identity."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=True, mock_containers="EMPTY"
        )
        ok, errors = orch.run_preflight_checks()
        self.assertTrue(ok, f"Preflight failed: {errors}")
        self.assertEqual(orch.results["preflight"]["usb_identity"], "PASSED")
        self.assertIn("1a86:55d4", orch.results["preflight"]["usb_details"])

    # 5. Battery Voltage Threshold
    def test_battery_voltage_threshold(self):
        """Battery voltage below 9600 mV must fail preflight to prevent brownout."""
        # Low voltage fault
        orch_low = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=True,
            mock_battery_mv=9200,
            mock_containers="EMPTY",
        )
        ok, errors = orch_low.run_preflight_checks()
        self.assertFalse(ok)
        self.assertTrue(any("Battery voltage too low" in e for e in errors))
        self.assertEqual(
            orch_low.results["preflight"]["battery_status"], "LOW_VOLTAGE_FAULT"
        )

        # Healthy voltage
        orch_ok = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=True,
            mock_battery_mv=12200,
            mock_containers="EMPTY",
        )
        ok, errors = orch_ok.run_preflight_checks()
        self.assertTrue(ok, f"Healthy battery check failed: {errors}")
        self.assertEqual(orch_ok.results["preflight"]["battery_status"], "OK")

    # 6. Geometry and Conservative Limits
    def test_accepted_geometry_and_limits_validation(self):
        """Verify accepted geometry and conservative limits against controller.yaml."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path, lock_path=self.lock_path, mock=True
        )
        ok, errors = orch.verify_geometry_and_limits()
        self.assertTrue(ok, f"Geometry/limits failed: {errors}")
        geom = orch.results["geometry_and_limits"]
        self.assertAlmostEqual(geom["wheelbase"], 0.1368, places=3)
        self.assertAlmostEqual(geom["track_width"], 0.1446, places=3)
        self.assertAlmostEqual(geom["wheel_diameter"], 0.075, places=3)
        self.assertLessEqual(geom["max_linear_speed"], 0.5)
        self.assertLessEqual(geom["max_angular_speed"], 2.0)
        self.assertLessEqual(geom["max_rps"], 2.0)
        self.assertEqual(geom["correction_factor"], {"left": 1.0, "right": 1.0})

    def test_exceeded_limits_fail_validation(self):
        """Config values exceeding conservative bench limits must fail validation."""
        bad_config_path = os.path.join(self.tmp_dir, "bad_controller.yaml")
        bad_cfg = {
            "controller": {
                "wheelbase": 0.2000,
                "track_width": 0.2000,
                "wheel_diameter": 0.1000,
                "max_linear_speed": 1.2,
                "max_angular_speed": 4.0,
                "correction_factor": {"left": 1.5, "right": 1.0},
            },
            "motor_guard": {"max_rps": 5.0},
        }
        with open(bad_config_path, "w", encoding="utf-8") as f:
            import yaml

            yaml.safe_dump(bad_cfg, f)

        orch = BenchAcceptanceOrchestrator(config_path=bad_config_path, mock=True)
        ok, errors = orch.verify_geometry_and_limits()
        self.assertFalse(ok)
        self.assertTrue(any("max_linear_speed" in e for e in errors))
        self.assertTrue(any("max_angular_speed" in e for e in errors))
        self.assertTrue(any("max_rps" in e for e in errors))
        self.assertTrue(any("wheelbase" in e for e in errors))

    # 7-11. Kinematic Motor Polarity Checks
    def test_forward_kinematic_polarity(self):
        """Forward motion: Left motors (1, 2) < 0 RPS, Right motors (3, 4) > 0 RPS."""
        speeds = compute_kinematic_motor_speeds(
            linear_x=0.2,
            angular_z=0.0,
            wheelbase=0.1368,
            track_width=0.1446,
            wheel_diameter=0.075,
        )
        ok, msg = check_motor_polarity("forward", speeds)
        self.assertTrue(ok, msg)
        rps = dict(speeds)
        self.assertLess(rps[1], 0)
        self.assertLess(rps[2], 0)
        self.assertGreater(rps[3], 0)
        self.assertGreater(rps[4], 0)

    def test_reverse_kinematic_polarity(self):
        """Reverse motion: Left motors (1, 2) > 0 RPS, Right motors (3, 4) < 0 RPS."""
        speeds = compute_kinematic_motor_speeds(
            linear_x=-0.2,
            angular_z=0.0,
            wheelbase=0.1368,
            track_width=0.1446,
            wheel_diameter=0.075,
        )
        ok, msg = check_motor_polarity("reverse", speeds)
        self.assertTrue(ok, msg)
        rps = dict(speeds)
        self.assertGreater(rps[1], 0)
        self.assertGreater(rps[2], 0)
        self.assertLess(rps[3], 0)
        self.assertLess(rps[4], 0)

    def test_spin_left_kinematic_polarity(self):
        """Spin Left CCW: All 4 motors (1, 2, 3, 4) > 0 RPS."""
        speeds = compute_kinematic_motor_speeds(
            linear_x=0.0,
            angular_z=0.8,
            wheelbase=0.1368,
            track_width=0.1446,
            wheel_diameter=0.075,
        )
        ok, msg = check_motor_polarity("spin_left", speeds)
        self.assertTrue(ok, msg)
        rps = dict(speeds)
        for i in (1, 2, 3, 4):
            self.assertGreater(rps[i], 0)

    def test_spin_right_kinematic_polarity(self):
        """Spin Right CW: All 4 motors (1, 2, 3, 4) < 0 RPS."""
        speeds = compute_kinematic_motor_speeds(
            linear_x=0.0,
            angular_z=-0.8,
            wheelbase=0.1368,
            track_width=0.1446,
            wheel_diameter=0.075,
        )
        ok, msg = check_motor_polarity("spin_right", speeds)
        self.assertTrue(ok, msg)
        rps = dict(speeds)
        for i in (1, 2, 3, 4):
            self.assertLess(rps[i], 0)

    def test_stop_kinematic_polarity(self):
        """Stop: All 4 motors exactly 0.0 RPS."""
        speeds = compute_kinematic_motor_speeds(
            linear_x=0.0,
            angular_z=0.0,
            wheelbase=0.1368,
            track_width=0.1446,
            wheel_diameter=0.075,
        )
        ok, msg = check_motor_polarity("stop", speeds)
        self.assertTrue(ok, msg)
        rps = dict(speeds)
        for i in (1, 2, 3, 4):
            self.assertEqual(rps[i], 0.0)

    # 12. Bounded Motion Tests Execution
    def test_motion_acceptance_tests_execution(self):
        """Motion tests sequence must execute cleanly and record valid results."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path,
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
        )
        ok, errors = orch.run_motion_acceptance_tests()
        self.assertTrue(ok, f"Motion tests failed: {errors}")
        m_tests = orch.results["motion_tests"]
        for motion in ["forward", "reverse", "spin_left", "spin_right", "stop"]:
            self.assertIn(motion, m_tests)
            self.assertTrue(m_tests[motion]["polarity_valid"])
        seq = m_tests["execution_sequence"]
        self.assertTrue(seq["all_bursts_ended_in_zero"])
        self.assertTrue(seq["disarmed_after_run"])

    # 13-18. Stop Latencies for All 6 Conditions
    def test_all_six_stop_latencies(self):
        """Measure all 6 stop conditions and verify they satisfy accepted bounds."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path,
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
        )
        ok, errors = orch.measure_stop_latencies()
        self.assertTrue(ok, f"Latency tests failed: {errors}")
        lats = orch.results["latency_measurements"]

        # Condition 1: Keyboard lease expiry <= 200 ms
        self.assertIn("keyboard_lease_expiry", lats)
        self.assertTrue(lats["keyboard_lease_expiry"]["passed"])
        self.assertLessEqual(
            lats["keyboard_lease_expiry"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["keyboard_lease_expiry"],
        )

        # Condition 2: Guard freshness timeout <= 300 ms
        self.assertIn("guard_freshness_timeout", lats)
        self.assertTrue(lats["guard_freshness_timeout"]["passed"])
        self.assertLessEqual(
            lats["guard_freshness_timeout"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["guard_freshness_timeout"],
        )

        # Condition 3: Teleop crash <= 300 ms
        self.assertIn("teleop_crash", lats)
        self.assertTrue(lats["teleop_crash"]["passed"])
        self.assertLessEqual(
            lats["teleop_crash"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["teleop_crash"],
        )

        # Condition 4: Supervisor child crash <= 250 ms
        self.assertIn("supervisor_child_crash", lats)
        self.assertTrue(lats["supervisor_child_crash"]["passed"])
        self.assertLessEqual(
            lats["supervisor_child_crash"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["supervisor_child_crash"],
        )

        # Condition 5: Service stop SIGTERM <= 100 ms
        self.assertIn("service_stop_sigterm", lats)
        self.assertTrue(lats["service_stop_sigterm"]["passed"])
        self.assertLessEqual(
            lats["service_stop_sigterm"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["service_stop_sigterm"],
        )

        # Condition 6: Serial loss <= 600 ms
        self.assertIn("serial_loss", lats)
        self.assertTrue(lats["serial_loss"]["passed"])
        self.assertLessEqual(
            lats["serial_loss"]["measured_ms"],
            ACCEPTED_LATENCY_BOUNDS_MS["serial_loss"],
        )

    # 19. STM32 Command-Loss Characterization
    def test_stm32_command_loss_characterization(self):
        """Verify characterization of host zeroing vs STM32 firmware timeout."""
        orch = BenchAcceptanceOrchestrator(mock=True)
        orch.record_stm32_command_loss_behavior()
        stm = orch.results["stm32_command_loss"]
        self.assertEqual(stm["mode"], "simulation")
        self.assertLessEqual(stm["host_zero_delivery_ms"], 275.0)
        self.assertLessEqual(stm["stm32_firmware_timeout_ms"], 1000.0)
        self.assertEqual(stm["operator_emergency_disconnect_sec"], 0.0)
        self.assertFalse(
            stm["safe_for_on_ground"], "On-ground motion must remain forbidden"
        )
        self.assertIn("DESIGN_SPECIFICATION", stm["host_zero_delivery_status"])

    # 20. Complete Acceptance Suite Run & Report Generation
    def test_full_acceptance_suite_and_report_generation(self):
        """Full acceptance suite must pass and emit valid JSON and Markdown reports."""
        report_json = os.path.join(self.tmp_dir, "report.json")
        report_md = os.path.join(self.tmp_dir, "report.md")

        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path,
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
        )
        passed = orch.run_acceptance_suite()
        self.assertTrue(passed)
        self.assertEqual(orch.results["status"], "SIMULATION_PASSED")

        # Write reports
        with open(report_json, "w", encoding="utf-8") as f:
            json.dump(orch.results, f, indent=2)
        with open(report_md, "w", encoding="utf-8") as f:
            f.write(orch.generate_markdown_report())

        # Validate JSON content
        with open(report_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["status"], "SIMULATION_PASSED")
        self.assertIn("preflight", data)
        self.assertIn("geometry_and_limits", data)
        self.assertIn("motion_tests", data)
        self.assertIn("latency_measurements", data)
        self.assertIn("stm32_command_loss", data)

        # Validate Markdown content
        with open(report_md, "r", encoding="utf-8") as f:
            md_text = f.read()
        self.assertIn("# Milestone 6 — Raised-Track Acceptance Report", md_text)
        self.assertIn("**SIMULATION_PASSED**", md_text)
        self.assertIn("keyboard_lease_expiry", md_text)
        self.assertIn("guard_freshness_timeout", md_text)
        self.assertIn("serial_loss", md_text)
        self.assertIn("guard_freshness_timeout", md_text)
        self.assertIn("serial_loss", md_text)

    # 21. BenchClientNode Unit Test
    def test_bench_client_node_operations(self):
        """Test BenchClientNode methods for arm/disarm, cmd_vel, and motion burst."""
        node = BenchClientNode()
        self.assertIsNone(node.guard_state)
        self.assertIsNone(node.guard_armed)

        # Mock publishing and arming
        node.cmd_vel_pub = MagicMock()
        node.arm_client = MagicMock()

        # Publish cmd_vel
        res = node.publish_cmd_vel(0.2, 0.5)
        self.assertTrue(res)
        self.assertEqual(node.cmd_vel_pub.publish.call_count, 1)

        # Send stop
        res_stop = node.send_stop(count=4)
        self.assertTrue(res_stop)
        self.assertGreaterEqual(node.cmd_vel_pub.publish.call_count, 4)

        # Motion burst
        res_burst = node.run_motion_burst(0.2, 0.0, duration_sec=0.1, rate_hz=20.0)
        self.assertTrue(res_burst)

        # Call set_arm with mock service
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = MagicMock(success=True, message="Armed")
        node.arm_client.wait_for_service.return_value = True
        node.arm_client.call_async.return_value = future

        with patch("ubuntu_tank_bringup.bench_client.rclpy") as mock_rclpy:
            mock_rclpy.ok.return_value = True
            success, msg = node.call_set_arm(True, timeout_sec=0.5)
            self.assertTrue(success)
            self.assertEqual(msg, "Armed")

    # 22. deploy.sh bench CLI integration
    def test_deploy_bench_cli_invocation(self):
        """Verify ./deploy.sh bench passes with --ack-tracks-raised and fails without it."""
        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")

        # Missing acknowledgment fails
        res_fail = subprocess.run(
            ["bash", deploy_sh, "bench"], capture_output=True, text=True
        )
        self.assertNotEqual(res_fail.returncode, 0)
        self.assertIn(
            "physical safety acknowledgment: --ack-tracks-raised", res_fail.stderr
        )

        # With acknowledgment and mock mode
        env = dict(os.environ)
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "EMPTY"
        res_pass = subprocess.run(
            ["bash", deploy_sh, "bench", "--ack-tracks-raised", "--mock"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(
            res_pass.returncode,
            0,
            f"bench failed: {res_pass.stderr}\n{res_pass.stdout}",
        )
        self.assertIn(
            "Milestone 6 Acceptance Result: SIMULATION_PASSED", res_pass.stdout
        )

    # --- Review Remediation Tests (Findings 1 & 2) ---

    def test_finding2_live_battery_preflight_fails_when_ros_unavailable(self):
        """Finding 2: In live mode (mock=False), missing ROS packages must fail preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_battery_mv=12500,  # Should be ignored in live mode!
        )
        with patch.dict("sys.modules", {"rclpy": None}):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertFalse(ok)
            self.assertIsNone(
                val, "Synthetic voltage must never be returned in live mode"
            )
            self.assertIn("unavailable", msg.lower())

    def test_finding2_live_battery_preflight_fails_when_status_node_throws(self):
        """Finding 2: In live mode, status client exceptions must fail preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, mock_battery_mv=12500
        )
        mock_rclpy = MagicMock()
        mock_rclpy.ok.return_value = True
        mock_client_cls = MagicMock()
        mock_client_cls.side_effect = RuntimeError("Failed to create subscription")

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_client_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertFalse(ok)
            self.assertIsNone(val)
            self.assertIn("Error observing live battery telemetry", msg)

    def test_finding2_live_battery_preflight_fails_when_reading_absent_or_invalid(self):
        """Finding 2: In live mode, absent or non-positive reading must fail preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, mock_battery_mv=12500
        )
        mock_rclpy = MagicMock()
        mock_rclpy.ok.return_value = True
        mock_node = MagicMock()
        mock_node.collect_status.return_value = {"battery_mv": None}
        mock_client_cls = MagicMock(return_value=mock_node)

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_client_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertFalse(ok)
            self.assertIsNone(val)
            self.assertIn("unavailable: no reading received", msg)

            # Test invalid non-positive reading
            mock_node.collect_status.return_value = {"battery_mv": -10}
            ok_inv, val_inv, msg_inv = orch._verify_battery_voltage()
            self.assertFalse(ok_inv)
            self.assertIsNone(val_inv)
            self.assertIn("Invalid battery telemetry reading", msg_inv)

    def test_finding2_live_battery_preflight_fails_when_voltage_low(self):
        """Finding 2: In live mode, fresh reading below 9600 mV must fail preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, mock_battery_mv=12500
        )
        mock_rclpy = MagicMock()
        mock_rclpy.ok.return_value = True
        mock_node = MagicMock()
        mock_node.collect_status.return_value = {"battery_mv": 9200}
        mock_client_cls = MagicMock(return_value=mock_node)

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_client_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertFalse(ok)
            self.assertEqual(val, 9200)
            self.assertIn("Battery voltage too low", msg)

    def test_finding2_live_battery_preflight_passes_on_fresh_valid_reading(self):
        """Finding 2: In live mode, fresh observed voltage >= 9600 mV passes preflight."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_battery_mv=8000,  # Even if mock is low, live observation takes precedence
        )
        mock_rclpy = MagicMock()
        mock_rclpy.ok.return_value = True
        mock_node = MagicMock()
        mock_node.collect_status.return_value = {"battery_mv": 11800}
        mock_client_cls = MagicMock(return_value=mock_node)

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_client_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertTrue(ok)
            self.assertEqual(val, 11800)
            self.assertIn("Live battery voltage verified: 11800 mV (11.80 V)", msg)

    def test_finding1_live_motion_fails_closed_when_unobserved(self):
        """Finding 1: Non-mock motion acceptance must fail closed without live ROS/service."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path, lock_path=self.lock_path, mock=False
        )
        with patch.dict("sys.modules", {"rclpy": None}):
            ok, errors = orch.run_motion_acceptance_tests()
            self.assertFalse(ok)
            self.assertTrue(any("Live motion execution failed" in e for e in errors))
            seq = orch.results["motion_tests"]["execution_sequence"]
            self.assertEqual(seq["mode"], "live_hardware")
            self.assertFalse(seq["passed"])
            self.assertFalse(seq["armed_before_run"])
            self.assertEqual(seq["finite_bursts_executed"], [])
            self.assertFalse(seq["all_bursts_ended_in_zero"])
            self.assertFalse(seq["disarmed_after_run"])

    def test_finding1_live_motion_executes_with_observed_client(self):
        """Finding 1: Non-mock motion acceptance executes bursts and verifies disarm via BenchClientNode."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path, lock_path=self.lock_path, mock=False
        )
        mock_rclpy = MagicMock()
        mock_rclpy.ok.return_value = True
        mock_node = MagicMock()
        mock_node.arm_client.wait_for_service.return_value = True
        mock_node.call_set_arm.return_value = (True, "acknowledged")
        mock_node.wait_for_state.side_effect = [
            {"guard_state": True, "guard_armed": True},
            {"guard_state": True, "guard_armed": True},
            {"guard_state": False, "guard_armed": False},
        ] * 4
        mock_bench_cls = MagicMock(return_value=mock_node)

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "ubuntu_tank_bringup.bench_client": MagicMock(
                    BenchClientNode=mock_bench_cls
                ),
            },
        ):
            ok, errors = orch.run_motion_acceptance_tests()
            self.assertTrue(
                ok, f"Expected live motion with mock BenchClient to pass: {errors}"
            )
            seq = orch.results["motion_tests"]["execution_sequence"]
            self.assertEqual(seq["mode"], "live_hardware")
            self.assertTrue(seq["passed"])
            self.assertTrue(seq["armed_before_run"])
            self.assertEqual(
                seq["finite_bursts_executed"],
                ["forward", "reverse", "spin_left", "spin_right"],
            )
            self.assertTrue(seq["all_bursts_ended_in_zero"])
            self.assertTrue(seq["disarmed_after_run"])

    def test_finding1_live_latency_measurement_rejects_synthetic_timing(self):
        """Finding 1: Non-mock latency measurement must reject synthetic timing and report pending."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path, lock_path=self.lock_path, mock=False
        )
        ok, errors = orch.measure_stop_latencies()
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "synthetic measurements are forbidden in live mode" in e for e in errors
            )
        )
        lats = orch.results["latency_measurements"]
        for cond in ACCEPTED_LATENCY_BOUNDS_MS.keys():
            self.assertIn(cond, lats)
            self.assertFalse(lats[cond]["passed"])
            self.assertIsNone(lats[cond]["measured_ms"])
            self.assertEqual(lats[cond]["status"], "PENDING_PHYSICAL_MEASUREMENT")

    def test_finding1_live_stm32_characterization_records_pending_status(self):
        """Finding 1: Non-mock STM32 characterization must report pending status without physical measurement."""
        orch = BenchAcceptanceOrchestrator(mock=False)
        orch.record_stm32_command_loss_behavior()
        stm = orch.results["stm32_command_loss"]
        self.assertEqual(stm["mode"], "live_hardware")
        self.assertIsNone(stm["host_zero_delivery_ms"])
        self.assertIsNone(stm["stm32_firmware_timeout_ms"])
        self.assertEqual(
            stm["host_zero_delivery_status"], "PENDING_PHYSICAL_MEASUREMENT"
        )
        self.assertIn(
            "PENDING_PHYSICAL_BENCH_TEST", stm["stm32_firmware_timeout_status"]
        )
        self.assertFalse(stm["safe_for_on_ground"])

    def test_finding1_full_live_suite_fails_without_physical_hardware(self):
        """Finding 1: Full acceptance suite with mock=False in hardware-free environment must fail closed."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path,
            lock_path=self.lock_path,
            mock=False,
            mock_containers="EMPTY",
        )
        passed = orch.run_acceptance_suite()
        self.assertFalse(passed)
        self.assertEqual(orch.results["status"], "FAILED")

    # 26. Review Finding 1: Context Isolation & Enclave Credential Selection
    def test_finding1_separate_contexts_for_status_preflight_and_operator_motion(self):
        """Finding 1: Status preflight and operator motion must use separate, isolated ROS contexts."""
        orch = BenchAcceptanceOrchestrator(
            config_path=self.config_path,
            lock_path=self.lock_path,
            mock=False,
            mock_battery_mv=12000,
        )

        created_contexts = []

        class MockContext:
            def __init__(self):
                self.enclave = None
                self._is_ok = False
                created_contexts.append(self)

            def init(self):
                self.enclave = os.environ.get("ROS_SECURITY_ENCLAVE_OVERRIDE")
                self._is_ok = True

            def ok(self):
                return self._is_ok

            def shutdown(self):
                self._is_ok = False

        mock_rclpy = MagicMock()
        mock_rclpy.context.Context = MockContext

        mock_status_node = MagicMock()
        mock_status_node.collect_status.return_value = {"battery_mv": 12100}
        mock_status_cls = MagicMock(return_value=mock_status_node)

        mock_bench_node = MagicMock()
        mock_bench_node.arm_client.wait_for_service.return_value = True
        mock_bench_node.call_set_arm.return_value = (True, "acknowledged")
        mock_bench_node.wait_for_state.side_effect = [
            {"guard_state": True, "guard_armed": True},
            {"guard_state": True, "guard_armed": True},
            {"guard_state": False, "guard_armed": False},
        ] * 4
        mock_bench_cls = MagicMock(return_value=mock_bench_node)

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "rclpy.context": mock_rclpy.context,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_status_cls
                ),
                "ubuntu_tank_bringup.bench_client": MagicMock(
                    BenchClientNode=mock_bench_cls
                ),
            },
        ):
            # 1. Run battery preflight
            bat_ok, val, msg = orch._verify_battery_voltage()
            self.assertTrue(bat_ok)
            self.assertEqual(val, 12100)
            self.assertEqual(len(created_contexts), 1)
            status_ctx = created_contexts[0]
            self.assertEqual(status_ctx.enclave, "/ubuntu_tank/status")
            self.assertFalse(
                status_ctx.ok(), "Status context must be shut down following preflight"
            )
            self.assertEqual(mock_status_cls.call_args[1].get("context"), status_ctx)

            # 2. Run motion acceptance
            mot_ok, mot_errors = orch.run_motion_acceptance_tests()
            self.assertTrue(mot_ok, f"Motion failed: {mot_errors}")
            self.assertEqual(len(created_contexts), 2)
            operator_ctx = created_contexts[1]
            self.assertEqual(operator_ctx.enclave, "/ubuntu_tank/operator")
            self.assertFalse(
                operator_ctx.ok(),
                "Operator context must be shut down following motion run",
            )
            self.assertIsNot(
                operator_ctx,
                status_ctx,
                "Motion must not reuse status preflight context",
            )
            self.assertEqual(mock_bench_cls.call_args[1].get("context"), operator_ctx)

    def test_finding1_sros2_permissions_and_fake_guard_enforcement(self):
        """Finding 1: SROS2 permissions strictly enforce read-only status and operator arming on fake guard."""
        import sros2_policy

        # Status enclave: strictly read-only
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status",
                "subscribe_topic",
                "/ros_robot_controller/battery",
            )
        )
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "request_service", "/ubuntu_tank_safety/set_arm"
            ),
            "Status enclave must NOT be permitted to call /ubuntu_tank_safety/set_arm",
        )
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "publish_topic", "/controller/cmd_vel"
            ),
            "Status enclave must NOT be permitted to publish /controller/cmd_vel",
        )

        # Operator enclave: permitted to arm and publish cmd_vel
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/operator",
                "request_service",
                "/ubuntu_tank_safety/set_arm",
            )
        )
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/operator", "publish_topic", "/controller/cmd_vel"
            )
        )

        # Verify interaction with fake guard without touching hardware motors
        fake_guard = MotorGuard(max_rps=2.0, timeout_sec=0.250)
        self.assertFalse(fake_guard.is_armed)

        # Operator arms and disarms
        can_arm = sros2_policy.simulate_participant_access(
            "/ubuntu_tank/operator", "request_service", "/ubuntu_tank_safety/set_arm"
        )
        if can_arm:
            fake_guard.arm()
        self.assertTrue(fake_guard.is_armed)

        can_disarm = sros2_policy.simulate_participant_access(
            "/ubuntu_tank/operator", "request_service", "/ubuntu_tank_safety/set_arm"
        )
        if can_disarm:
            fake_guard.disarm()
        self.assertFalse(fake_guard.is_armed)

        # Rejection: If status enclave attempts to arm, policy must forbid it
        can_status_arm = sros2_policy.simulate_participant_access(
            "/ubuntu_tank/status", "request_service", "/ubuntu_tank_safety/set_arm"
        )
        self.assertFalse(can_status_arm)
        if can_status_arm:
            fake_guard.arm()
        self.assertFalse(
            fake_guard.is_armed,
            "Fake guard must remain disarmed when status enclave is rejected",
        )

    def test_finding1_caller_owned_context_is_not_shut_down(self):
        """Finding 1: Preflight must NOT shut down a caller-owned default context in fallback mode."""
        orch = BenchAcceptanceOrchestrator(mock=False)

        mock_rclpy = MagicMock()
        mock_rclpy.Context = None
        mock_rclpy.context = None
        mock_rclpy.ok.return_value = True  # Caller already initialized context!
        mock_node = MagicMock()
        mock_node.collect_status.return_value = {"battery_mv": 12200}
        mock_status_cls = MagicMock(return_value=mock_node)

        # Hide Context to test fallback path
        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "rclpy.context": None,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_status_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertTrue(ok)
            # Must NOT call rclpy.shutdown() on caller-owned context!
            mock_rclpy.shutdown.assert_not_called()

        # If rclpy was NOT ok before preflight (preflight owned it), verify shutdown IS called
        state = {"ok": False}

        def fake_init():
            state["ok"] = True

        def fake_shutdown():
            state["ok"] = False

        def fake_ok():
            return state["ok"]

        mock_rclpy_not_owned = MagicMock()
        mock_rclpy_not_owned.Context = None
        mock_rclpy_not_owned.context = None
        mock_rclpy_not_owned.init.side_effect = fake_init
        mock_rclpy_not_owned.shutdown.side_effect = fake_shutdown
        mock_rclpy_not_owned.ok.side_effect = fake_ok

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy_not_owned,
                "rclpy.context": None,
                "ubuntu_tank_bringup.status_client": MagicMock(
                    StatusClientNode=mock_status_cls
                ),
            },
        ):
            ok, val, msg = orch._verify_battery_voltage()
            self.assertTrue(ok)
            mock_rclpy_not_owned.shutdown.assert_called_once()

    def test_client_nodes_accept_explicit_context(self):
        """StatusClientNode and BenchClientNode must accept explicit context parameter."""
        mock_ctx = MagicMock()
        mock_ctx.ok.return_value = True

        status_node = StatusClientNode(context=mock_ctx)
        self.assertTrue(status_node._is_ok())
        self.assertEqual(status_node.context, mock_ctx)
        status_node.destroy_node()

        bench_node = BenchClientNode(context=mock_ctx)
        self.assertTrue(bench_node._is_ok())
        self.assertEqual(bench_node.context, mock_ctx)
        bench_node.destroy_node()

    def test_finding1_inherited_read_only_context_property_respected(self):
        """Finding 1: Do not assign the inherited read-only ROS context property."""
        import types

        class PinnedUpstreamNode:
            """Faithfully mirrors rclpy 10.0.10 BaseNode.context property descriptor."""

            def __init__(self, node_name: str, *, context=None, **kwargs):
                self._context = (
                    context if context is not None else "default_rclpy_context"
                )

            @property
            def context(self):
                return self._context

            def create_subscription(self, *args, **kwargs):
                return MagicMock()

            def create_publisher(self, *args, **kwargs):
                return MagicMock()

            def create_client(self, *args, **kwargs):
                return MagicMock()

            def destroy_node(self):
                pass

        mock_rclpy = types.ModuleType("rclpy")
        mock_node_mod = types.ModuleType("rclpy.node")
        mock_param_mod = types.ModuleType("rclpy.parameter")
        mock_qos_mod = types.ModuleType("rclpy.qos")
        mock_geo_msgs = types.ModuleType("geometry_msgs")
        mock_geo_msgs_msg = types.ModuleType("geometry_msgs.msg")
        mock_std_msgs = types.ModuleType("std_msgs")
        mock_std_msgs_msg = types.ModuleType("std_msgs.msg")
        mock_std_srvs = types.ModuleType("std_srvs")
        mock_std_srvs_srv = types.ModuleType("std_srvs.srv")

        mock_node_mod.Node = PinnedUpstreamNode
        mock_param_mod.Parameter = None
        mock_qos_mod.QoSProfile = None
        mock_qos_mod.DurabilityPolicy = None
        mock_qos_mod.ReliabilityPolicy = None
        mock_geo_msgs_msg.Twist = None
        mock_geo_msgs.msg = mock_geo_msgs_msg
        mock_std_msgs_msg.Bool = None
        mock_std_msgs_msg.UInt16 = None
        mock_std_msgs.msg = mock_std_msgs_msg
        mock_std_srvs_srv.SetBool = None
        mock_std_srvs.srv = mock_std_srvs_srv

        mock_rclpy.node = mock_node_mod
        mock_rclpy.parameter = mock_param_mod
        mock_rclpy.qos = mock_qos_mod
        mock_rclpy.ok = lambda: True

        with patch.dict(
            "sys.modules",
            {
                "rclpy": mock_rclpy,
                "rclpy.node": mock_node_mod,
                "rclpy.parameter": mock_param_mod,
                "rclpy.qos": mock_qos_mod,
                "geometry_msgs": mock_geo_msgs,
                "geometry_msgs.msg": mock_geo_msgs_msg,
                "std_msgs": mock_std_msgs,
                "std_msgs.msg": mock_std_msgs_msg,
                "std_srvs": mock_std_srvs,
                "std_srvs.srv": mock_std_srvs_srv,
            },
        ):
            import importlib
            import ubuntu_tank_bringup.status_client as sc
            import ubuntu_tank_bringup.bench_client as bc

            importlib.reload(sc)
            importlib.reload(bc)

            # 1. StatusClientNode with default context (context=None)
            s_def = sc.StatusClientNode()
            self.assertEqual(s_def.context, "default_rclpy_context")
            with self.assertRaises(AttributeError):
                s_def.context = "attempted_reassignment"
            s_def.destroy_node()

            # 2. StatusClientNode with explicit context
            mock_status_ctx = MagicMock()
            mock_status_ctx.ok.return_value = True
            s_exp = sc.StatusClientNode(context=mock_status_ctx)
            self.assertIs(s_exp.context, mock_status_ctx)
            self.assertTrue(s_exp._is_ok())
            with self.assertRaises(AttributeError):
                s_exp.context = "attempted_reassignment"
            s_exp.destroy_node()

            # 3. BenchClientNode with default context (context=None)
            b_def = bc.BenchClientNode()
            self.assertEqual(b_def.context, "default_rclpy_context")
            with self.assertRaises(AttributeError):
                b_def.context = "attempted_reassignment"
            b_def.destroy_node()

            # 4. BenchClientNode with explicit context
            mock_op_ctx = MagicMock()
            mock_op_ctx.ok.return_value = True
            b_exp = bc.BenchClientNode(context=mock_op_ctx)
            self.assertIs(b_exp.context, mock_op_ctx)
            self.assertTrue(b_exp._is_ok())
            with self.assertRaises(AttributeError):
                b_exp.context = "attempted_reassignment"
            b_exp.destroy_node()

            # Revert reloaded modules back to clean state
            importlib.reload(sc)
            importlib.reload(bc)

    def test_finding1_fallback_node_mirrors_readonly_context_property(self):
        """Finding 1: Fallback Node class in absence of rclpy also mirrors read-only property."""
        import ubuntu_tank_bringup.status_client as sc
        import ubuntu_tank_bringup.bench_client as bc

        # When rclpy is None, StatusClientNode and BenchClientNode use _FallbackNode
        s_node = sc.StatusClientNode(context="fallback_status_ctx")
        self.assertEqual(s_node.context, "fallback_status_ctx")
        with self.assertRaises(AttributeError):
            s_node.context = "tamper"
        s_node.destroy_node()

        b_node = bc.BenchClientNode(context="fallback_op_ctx")
        self.assertEqual(b_node.context, "fallback_op_ctx")
        with self.assertRaises(AttributeError):
            b_node.context = "tamper"
        b_node.destroy_node()


class TestBenchSafetyRegressions(unittest.TestCase):
    """Exercise real guard deadlines and deployment coordination without hardware."""

    def test_failed_prerequisites_never_enter_motion(self):
        for fault in ("battery", "USB", "owner", "lock", "geometry"):
            with self.subTest(fault=fault):
                orch = BenchAcceptanceOrchestrator(mock=True)
                with (
                    patch.object(
                        orch,
                        "run_preflight_checks",
                        return_value=(fault == "geometry", [fault]),
                    ),
                    patch.object(
                        orch,
                        "verify_geometry_and_limits",
                        return_value=(fault != "geometry", [fault]),
                    ),
                    patch.object(orch, "run_motion_acceptance_tests") as motion,
                ):
                    self.assertFalse(orch.run_acceptance_suite())
                    motion.assert_not_called()
                    self.assertEqual(
                        orch.results["motion_tests"]["execution_sequence"]["status"],
                        "SKIPPED",
                    )

    def test_invalid_kinematics_never_construct_live_client(self):
        orch = BenchAcceptanceOrchestrator(mock=False)
        with (
            patch(
                "scripts.bench_acceptance.check_motor_polarity",
                return_value=(False, "wrong polarity"),
            ),
            patch("ubuntu_tank_bringup.bench_client.BenchClientNode") as client,
        ):
            self.assertFalse(orch.run_motion_acceptance_tests()[0])
            client.assert_not_called()

    def test_rearms_each_burst_with_production_guard(self):
        self._exercise_guard_sequence(False)

    def test_unexpected_disarm_rejects_burst(self):
        self._exercise_guard_sequence(True)

    def _exercise_guard_sequence(self, inject_fault):
        guard = MotorGuard(timeout_sec=0.250)
        clock = [0.0]
        forwarded = []
        arms = []

        def advance(seconds):
            clock[0] += seconds
            guard.check_timeout(clock[0])

        def set_arm(value, **kwargs):
            arms.append(value)
            if value:
                return guard.arm(now_monotonic=clock[0])
            ok, message, _ = guard.disarm()
            return ok, message

        def burst(lx, az, **kwargs):
            command = compute_kinematic_motor_speeds(lx, az, 0.1368, 0.1446, 0.075)
            accepted, _, _ = guard.handle_command(command, clock[0])
            forwarded.append(accepted is not None)
            if inject_fault:
                guard.disarm()
            return accepted is not None

        def stop(**kwargs):
            guard.handle_command(guard.get_zero_command(), clock[0])
            return True

        node = MagicMock()
        node.call_set_arm.side_effect = set_arm
        node.run_motion_burst.side_effect = burst
        node.send_stop.side_effect = stop
        node.wait_for_state.side_effect = lambda **kw: {
            "guard_state": guard.is_armed,
            "guard_armed": guard.is_armed,
        }
        ros = MagicMock()
        ros.ok.return_value = True
        orch = BenchAcceptanceOrchestrator(mock=False)
        with (
            patch.dict(sys.modules, {"rclpy": ros}),
            patch(
                "ubuntu_tank_bringup.bench_client.BenchClientNode", return_value=node
            ),
            patch("scripts.bench_acceptance.time.sleep", side_effect=advance),
        ):
            passed, errors = orch.run_motion_acceptance_tests()
        self.assertFalse(guard.is_armed)
        if inject_fault:
            self.assertFalse(passed)
            self.assertTrue(errors)
            self.assertEqual(forwarded, [True])
        else:
            self.assertTrue(passed, errors)
            self.assertEqual(forwarded, [True] * 4)
            self.assertEqual(arms.count(True), 4)

    def test_shared_lock_is_read_only_and_held_until_suite_finishes(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "deploy.lock")
            with open(path, "w") as stream:
                stream.write("unchanged metadata")
            os.chmod(path, 0o444)
            orch = BenchAcceptanceOrchestrator(mock=False, lock_path=path)
            real_open = os.open
            with patch("scripts.bench_acceptance.os.open", wraps=real_open) as opening:
                with orch.deployment_read_lock():
                    opening.assert_called_once_with(path, os.O_RDONLY)
                    fd = real_open(path, os.O_RDONLY)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    finally:
                        os.close(fd)
            with open(path) as stream:
                self.assertEqual(stream.read(), "unchanged metadata")
            fd = real_open(path, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    with orch.deployment_read_lock():
                        self.fail("exclusive deployment must block bench")
            finally:
                os.close(fd)

    def test_only_verified_bridge_holder_is_allowed(self):
        from scripts.deployment_manager import check_hardware_mutual_exclusion

        for holders, expected in [("42", True), ("42 77", False), ("77", False)]:
            with (
                self.subTest(holders=holders),
                patch(
                    "scripts.deployment_manager.shutil.which",
                    return_value="/usr/bin/fuser",
                ),
                patch("scripts.deployment_manager.subprocess.run") as run,
            ):
                run.side_effect = lambda args, **kw: subprocess.CompletedProcess(
                    args,
                    0 if args[0] == "fuser" else 3,
                    stdout=holders if args[0] == "fuser" else "",
                    stderr="",
                )
                ok, errors = check_hardware_mutual_exclusion(
                    serial_dev="/dev/null",
                    mock_containers="EMPTY",
                    allowed_serial_pid=42,
                )
                self.assertEqual(ok, expected, errors)

    def test_stale_bridge_pid_rejected(self):
        import io

        orch = BenchAcceptanceOrchestrator(mock=False)
        for active, group, expected in [
            ("active", "/system.slice/mentorpi-tank.service", True),
            ("inactive", "/system.slice/mentorpi-tank.service", False),
            ("active", "/another.service", False),
        ]:
            with self.subTest(active=active, group=group):
                result = subprocess.CompletedProcess(
                    [],
                    0,
                    "ActiveState="
                    + active
                    + "\nControlGroup=/system.slice/mentorpi-tank.service\n",
                )
                with (
                    patch(
                        "scripts.bench_acceptance.subprocess.run", return_value=result
                    ),
                    patch(
                        "scripts.bench_acceptance.os.walk",
                        return_value=[("/sys/fs/cgroup/service", [], ["cgroup.procs"])],
                    ),
                    patch(
                        "scripts.bench_acceptance.open",
                        create=True,
                        side_effect=[
                            io.StringIO("42"),
                            io.BytesIO(
                                b"/opt/install/lib/ros_robot_controller/ros_robot_controller\0"
                            ),
                            io.StringIO("0::" + group + "\n"),
                        ],
                    ),
                ):
                    if expected:
                        self.assertEqual(orch.managed_bridge_pid(), 42)
                    else:
                        with self.assertRaises(RuntimeError):
                            orch.managed_bridge_pid()


if __name__ == "__main__":
    unittest.main()
