"""
test_milestone9_physical_closure.py - Unit and integration tests for Milestone 9:
Installed candidate and physical acceptance closure (§11.9, §10.3).

Validates:
1. Complete, valid owner physical observations close physical acceptance in live mode.
2. Rejection of missing motion directions (forward, reverse, spin_left, spin_right).
3. Rejection of direction mismatches (observed direction != commanded direction).
4. Rejection of runaway / unstopped tracks (stopped_after_burst != True).
5. Rejection of missing observer identity.
6. Validation of physical stop latency measurements against accepted bounds.
7. Validation of STM32 command-loss safe stop measurements and contingency verification.
8. Safety invariant: mock/simulation mode NEVER certifies physical acceptance.
9. CLI parsing of --physical-observations from file and inline JSON.
10. Interactive observation entry flow.
11. Markdown report formatting with owner observations and ACCEPTED status.
"""

import json
import os
import shutil
import sys
import tempfile
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
    ACCEPTED_LATENCY_BOUNDS_MS,
    BenchAcceptanceOrchestrator,
)


def _make_valid_physical_observations():
    return {
        "observed_movements": {
            "forward": {
                "observed": True,
                "direction_matched": True,
                "stopped_after_burst": True,
                "observer": "owner",
            },
            "reverse": {
                "observed": True,
                "direction_matched": True,
                "stopped_after_burst": True,
                "observer": "owner",
            },
            "spin_left": {
                "observed": True,
                "direction_matched": True,
                "stopped_after_burst": True,
                "observer": "owner",
            },
            "spin_right": {
                "observed": True,
                "direction_matched": True,
                "stopped_after_burst": True,
                "observer": "owner",
            },
        },
        "physical_latencies": {
            "keyboard_lease_expiry": {
                "measured_ms": 155.0,
                "measurement_method": "logic_analyzer",
            },
            "terminal_loss": {
                "measured_ms": 160.0,
                "measurement_method": "logic_analyzer",
            },
            "teleop_crash": {
                "measured_ms": 260.0,
                "measurement_method": "logic_analyzer",
            },
            "guard_freshness_timeout": {
                "measured_ms": 260.0,
                "measurement_method": "logic_analyzer",
            },
            "guard_crash": {
                "measured_ms": 120.0,
                "measurement_method": "logic_analyzer",
            },
            "bridge_crash": {
                "measured_ms": 125.0,
                "measurement_method": "logic_analyzer",
            },
            "supervisor_child_crash": {
                "measured_ms": 120.0,
                "measurement_method": "logic_analyzer",
            },
            "service_stop_sigterm": {
                "measured_ms": 15.0,
                "measurement_method": "logic_analyzer",
            },
            "serial_disconnect": {
                "measured_ms": 510.0,
                "measurement_method": "logic_analyzer",
            },
            "serial_loss": {
                "measured_ms": 510.0,
                "measurement_method": "logic_analyzer",
            },
            "host_shutdown": {
                "measured_ms": 20.0,
                "measurement_method": "logic_analyzer",
            },
        },
        "stm32_command_loss": {
            "host_zero_delivery_ms": 260.0,
            "stm32_firmware_timeout_ms": 280.0,
            "measured_stop_ms": 280.0,
            "safe_stop_observed": True,
            "contingency_verified": True,
            "measurement_method": "serial_cut_oscilloscope",
        },
    }


class TestMilestone9PhysicalObservationsValidation(unittest.TestCase):
    """Test schema and rule validation for owner physical observations."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "test.lock")
        self.orch = BenchAcceptanceOrchestrator(lock_path=self.lock_path, mock=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_valid_physical_observations_pass_validation(self):
        obs = _make_valid_physical_observations()
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertTrue(ok, f"Expected validation to pass, got errors: {errs}")
        self.assertEqual(len(errs), 0)

    def test_missing_observed_movements_dict_fails(self):
        ok, errs = self.orch.validate_physical_observations({})
        self.assertFalse(ok)
        self.assertTrue(any("Missing 'observed_movements'" in e for e in errs))

    def test_missing_required_motion_fails(self):
        obs = _make_valid_physical_observations()
        del obs["observed_movements"]["spin_right"]
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("spin_right" in e for e in errs))

    def test_direction_mismatch_fails(self):
        obs = _make_valid_physical_observations()
        obs["observed_movements"]["forward"]["direction_matched"] = False
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("did not match commanded direction" in e for e in errs))

    def test_unstopped_tracks_fails(self):
        obs = _make_valid_physical_observations()
        obs["observed_movements"]["reverse"]["stopped_after_burst"] = False
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("did not come to a complete stop" in e for e in errs))

    def test_missing_observer_identity_fails(self):
        obs = _make_valid_physical_observations()
        obs["observed_movements"]["spin_left"]["observer"] = ""
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("observer identity" in e for e in errs))

    def test_excessive_latency_fails_validation(self):
        obs = _make_valid_physical_observations()
        # Bound is 300 ms for guard_freshness_timeout
        obs["physical_latencies"]["guard_freshness_timeout"]["measured_ms"] = 350.0
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("exceeded bound" in e for e in errs))

    def test_unsafe_stm32_command_loss_fails_validation(self):
        obs = _make_valid_physical_observations()
        obs["stm32_command_loss"]["safe_stop_observed"] = False
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("safe stopping" in e for e in errs))

    def test_missing_emergency_contingency_fails_validation(self):
        obs = _make_valid_physical_observations()
        obs["stm32_command_loss"]["contingency_verified"] = False
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("emergency disconnect contingency" in e for e in errs))

    def test_missing_physical_latencies_dict_fails(self):
        obs = _make_valid_physical_observations()
        del obs["physical_latencies"]
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("Missing required 'physical_latencies'" in e for e in errs))

    def test_missing_stm32_command_loss_dict_fails(self):
        obs = _make_valid_physical_observations()
        del obs["stm32_command_loss"]
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("Missing required 'stm32_command_loss'" in e for e in errs))

    def test_omitting_each_required_latency_fails(self):
        from scripts.bench_acceptance import REQUIRED_FAILURE_CONDITIONS

        for cond in REQUIRED_FAILURE_CONDITIONS:
            obs = _make_valid_physical_observations()
            del obs["physical_latencies"][cond]
            if (
                cond == "serial_disconnect"
                and "serial_loss" in obs["physical_latencies"]
            ):
                del obs["physical_latencies"]["serial_loss"]
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Expected omitting {cond} to fail validation")
            self.assertTrue(
                any(cond in e for e in errs),
                f"Expected error mentioning {cond}, got {errs}",
            )

    def test_separate_guard_and_bridge_crash_validation(self):
        obs_no_guard = _make_valid_physical_observations()
        del obs_no_guard["physical_latencies"]["guard_crash"]
        ok1, errs1 = self.orch.validate_physical_observations(obs_no_guard)
        self.assertFalse(ok1)
        self.assertTrue(any("guard_crash" in e for e in errs1))

        obs_no_bridge = _make_valid_physical_observations()
        del obs_no_bridge["physical_latencies"]["bridge_crash"]
        ok2, errs2 = self.orch.validate_physical_observations(obs_no_bridge)
        self.assertFalse(ok2)
        self.assertTrue(any("bridge_crash" in e for e in errs2))

    def test_terminal_loss_and_host_shutdown_validation(self):
        obs_no_term = _make_valid_physical_observations()
        del obs_no_term["physical_latencies"]["terminal_loss"]
        ok1, errs1 = self.orch.validate_physical_observations(obs_no_term)
        self.assertFalse(ok1)
        self.assertTrue(any("terminal_loss" in e for e in errs1))

        obs_no_host = _make_valid_physical_observations()
        del obs_no_host["physical_latencies"]["host_shutdown"]
        ok2, errs2 = self.orch.validate_physical_observations(obs_no_host)
        self.assertFalse(ok2)
        self.assertTrue(any("host_shutdown" in e for e in errs2))

    def test_negative_latency_duration_rejected(self):
        for bad_val in [-1, -1.0, -0.001]:
            obs = _make_valid_physical_observations()
            obs["physical_latencies"]["keyboard_lease_expiry"]["measured_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Negative duration {bad_val} must be rejected")
            self.assertTrue(
                any("invalid duration" in e for e in errs),
                f"Expected invalid duration error for {bad_val}, got {errs}",
            )

    def test_boolean_latency_duration_rejected(self):
        for bad_val in [True, False]:
            obs = _make_valid_physical_observations()
            obs["physical_latencies"]["keyboard_lease_expiry"]["measured_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Boolean duration {bad_val} must be rejected")
            self.assertTrue(
                any("invalid duration" in e for e in errs),
                f"Expected invalid duration error for {bad_val}, got {errs}",
            )

    def test_nan_and_infinity_latency_duration_rejected(self):
        for bad_val in [float("nan"), float("inf"), float("-inf")]:
            obs = _make_valid_physical_observations()
            obs["physical_latencies"]["keyboard_lease_expiry"]["measured_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"NaN/Inf duration {bad_val} must be rejected")
            self.assertTrue(
                any("invalid duration" in e for e in errs),
                f"Expected invalid duration error for {bad_val}, got {errs}",
            )

    def test_non_numeric_latency_duration_rejected(self):
        for bad_val in ["150ms", None, [], {}]:
            obs = _make_valid_physical_observations()
            obs["physical_latencies"]["keyboard_lease_expiry"]["measured_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Non-numeric duration {bad_val} must be rejected")
            self.assertTrue(any("invalid duration" in e for e in errs))

    def test_stm32_missing_host_zero_fails_validation(self):
        obs = _make_valid_physical_observations()
        del obs["stm32_command_loss"]["host_zero_delivery_ms"]
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("host_zero_delivery_ms" in e for e in errs))

    def test_stm32_missing_firmware_timeout_fails_validation(self):
        obs = _make_valid_physical_observations()
        obs["stm32_command_loss"].pop("stm32_firmware_timeout_ms", None)
        obs["stm32_command_loss"].pop("measured_stop_ms", None)
        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        self.assertTrue(any("firmware timeout" in e for e in errs))

    def test_stm32_invalid_host_zero_duration_fails_validation(self):
        for bad_val in [-10.0, True, False, float("nan"), float("inf"), "200ms", 350.0]:
            obs = _make_valid_physical_observations()
            obs["stm32_command_loss"]["host_zero_delivery_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Expected {bad_val} to fail STM32 host zero")
            self.assertTrue(
                any(("invalid duration" in e or "exceeded bound" in e) for e in errs)
            )

    def test_stm32_invalid_firmware_timeout_duration_fails_validation(self):
        for bad_val in [-5.0, True, False, float("nan"), float("inf"), "500ms", 1200.0]:
            obs = _make_valid_physical_observations()
            obs["stm32_command_loss"]["stm32_firmware_timeout_ms"] = bad_val
            obs["stm32_command_loss"]["measured_stop_ms"] = bad_val
            ok, errs = self.orch.validate_physical_observations(obs)
            self.assertFalse(ok, f"Expected {bad_val} to fail STM32 firmware timeout")
            self.assertTrue(
                any(("invalid duration" in e or "exceeded bound" in e) for e in errs)
            )

    def test_non_boolean_confirmation_fields_rejected(self):
        """String, numeric, array, object, None, and False inputs in confirmation fields must be rejected."""
        bad_inputs = ["false", "true", 1, 0, 1.0, 0.0, [True], {"val": True}, None, ""]
        targets = [
            ("observed_movements", "forward", "observed"),
            ("observed_movements", "forward", "direction_matched"),
            ("observed_movements", "forward", "stopped_after_burst"),
            ("stm32_command_loss", None, "safe_stop_observed"),
            ("stm32_command_loss", None, "contingency_verified"),
        ]
        for section, subkey, field in targets:
            for bad_val in bad_inputs:
                obs = _make_valid_physical_observations()
                if subkey:
                    obs[section][subkey][field] = bad_val
                else:
                    obs[section][field] = bad_val
                ok, errs = self.orch.validate_physical_observations(obs)
                self.assertFalse(
                    ok,
                    f"Expected non-boolean confirmation {field}={bad_val!r} ({type(bad_val).__name__}) to be rejected",
                )
                self.assertTrue(
                    any("confirmation must be boolean true" in e for e in errs),
                    f"Expected boolean true error message for {field}={bad_val!r}, got: {errs}",
                )

    def test_all_five_confirmation_fields_replaced_with_string_false_fails_validation(
        self,
    ):
        """Primary reproduction: replacing all 5 confirmation fields with string 'false' must be rejected."""
        obs = _make_valid_physical_observations()
        for m in ["forward", "reverse", "spin_left", "spin_right"]:
            obs["observed_movements"][m]["observed"] = "false"
            obs["observed_movements"][m]["direction_matched"] = "false"
            obs["observed_movements"][m]["stopped_after_burst"] = "false"
        obs["stm32_command_loss"]["safe_stop_observed"] = "false"
        obs["stm32_command_loss"]["contingency_verified"] = "false"

        ok, errs = self.orch.validate_physical_observations(obs)
        self.assertFalse(ok)
        # Should have at least 14 errors (4 motions * 3 fields + 2 stm32 fields)
        self.assertGreaterEqual(len(errs), 14)
        for e in errs:
            self.assertIn("confirmation must be boolean true", e)


class TestMilestone9PhysicalAcceptanceClosure(unittest.TestCase):
    """Test full acceptance suite closure with physical observations."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "test.lock")
        with open(self.lock_path, "w") as f:
            f.write("")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("ubuntu_tank_bringup.bench_client.BenchClientNode")
    @patch("ubuntu_tank_bringup.status_client.StatusClientNode")
    def test_live_mode_with_valid_physical_observations_accepts_milestone9(
        self, mock_status_cls, mock_bench_cls
    ):
        """In live hardware mode, verified software delivery + physical observations = ACCEPTED."""
        obs = _make_valid_physical_observations()

        # Mock bench node
        bench_node = MagicMock()
        bench_node.prepare_discovery.return_value = True
        bench_node.call_set_arm.return_value = (True, "OK")
        bench_node.run_motion_burst.return_value = True
        bench_node.wait_for_state.side_effect = [
            {"guard_armed": True},
            {"guard_armed": True},
            {"guard_armed": False},
        ] * 4
        bench_node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {
                "sink_type": "serial",
                "bytes_written": 27,
                "bytes_expected": 27,
                "success": True,
            },
        )
        bench_node.verify_disarm_stop_delivery.return_value = (
            True,
            "Disarm stop verified",
            {"sink_type": "serial"},
        )
        mock_bench_cls.return_value = bench_node

        # Mock status node
        status_node = MagicMock()
        status_node.read_status.return_value = {
            "guard_state": True,
            "battery_mv": 12100,
        }
        mock_status_cls.return_value = status_node

        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_containers="EMPTY",
            mock_serial_holder="EMPTY",
            physical_observations=obs,
        )

        with patch(
            "scripts.bench_acceptance.check_hardware_mutual_exclusion",
            return_value=(True, []),
        ):
            with patch.object(orch, "run_preflight_checks", return_value=(True, [])):
                with patch.object(
                    orch, "verify_geometry_and_limits", return_value=(True, [])
                ):
                    with patch.dict(sys.modules, {"rclpy": MagicMock()}):
                        suite_ok = orch.run_acceptance_suite()

        self.assertTrue(suite_ok)
        self.assertEqual(orch.results["software_delivery_status"], "PASSED")
        self.assertEqual(orch.results["physical_acceptance_status"], "PASSED")
        self.assertEqual(orch.results["status"], "ACCEPTED")

        # Verify execution sequence
        exec_seq = orch.results["motion_tests"]["execution_sequence"]
        self.assertTrue(exec_seq["physical_movement_verified"])
        self.assertTrue(exec_seq["software_delivery_verified"])

        # Check Markdown report
        md = orch.generate_markdown_report()
        self.assertIn("**Overall Status**: **ACCEPTED**", md)
        self.assertIn("**PASS (owner)**", md)
        self.assertIn("PHYSICAL_PASS", md)
        self.assertIn("PHYSICALLY_VERIFIED", md)

    def test_mock_mode_strictly_rejects_physical_acceptance_closure(self):
        """Mock mode must NEVER produce physical acceptance pass even if observations provided."""
        obs = _make_valid_physical_observations()
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=True,
            mock_containers="EMPTY",
            physical_observations=obs,
        )
        suite_ok = orch.run_acceptance_suite()
        self.assertTrue(suite_ok)
        # Software passes, but physical acceptance MUST remain INCOMPLETE
        self.assertEqual(orch.results["software_delivery_status"], "PASSED")
        self.assertEqual(orch.results["physical_acceptance_status"], "INCOMPLETE")
        self.assertEqual(orch.results["status"], "SIMULATION_PASSED")

        exec_seq = orch.results["motion_tests"].get("execution_sequence", {})
        self.assertFalse(exec_seq.get("physical_movement_verified", False))

        md = orch.generate_markdown_report()
        self.assertIn("SIMULATION_PASSED", md)
        self.assertIn("PENDING_OWNER_OBSERVATION", md)

    @patch("ubuntu_tank_bringup.bench_client.BenchClientNode")
    def test_live_mode_without_physical_observations_leaves_status_incomplete(
        self, mock_bench_cls
    ):
        """In live mode, software delivery passes but physical acceptance remains INCOMPLETE when unobserved."""
        bench_node = MagicMock()
        bench_node.prepare_discovery.return_value = True
        bench_node.call_set_arm.return_value = (True, "OK")
        bench_node.run_motion_burst.return_value = True
        bench_node.wait_for_state.side_effect = [
            {"guard_armed": True},
            {"guard_armed": True},
            {"guard_armed": False},
        ] * 4
        bench_node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {"sink_type": "serial"},
        )
        bench_node.verify_disarm_stop_delivery.return_value = (
            True,
            "Disarm stop verified",
            {"sink_type": "serial"},
        )
        mock_bench_cls.return_value = bench_node

        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_containers="EMPTY",
            mock_serial_holder="EMPTY",
            physical_observations=None,  # No observations provided
        )

        with patch(
            "scripts.bench_acceptance.check_hardware_mutual_exclusion",
            return_value=(True, []),
        ):
            with patch.object(orch, "run_preflight_checks", return_value=(True, [])):
                with patch.object(
                    orch, "verify_geometry_and_limits", return_value=(True, [])
                ):
                    with patch.dict(sys.modules, {"rclpy": MagicMock()}):
                        suite_ok = orch.run_acceptance_suite()

        self.assertFalse(suite_ok)  # Unmeasured live latencies fail overall suite
        self.assertEqual(orch.results["physical_acceptance_status"], "INCOMPLETE")
        self.assertNotEqual(orch.results["status"], "ACCEPTED")

    @patch("ubuntu_tank_bringup.bench_client.BenchClientNode")
    @patch("ubuntu_tank_bringup.status_client.StatusClientNode")
    def test_omitting_stm32_command_loss_does_not_declare_accepted(
        self, mock_status_cls, mock_bench_cls
    ):
        """Omitting stm32_command_loss must prevent ACCEPTED status and remain INCOMPLETE."""
        obs = _make_valid_physical_observations()
        del obs["stm32_command_loss"]

        bench_node = MagicMock()
        bench_node.prepare_discovery.return_value = True
        bench_node.call_set_arm.return_value = (True, "OK")
        bench_node.run_motion_burst.return_value = True
        bench_node.wait_for_state.side_effect = [
            {"guard_armed": True},
            {"guard_armed": True},
            {"guard_armed": False},
        ] * 4
        bench_node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {"sink_type": "serial"},
        )
        bench_node.verify_disarm_stop_delivery.return_value = (
            True,
            "Disarm stop verified",
            {"sink_type": "serial"},
        )
        mock_bench_cls.return_value = bench_node

        status_node = MagicMock()
        status_node.read_status.return_value = {
            "guard_state": True,
            "battery_mv": 12100,
        }
        mock_status_cls.return_value = status_node

        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_containers="EMPTY",
            mock_serial_holder="EMPTY",
            physical_observations=obs,
        )

        with patch(
            "scripts.bench_acceptance.check_hardware_mutual_exclusion",
            return_value=(True, []),
        ):
            with patch.object(orch, "run_preflight_checks", return_value=(True, [])):
                with patch.object(
                    orch, "verify_geometry_and_limits", return_value=(True, [])
                ):
                    with patch.dict(sys.modules, {"rclpy": MagicMock()}):
                        suite_ok = orch.run_acceptance_suite()

        self.assertFalse(suite_ok)
        self.assertEqual(orch.results["physical_acceptance_status"], "INCOMPLETE")
        self.assertEqual(orch.results["status"], "SOFTWARE_DELIVERY_PASSED")

    def test_omitted_host_zero_measurement_does_not_fabricate_275ms_or_verified_status(
        self,
    ):
        """Omitting host_zero_delivery_ms in live mode must record None and PENDING, never 275.0 ms or PHYSICALLY_VERIFIED."""
        obs = {
            "stm32_command_loss": {
                "safe_stop_observed": True,
                "contingency_verified": True,
                "stm32_firmware_timeout_ms": 300.0,
            }
        }
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, physical_observations=obs
        )
        ok, errs = orch.record_stm32_command_loss_behavior()
        self.assertFalse(ok)
        res = orch.results["stm32_command_loss"]
        self.assertIsNone(res["host_zero_delivery_ms"])
        self.assertEqual(
            res["host_zero_delivery_status"], "PENDING_PHYSICAL_MEASUREMENT"
        )
        self.assertFalse(res["passed"])
        self.assertEqual(res["status"], "PENDING_PHYSICAL_BENCH")

    def test_omitted_firmware_timeout_does_not_claim_physically_verified(self):
        """Omitting stm32_firmware_timeout_ms must record None and PENDING, never claim verified."""
        obs = {
            "stm32_command_loss": {
                "safe_stop_observed": True,
                "contingency_verified": True,
                "host_zero_delivery_ms": 250.0,
            }
        }
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, physical_observations=obs
        )
        ok, errs = orch.record_stm32_command_loss_behavior()
        self.assertFalse(ok)
        res = orch.results["stm32_command_loss"]
        self.assertIsNone(res["stm32_firmware_timeout_ms"])
        self.assertEqual(
            res["stm32_firmware_timeout_status"], "PENDING_PHYSICAL_BENCH_TEST"
        )
        self.assertFalse(res["passed"])
        self.assertEqual(res["status"], "PENDING_PHYSICAL_BENCH")

    def test_safe_stop_observed_alone_without_timings_does_not_certify_timings(self):
        """safe_stop_observed=True without measured timings must NOT certify timings."""
        obs = {
            "stm32_command_loss": {
                "safe_stop_observed": True,
                "contingency_verified": True,
            }
        }
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, physical_observations=obs
        )
        ok, errs = orch.record_stm32_command_loss_behavior()
        self.assertFalse(ok)
        res = orch.results["stm32_command_loss"]
        self.assertFalse(res["passed"])
        self.assertNotEqual(res["status"], "PHYSICALLY_VERIFIED")
        self.assertNotEqual(res["host_zero_delivery_status"], "PHYSICALLY_VERIFIED")
        self.assertNotEqual(res["stm32_firmware_timeout_status"], "PHYSICALLY_VERIFIED")

    def test_negative_latency_measurements_fail_acceptance(self):
        """Negative latency duration in physical measurements must fail measurement processing."""
        obs = _make_valid_physical_observations()
        obs["physical_latencies"]["keyboard_lease_expiry"]["measured_ms"] = -10.0
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, physical_observations=obs
        )
        ok, errs = orch.measure_stop_latencies()
        self.assertFalse(ok)
        entry = orch.results["latency_measurements"]["keyboard_lease_expiry"]
        self.assertFalse(entry["passed"])
        self.assertEqual(entry["status"], "FAILED")

    def test_boolean_latency_measurements_fail_acceptance(self):
        """Boolean duration in physical measurements must fail measurement processing."""
        obs = _make_valid_physical_observations()
        obs["physical_latencies"]["guard_freshness_timeout"]["measured_ms"] = True
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, physical_observations=obs
        )
        ok, errs = orch.measure_stop_latencies()
        self.assertFalse(ok)
        entry = orch.results["latency_measurements"]["guard_freshness_timeout"]
        self.assertFalse(entry["passed"])
        self.assertEqual(entry["status"], "FAILED")

    def test_omitting_each_required_latency_in_live_suite_fails_acceptance(self):
        """Omitting any required latency condition in live mode must record PENDING and fail acceptance."""
        from scripts.bench_acceptance import REQUIRED_FAILURE_CONDITIONS

        for cond in REQUIRED_FAILURE_CONDITIONS:
            obs = _make_valid_physical_observations()
            del obs["physical_latencies"][cond]
            if (
                cond == "serial_disconnect"
                and "serial_loss" in obs["physical_latencies"]
            ):
                del obs["physical_latencies"]["serial_loss"]
            orch = BenchAcceptanceOrchestrator(
                lock_path=self.lock_path, mock=False, physical_observations=obs
            )
            ok, errs = orch.measure_stop_latencies()
            self.assertFalse(
                ok, f"Expected omitting {cond} to fail measure_stop_latencies"
            )
            entry = orch.results["latency_measurements"][cond]
            self.assertFalse(entry["passed"])
            self.assertEqual(entry["status"], "PENDING_PHYSICAL_MEASUREMENT")

    def test_all_latencies_simulated_in_mock_mode(self):
        """All required failure conditions must be simulated and pass in mock mode."""
        from scripts.bench_acceptance import REQUIRED_FAILURE_CONDITIONS

        orch = BenchAcceptanceOrchestrator(lock_path=self.lock_path, mock=True)
        ok, errs = orch.measure_stop_latencies()
        self.assertTrue(ok, f"Mock latency simulation failed: {errs}")
        for cond in REQUIRED_FAILURE_CONDITIONS:
            self.assertIn(cond, orch.results["latency_measurements"])
            entry = orch.results["latency_measurements"][cond]

    @patch("ubuntu_tank_bringup.bench_client.BenchClientNode")
    @patch("ubuntu_tank_bringup.status_client.StatusClientNode")
    def test_all_five_confirmation_fields_as_string_false_fails_acceptance_path(
        self, mock_status_cls, mock_bench_cls
    ):
        """Full acceptance-path regression: all 5 confirmation fields set to 'false' must FAIL, never ACCEPTED/PASSED."""
        obs = _make_valid_physical_observations()
        for m in ["forward", "reverse", "spin_left", "spin_right"]:
            obs["observed_movements"][m]["observed"] = "false"
            obs["observed_movements"][m]["direction_matched"] = "false"
            obs["observed_movements"][m]["stopped_after_burst"] = "false"
        obs["stm32_command_loss"]["safe_stop_observed"] = "false"
        obs["stm32_command_loss"]["contingency_verified"] = "false"

        bench_node = MagicMock()
        bench_node.prepare_discovery.return_value = True
        bench_node.call_set_arm.return_value = (True, "OK")
        bench_node.run_motion_burst.return_value = True
        bench_node.wait_for_state.side_effect = [
            {"guard_armed": True},
            {"guard_armed": True},
            {"guard_armed": False},
        ] * 4
        bench_node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {
                "sink_type": "serial",
                "bytes_written": 27,
                "bytes_expected": 27,
                "success": True,
            },
        )
        bench_node.verify_disarm_stop_delivery.return_value = (
            True,
            "Disarm stop verified",
            {"sink_type": "serial"},
        )
        mock_bench_cls.return_value = bench_node

        status_node = MagicMock()
        status_node.read_status.return_value = {
            "guard_state": True,
            "battery_mv": 12100,
        }
        mock_status_cls.return_value = status_node

        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path,
            mock=False,
            mock_containers="EMPTY",
            mock_serial_holder="EMPTY",
            physical_observations=obs,
        )

        with patch(
            "scripts.bench_acceptance.check_hardware_mutual_exclusion",
            return_value=(True, []),
        ):
            with patch.object(orch, "run_preflight_checks", return_value=(True, [])):
                with patch.object(
                    orch, "verify_geometry_and_limits", return_value=(True, [])
                ):
                    with patch.dict(sys.modules, {"rclpy": MagicMock()}):
                        suite_ok = orch.run_acceptance_suite()

        self.assertFalse(suite_ok)
        self.assertEqual(orch.results["status"], "FAILED")
        self.assertEqual(orch.results["physical_acceptance_status"], "FAILED")
        self.assertNotEqual(orch.results["status"], "ACCEPTED")
        self.assertNotEqual(orch.results["physical_acceptance_status"], "PASSED")

        exec_seq = orch.results["motion_tests"]["execution_sequence"]
        self.assertFalse(exec_seq["physical_movement_verified"])
        self.assertEqual(exec_seq["status"], "PHYSICAL_OBSERVATION_FAILED")
        self.assertIn("confirmation must be boolean true", exec_seq["evidence"])

        stm_res = orch.results["stm32_command_loss"]
        self.assertFalse(stm_res["safe_stop_observed"])
        self.assertFalse(stm_res["contingency_verified"])
        self.assertEqual(stm_res["status"], "FAILED")

    @patch("ubuntu_tank_bringup.bench_client.BenchClientNode")
    @patch("ubuntu_tank_bringup.status_client.StatusClientNode")
    def test_individual_non_boolean_confirmation_fields_fail_acceptance_path(
        self, mock_status_cls, mock_bench_cls
    ):
        """Each non-boolean confirmation field individually must fail the acceptance path."""
        bench_node = MagicMock()
        bench_node.prepare_discovery.return_value = True
        bench_node.call_set_arm.return_value = (True, "OK")
        bench_node.run_motion_burst.return_value = True
        bench_node.wait_for_state.side_effect = [
            {"guard_armed": True},
            {"guard_armed": True},
            {"guard_armed": False},
        ] * 100
        bench_node.verify_correlated_delivery.return_value = (
            True,
            "Motion verified",
            {
                "sink_type": "serial",
                "bytes_written": 27,
                "bytes_expected": 27,
                "success": True,
            },
        )
        bench_node.verify_disarm_stop_delivery.return_value = (
            True,
            "Disarm stop verified",
            {"sink_type": "serial"},
        )
        mock_bench_cls.return_value = bench_node

        status_node = MagicMock()
        status_node.read_status.return_value = {
            "guard_state": True,
            "battery_mv": 12100,
        }
        mock_status_cls.return_value = status_node

        test_cases = [
            ("observed_movements", "forward", "observed", "false"),
            ("observed_movements", "forward", "direction_matched", "false"),
            ("observed_movements", "forward", "stopped_after_burst", "false"),
            ("stm32_command_loss", None, "safe_stop_observed", "false"),
            ("stm32_command_loss", None, "contingency_verified", "false"),
            ("observed_movements", "forward", "observed", 1),
            ("stm32_command_loss", None, "safe_stop_observed", 0),
            ("stm32_command_loss", None, "contingency_verified", "true"),
        ]

        for sec, subk, fld, bad_v in test_cases:
            obs = _make_valid_physical_observations()
            if subk:
                obs[sec][subk][fld] = bad_v
            else:
                obs[sec][fld] = bad_v

            orch = BenchAcceptanceOrchestrator(
                lock_path=self.lock_path,
                mock=False,
                mock_containers="EMPTY",
                mock_serial_holder="EMPTY",
                physical_observations=obs,
            )

            with patch(
                "scripts.bench_acceptance.check_hardware_mutual_exclusion",
                return_value=(True, []),
            ):
                with patch.object(
                    orch, "run_preflight_checks", return_value=(True, [])
                ):
                    with patch.object(
                        orch, "verify_geometry_and_limits", return_value=(True, [])
                    ):
                        with patch.dict(sys.modules, {"rclpy": MagicMock()}):
                            suite_ok = orch.run_acceptance_suite()

            self.assertFalse(
                suite_ok,
                f"Expected {sec}.{subk or ''}.{fld}={bad_v!r} to fail suite execution",
            )
            self.assertEqual(
                orch.results["status"],
                "FAILED",
                f"Expected status FAILED for {sec}.{subk or ''}.{fld}={bad_v!r}, got: {orch.results['status']}",
            )
            self.assertEqual(
                orch.results["physical_acceptance_status"],
                "FAILED",
                f"Expected physical_acceptance_status FAILED for {sec}.{subk or ''}.{fld}={bad_v!r}",
            )

    def test_interactive_observation_prompt_parses_inputs(self):
        """Interactive observation entry prompts operator and structures valid output."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, interactive_observations=True
        )

        # Simulate user inputs: 4 motions x (move=y, dir=y, stop=y, who=tester)
        user_inputs = ["y", "y", "y", "tester"] * 4
        with patch("builtins.input", side_effect=user_inputs):
            obs = orch.prompt_physical_observations()

        self.assertIn("observed_movements", obs)
        for m in ["forward", "reverse", "spin_left", "spin_right"]:
            self.assertTrue(obs["observed_movements"][m]["observed"])
            self.assertTrue(obs["observed_movements"][m]["direction_matched"])
            self.assertTrue(obs["observed_movements"][m]["stopped_after_burst"])
            self.assertEqual(obs["observed_movements"][m]["observer"], "tester")

    def test_interactive_observation_prompt_handles_eof(self):
        """Interactive observation entry handles EOF gracefully without crashing."""
        orch = BenchAcceptanceOrchestrator(
            lock_path=self.lock_path, mock=False, interactive_observations=True
        )
        with patch("builtins.input", side_effect=EOFError):
            obs = orch.prompt_physical_observations()
        self.assertEqual(obs, {"observed_movements": {}})


class TestMilestone9CliParsing(unittest.TestCase):
    """Test CLI argument parsing for --physical-observations and --interactive-observations."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_reads_observations_file(self):
        obs = _make_valid_physical_observations()
        fpath = os.path.join(self.tmp_dir, "phys_obs.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(obs, f)

        import subprocess

        res = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS_DIR, "bench_acceptance.py"),
                "--ack-tracks-raised",
                "--mock",
                "--physical-observations",
                fpath,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI execution failed: {res.stderr}")
        self.assertIn("SIMULATION_PASSED", res.stdout)

    def test_cli_reads_inline_json_observations(self):
        obs = _make_valid_physical_observations()
        obs_json = json.dumps(obs)

        import subprocess

        res = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS_DIR, "bench_acceptance.py"),
                "--ack-tracks-raised",
                "--mock",
                "--physical-observations",
                obs_json,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI execution failed: {res.stderr}")
        self.assertIn("SIMULATION_PASSED", res.stdout)

    def test_cli_rejects_invalid_json(self):
        import subprocess

        res = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS_DIR, "bench_acceptance.py"),
                "--ack-tracks-raised",
                "--mock",
                "--physical-observations",
                "{invalid_json_format",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ERROR: Failed to parse --physical-observations", res.stderr)


if __name__ == "__main__":
    unittest.main()
