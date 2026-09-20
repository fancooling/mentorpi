"""
test_milestone15_web_acceptance.py - Unit and Integration Tests for Milestone 15:
Raised-Track Web Movement and Failure Acceptance Orchestration.

Verifies:
1. Physical safety acknowledgment enforcement:
   - Fails closed if --ack-tracks-raised is omitted.
2. Platform targeting and non-target detection:
   - Accurately detects non-target environment and reports PENDING_TARGET_EXECUTION.
   - Enforces --require-target flag when running on non-target platform.
3. Physical movement observation validation:
   - Verifies all 4 directions (forward, reverse, spin_left, spin_right).
   - Enforces boolean correctness, direction match, burst stop, and observer identity.
4. Web driving safety controls observation validation:
   - Verifies key release, Space emergency stop, Disarm, Stop controller,
     idle timeout, and continuous hold cap.
5. PWA / mobile controls validation:
   - Verifies touch cancellation, app switching, screen lock, resume, and disarmed updates.
6. Measured stop latencies validation:
   - Enforces strict Section 7 bounds for all 8 web and 5 native failure modes.
   - Rejects measurements exceeding bounds or invalid duration types.
   - Validates event-to-agent, zero-write, and physical-stop breakdown timing.
7. Mock safety invariant:
   - Mock execution yields MOCK_VERIFICATION_ONLY and NEVER claims ACCEPTED status.
8. Physical acceptance on target platform:
   - Only a complete live interactive session correlated to the active release yields ACCEPTED.
9. Report generation and CLI entrypoints:
   - Generates schema-compliant JSON and Markdown reports.
   - Validates CLI argument handling, file/inline observations parsing, and exit codes.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")

for p in [REPO_ROOT, UBUNTU_TANK_DIR, SCRIPTS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ubuntu_tank.scripts.web_acceptance import (
    ACCEPTED_WEB_LATENCY_BOUNDS_MS,
    REQUIRED_FAILURE_CONDITIONS,
    REQUIRED_MOTIONS,
    REQUIRED_PWA_CONTROLS,
    REQUIRED_SAFETY_CONTROLS,
    WebAcceptanceOrchestrator,
    is_exact_bool_true,
    is_valid_duration_ms,
    main,
)

SAMPLE_VALID_OBSERVATIONS = {
    "metadata": {
        "physical_observer": "owner",
        "instruments": ["logic_analyzer", "high_speed_camera", "stopwatch"],
        "raw_evidence": ["recording://milestone15/session-001"],
        "network_conditions": "Dedicated 5 GHz LAN with injected disconnects",
        "client_versions": {
            "desktop_browser": "Chrome 140",
            "android_pwa": "Android 17 Chrome 140 installed PWA",
            "ios_pwa": "iOS 20 Safari installed PWA",
        },
        "power_disconnect_accessible": True,
        "notes": "Raised-track acceptance run on elevated chassis with tracks clear",
    },
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
    "safety_controls": {
        "key_release": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
        "space_stop": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
        "disarm": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
        "stop_controller": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
        "idle_timeout": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
        "hold_cap": {
            "verified": True,
            "motion_disarmed": True,
            "observer": "owner",
        },
    },
    "pwa_controls": {
        "touch_cancellation": {
            "verified": True,
            "no_latched_motion": True,
        },
        "app_switching": {
            "verified": True,
            "no_latched_motion": True,
        },
        "screen_lock": {
            "verified": True,
            "no_latched_motion": True,
        },
        "resume": {
            "verified": True,
            "no_latched_motion": True,
        },
        "disarmed_updates": {
            "verified": True,
            "no_latched_motion": True,
        },
    },
    "latencies": {
        "loss_of_focus": {
            "measured_ms": 142.5,
            "breakdown": {
                "event_to_agent_ms": 18.2,
                "zero_write_ms": 42.1,
                "physical_stop_ms": 82.2,
            },
        },
        "tab_close": {
            "measured_ms": 128.0,
            "breakdown": {
                "event_to_agent_ms": 15.0,
                "zero_write_ms": 38.0,
                "physical_stop_ms": 75.0,
            },
        },
        "browser_crash": {
            "measured_ms": 165.0,
            "breakdown": {
                "event_to_agent_ms": 25.0,
                "zero_write_ms": 50.0,
                "physical_stop_ms": 90.0,
            },
        },
        "wifi_loss": {
            "measured_ms": 185.0,
            "breakdown": {
                "event_to_agent_ms": 45.0,
                "zero_write_ms": 48.0,
                "physical_stop_ms": 92.0,
            },
        },
        "delayed_buffered_packets": {
            "measured_ms": 155.0,
            "breakdown": {
                "event_to_agent_ms": 20.0,
                "zero_write_ms": 45.0,
                "physical_stop_ms": 90.0,
            },
        },
        "web_crash_hang": {
            "measured_ms": 170.0,
            "breakdown": {
                "event_to_agent_ms": 30.0,
                "zero_write_ms": 50.0,
                "physical_stop_ms": 90.0,
            },
        },
        "operator_crash_hang": {
            "measured_ms": 252.0,
            "breakdown": {
                "event_to_agent_ms": 5.0,
                "zero_write_ms": 247.0,
                "physical_stop_ms": 0.0,
            },
        },
        "reconnect_behavior": {
            "measured_ms": 110.0,
            "breakdown": {
                "event_to_agent_ms": 10.0,
                "zero_write_ms": 30.0,
                "physical_stop_ms": 70.0,
            },
        },
        "guard_freshness_timeout": {
            "measured_ms": 255.0,
            "breakdown": {
                "event_to_agent_ms": 0.0,
                "zero_write_ms": 250.0,
                "physical_stop_ms": 5.0,
            },
        },
        "bridge_crash": {
            "measured_ms": 180.0,
            "breakdown": {
                "event_to_agent_ms": 15.0,
                "zero_write_ms": 65.0,
                "physical_stop_ms": 100.0,
            },
        },
        "service_stop_sigterm": {
            "measured_ms": 45.0,
            "breakdown": {
                "event_to_agent_ms": 2.0,
                "zero_write_ms": 18.0,
                "physical_stop_ms": 25.0,
            },
        },
        "serial_disconnect": {
            "measured_ms": 510.0,
            "breakdown": {
                "event_to_agent_ms": 0.0,
                "zero_write_ms": 500.0,
                "physical_stop_ms": 10.0,
            },
        },
        "host_shutdown": {
            "measured_ms": 60.0,
            "breakdown": {
                "event_to_agent_ms": 5.0,
                "zero_write_ms": 25.0,
                "physical_stop_ms": 30.0,
            },
        },
    },
}

for _motion_observation in SAMPLE_VALID_OBSERVATIONS["observed_movements"].values():
    _motion_observation["input_methods"] = {"button": True, "keyboard": True}

for _condition, _latency_observation in SAMPLE_VALID_OBSERVATIONS["latencies"].items():
    _latency_observation["physical_stop_observed"] = True
    _latency_observation["raw_evidence"] = f"instrument://session-001/{_condition}"


class TestMilestone15WebAcceptance(unittest.TestCase):
    """Test suite for Milestone 15 Web Acceptance Orchestrator."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.temp_dir, "dist")
        os.makedirs(self.report_dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Validation helper tests
    def test_duration_ms_validation(self) -> None:
        """Verify is_valid_duration_ms enforces finite, nonnegative numbers and rejects booleans."""
        self.assertTrue(is_valid_duration_ms(0))
        self.assertTrue(is_valid_duration_ms(0.0))
        self.assertTrue(is_valid_duration_ms(150.5))
        self.assertTrue(is_valid_duration_ms(300))

        # Must reject booleans
        self.assertFalse(is_valid_duration_ms(True))
        self.assertFalse(is_valid_duration_ms(False))

        # Must reject None and non-numeric
        self.assertFalse(is_valid_duration_ms(None))
        self.assertFalse(is_valid_duration_ms("150"))
        self.assertFalse(is_valid_duration_ms([100]))

        # Must reject negative numbers and infinities
        self.assertFalse(is_valid_duration_ms(-1.0))
        self.assertFalse(is_valid_duration_ms(float("inf")))
        self.assertFalse(is_valid_duration_ms(float("-inf")))
        self.assertFalse(is_valid_duration_ms(float("nan")))

    def test_exact_bool_validation(self) -> None:
        """Verify is_exact_bool_true strictly checks for True boolean."""
        self.assertTrue(is_exact_bool_true(True))
        self.assertFalse(is_exact_bool_true(False))
        self.assertFalse(is_exact_bool_true(1))
        self.assertFalse(is_exact_bool_true("true"))
        self.assertFalse(is_exact_bool_true("True"))
        self.assertFalse(is_exact_bool_true(None))

    # 2. Safety acknowledgment enforcement
    def test_preflight_fails_without_safety_acknowledgment(self) -> None:
        """Verify orchestrator rejects execution if --ack-tracks-raised is False."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=False,
            report_dir=self.report_dir,
            mock=True,
        )
        ok, errors = orch.run_preflight_checks()
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "Safety violation: --ack-tracks-raised was NOT provided" in e
                for e in errors
            )
        )

        success = orch.run_acceptance_suite()
        self.assertFalse(success)
        self.assertEqual(orch.overall_status, "FAILED")

    # 3. Platform detection and non-target handling
    def test_platform_check_on_dev_host(self) -> None:
        """Verify running on non-ARM64 / non-Ubuntu Pi 5 triggers proper detection."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            mock=False,
            require_target=False,
        )
        # On x86_64 dev machine, check_target_platform returns False
        if os.uname().machine != "aarch64":
            is_target, reason = orch.check_target_platform()
            self.assertFalse(is_target)
            self.assertIn("Architecture mismatch", reason)

            # Suite should run safely and conclude PENDING_TARGET_EXECUTION
            success = orch.run_acceptance_suite()
            self.assertTrue(success)
            self.assertEqual(orch.overall_status, "PENDING_TARGET_EXECUTION")

    def test_platform_check_enforces_require_target(self) -> None:
        """Verify --require-target causes failure if not running on Pi 5 target."""
        if os.uname().machine != "aarch64":
            orch = WebAcceptanceOrchestrator(
                ack_tracks_raised=True,
                report_dir=self.report_dir,
                mock=False,
                require_target=True,
            )
            success = orch.run_acceptance_suite()
            self.assertFalse(success)
            self.assertEqual(orch.overall_status, "FAILED")

    # 4. Physical motion observation validation
    def test_validate_physical_movement_observations(self) -> None:
        """Verify all 4 motion directions are required and validated."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        # Valid observations pass
        ok, errors = orch.validate_physical_movement_observations(
            SAMPLE_VALID_OBSERVATIONS["observed_movements"]
        )
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # Missing motion
        incomplete = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["observed_movements"])
        del incomplete["spin_right"]
        ok, errors = orch.validate_physical_movement_observations(incomplete)
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "Missing physical observation for required motion 'spin_right'" in e
                for e in errors
            )
        )

        # Direction mismatch
        bad_dir = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["observed_movements"])
        bad_dir["forward"]["direction_matched"] = False
        ok, errors = orch.validate_physical_movement_observations(bad_dir)
        self.assertFalse(ok)
        self.assertTrue(any("did not match commanded direction" in e for e in errors))

        # Failed burst stop
        no_stop = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["observed_movements"])
        no_stop["reverse"]["stopped_after_burst"] = False
        ok, errors = orch.validate_physical_movement_observations(no_stop)
        self.assertFalse(ok)
        self.assertTrue(any("did not come to a complete stop" in e for e in errors))

        # Missing observer
        no_obs = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["observed_movements"])
        no_obs["spin_left"]["observer"] = ""
        ok, errors = orch.validate_physical_movement_observations(no_obs)
        self.assertFalse(ok)
        self.assertTrue(any("lacks non-empty observer identity" in e for e in errors))

        missing_keyboard = copy.deepcopy(
            SAMPLE_VALID_OBSERVATIONS["observed_movements"]
        )
        missing_keyboard["forward"]["input_methods"]["keyboard"] = False
        ok, errors = orch.validate_physical_movement_observations(missing_keyboard)
        self.assertFalse(ok)
        self.assertTrue(any("not verified via keyboard" in e for e in errors))

    # 5. Web driving safety controls observation validation
    def test_validate_safety_control_observations(self) -> None:
        """Verify all required safety controls are validated."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        # Valid controls pass
        ok, errors = orch.validate_safety_control_observations(
            SAMPLE_VALID_OBSERVATIONS["safety_controls"]
        )
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # Missing space_stop control
        incomplete = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["safety_controls"])
        del incomplete["space_stop"]
        ok, errors = orch.validate_safety_control_observations(incomplete)
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "Missing observation for required safety control 'space_stop'" in e
                for e in errors
            )
        )

        # Control not verified
        unverified = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["safety_controls"])
        unverified["idle_timeout"]["verified"] = False
        ok, errors = orch.validate_safety_control_observations(unverified)
        self.assertFalse(ok)
        self.assertTrue(any("was not verified to halt motion" in e for e in errors))

        # Motors not disarmed
        not_disarmed = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["safety_controls"])
        not_disarmed["hold_cap"]["motion_disarmed"] = False
        ok, errors = orch.validate_safety_control_observations(not_disarmed)
        self.assertFalse(ok)
        self.assertTrue(
            any("did not leave motors in disarmed/stopped state" in e for e in errors)
        )

    def test_key_release_accepts_armed_idle_zero_motion(self) -> None:
        """Verify key_release allows truthful armed-idle state while requiring zero motion."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        # 1. Truthful browser release: direction neutral, ARMED_IDLE, motion_disarmed=False, verified=True
        controls = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["safety_controls"])
        controls["key_release"] = {
            "verified": True,
            "motion_stopped": True,
            "motion_disarmed": False,
            "observer": "owner",
        }
        ok, errors = orch.validate_safety_control_observations(controls)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # 2. Also valid without explicit motion_stopped field if verified=True
        controls["key_release"] = {
            "verified": True,
            "motion_disarmed": False,
            "observer": "owner",
        }
        ok, errors = orch.validate_safety_control_observations(controls)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # 3. If motion did not halt (verified=False or motion_stopped=False), validation must fail
        bad_stop = copy.deepcopy(controls)
        bad_stop["key_release"]["motion_stopped"] = False
        ok, errors = orch.validate_safety_control_observations(bad_stop)
        self.assertFalse(ok)
        self.assertTrue(any("did not physically halt motion" in e for e in errors))

        bad_verified = copy.deepcopy(controls)
        bad_verified["key_release"]["verified"] = False
        ok, errors = orch.validate_safety_control_observations(bad_verified)
        self.assertFalse(ok)
        self.assertTrue(any("was not verified to halt motion" in e for e in errors))

        # 4. Controls requiring disarm (space_stop, disarm, stop_controller, idle_timeout, hold_cap)
        # must continue to reject motion_disarmed=False
        for ctrl in (
            "space_stop",
            "disarm",
            "stop_controller",
            "idle_timeout",
            "hold_cap",
        ):
            bad_disarm = copy.deepcopy(controls)
            bad_disarm[ctrl]["motion_disarmed"] = False
            ok, errors = orch.validate_safety_control_observations(bad_disarm)
            self.assertFalse(ok)
            self.assertTrue(
                any(
                    f"Safety control '{ctrl}' did not leave motors in disarmed/stopped state"
                    in e
                    for e in errors
                )
            )

    # 6. PWA / mobile controls validation
    def test_validate_pwa_controls(self) -> None:
        """Verify mobile PWA interaction observations."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        ok, errors = orch.validate_pwa_control_observations(
            SAMPLE_VALID_OBSERVATIONS["pwa_controls"]
        )
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # Missing touch cancellation
        incomplete = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["pwa_controls"])
        del incomplete["screen_lock"]
        ok, errors = orch.validate_pwa_control_observations(incomplete)
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "Missing observation for required PWA interaction 'screen_lock'" in e
                for e in errors
            )
        )

        # Latched motion detected
        latched = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["pwa_controls"])
        latched["app_switching"]["no_latched_motion"] = False
        ok, errors = orch.validate_pwa_control_observations(latched)
        self.assertFalse(ok)
        self.assertTrue(any("did not prevent latched motion" in e for e in errors))

    # 7. Measured latency validation against Section 7 bounds
    def test_validate_latencies_against_bounds(self) -> None:
        """Verify all 8 web and 5 native failure modes are validated against bounds."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        ok, errors = orch.validate_web_latencies(SAMPLE_VALID_OBSERVATIONS["latencies"])
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # Bound exceeded for loss_of_focus (bound is 300 ms)
        exceeded = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        exceeded["loss_of_focus"]["measured_ms"] = 301.5
        ok, errors = orch.validate_web_latencies(exceeded)
        self.assertFalse(ok)
        self.assertTrue(any("exceeded bound (300.0 ms)" in e for e in errors))

        # Bound exceeded for bridge_crash (bound is 250 ms)
        exceeded_bridge = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        exceeded_bridge["bridge_crash"]["measured_ms"] = 251.0
        ok, errors = orch.validate_web_latencies(exceeded_bridge)
        self.assertFalse(ok)
        self.assertTrue(any("exceeded bound (250.0 ms)" in e for e in errors))

        # Missing failure mode
        missing_cond = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        del missing_cond["browser_crash"]
        ok, errors = orch.validate_web_latencies(missing_cond)
        self.assertFalse(ok)
        self.assertTrue(
            any(
                "Missing required latency measurement for failure condition: 'browser_crash'"
                in e
                for e in errors
            )
        )

        # Invalid timing breakdown field
        bad_breakdown = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        bad_breakdown["wifi_loss"]["breakdown"]["zero_write_ms"] = "invalid"
        ok, errors = orch.validate_web_latencies(bad_breakdown)
        self.assertFalse(ok)
        self.assertTrue(any("invalid or missing duration" in e for e in errors))

        missing_breakdown = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        del missing_breakdown["browser_crash"]["breakdown"]
        ok, errors = orch.validate_web_latencies(missing_breakdown)
        self.assertFalse(ok)
        self.assertTrue(any("all required phases" in e for e in errors))

        inconsistent = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS["latencies"])
        inconsistent["loss_of_focus"]["breakdown"]["physical_stop_ms"] = 1.0
        ok, errors = orch.validate_web_latencies(inconsistent)
        self.assertFalse(ok)
        self.assertTrue(any("does not reconcile" in e for e in errors))

    # 8. Complete physical observations validation
    def test_validate_physical_observations_complete(self) -> None:
        """Verify full physical observation payload validation."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True, report_dir=self.report_dir
        )

        ok, errors = orch.validate_physical_observations(SAMPLE_VALID_OBSERVATIONS)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

        # Missing metadata observer
        no_meta = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS)
        no_meta["metadata"]["physical_observer"] = ""
        ok, errors = orch.validate_physical_observations(no_meta)
        self.assertFalse(ok)
        self.assertTrue(
            any("missing non-empty 'physical_observer'" in e for e in errors)
        )

        # Missing metadata instruments
        no_inst = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS)
        del no_inst["metadata"]["instruments"]
        ok, errors = orch.validate_physical_observations(no_inst)
        self.assertFalse(ok)
        self.assertTrue(
            any("requires a non-empty 'instruments' list" in e for e in errors)
        )

    def test_interactive_prompt_collects_complete_evidence(self) -> None:
        """Verify interactive mode can produce every field required for acceptance."""
        responses = [
            "owner",
            "logic analyzer, high speed camera",
            "recording://session-002",
            "5 GHz LAN, injected disconnects",
            "Chrome 140",
            "Android 17 installed PWA",
            "iOS 20 installed PWA",
            "y",
        ]
        responses.extend(["y"] * (len(REQUIRED_MOTIONS) * 5))
        responses.extend(["y"] * (len(REQUIRED_SAFETY_CONTROLS) * 2))
        responses.extend(["y"] * (len(REQUIRED_PWA_CONTROLS) * 2))
        for condition in REQUIRED_FAILURE_CONDITIONS:
            if condition == "host_shutdown":
                responses.append("n")
            responses.extend(
                ["3", "1", "1", "1", "y", f"instrument://session-002/{condition}"]
            )

        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
        )
        with patch("builtins.input", side_effect=responses):
            observations = orch.prompt_physical_observations()

        ok, errors = orch.validate_physical_observations(observations)
        self.assertTrue(ok, errors)
        self.assertIn("screen_lock", observations["pwa_controls"])
        self.assertEqual(
            observations["metadata"]["client_versions"]["ios_pwa"],
            "iOS 20 installed PWA",
        )

    def test_shared_deployment_lock_blocks_exclusive_mutation(self) -> None:
        """Verify acceptance can read a root-style lock and retains it for its run."""
        lock_path = os.path.join(self.temp_dir, "deploy.lock")
        with open(lock_path, "w", encoding="utf-8"):
            pass
        os.chmod(lock_path, 0o444)
        orch = WebAcceptanceOrchestrator(lock_path=lock_path)

        with orch.deployment_read_lock():
            self.assertIsNotNone(orch._lock_fd)
            contender = os.open(lock_path, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)
        self.assertIsNone(orch._lock_fd)

    # 9. Mock safety invariant: Mocks NEVER certify ACCEPTED
    def test_mock_mode_safety_invariant(self) -> None:
        """Verify mock mode yields MOCK_VERIFICATION_ONLY and never ACCEPTED."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            mock=True,
            physical_observations=SAMPLE_VALID_OBSERVATIONS,
        )
        success = orch.run_acceptance_suite()
        self.assertTrue(success)
        self.assertEqual(orch.overall_status, "MOCK_VERIFICATION_ONLY")
        self.assertNotEqual(orch.overall_status, "ACCEPTED")

        # Rejection in mock mode if observations are invalid
        invalid_obs = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS)
        invalid_obs["safety_controls"]["space_stop"]["verified"] = False
        orch_bad = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            mock=True,
            physical_observations=invalid_obs,
        )
        success_bad = orch_bad.run_acceptance_suite()
        self.assertFalse(success_bad)
        self.assertEqual(orch_bad.overall_status, "FAILED")

    # 10. Physical target acceptance when on genuine target platform
    @patch.object(WebAcceptanceOrchestrator, "check_target_platform")
    def test_acceptance_on_genuine_target(self, mock_plat: MagicMock) -> None:
        """Require current interactive web evidence before target acceptance."""
        mock_plat.return_value = (
            True,
            "Authentic target platform: aarch64, ubuntu 26.04",
        )

        # Create a mock release link in temp_dir
        opt_dir = os.path.join(self.temp_dir, "opt")
        rel_dir = os.path.join(opt_dir, "release-test")
        os.makedirs(rel_dir, exist_ok=True)
        manifest_path = os.path.join(rel_dir, "release-manifest.txt")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("Release-Id: release-test-01\nTarget-Architecture: aarch64\n")
        current_link = os.path.join(opt_dir, "current")
        os.symlink(rel_dir, current_link)
        lock_path = os.path.join(self.temp_dir, "deploy.lock")
        with open(lock_path, "w", encoding="utf-8"):
            pass

        live_probe = {
            "base_url": "https://127.0.0.1:8443",
            "release_id": "release-test-01",
            "protocol_version": "1.0.0",
            "web_config_sha256": "abc123",
            "service_state": "active",
            "operator_state": "OWNED_DISARMED",
            "guard_armed": False,
            "linear_speed": 0.0,
            "angular_speed": 0.0,
            "battery_voltage": 12.2,
        }

        with (
            patch.object(
                WebAcceptanceOrchestrator, "managed_bridge_pid", return_value=1234
            ),
            patch.object(
                WebAcceptanceOrchestrator,
                "verify_usb_identity",
                return_value=(True, "verified"),
            ),
            patch.object(
                WebAcceptanceOrchestrator,
                "probe_web_runtime",
                return_value=live_probe,
            ),
            patch.object(
                WebAcceptanceOrchestrator,
                "request_safe_stop",
                return_value=(True, "stopped"),
            ),
            patch(
                "ubuntu_tank.scripts.web_acceptance.check_hardware_mutual_exclusion"
            ) as mock_mut,
        ):
            mock_mut.return_value = (True, [])

            # Without observations -> PENDING_PHYSICAL_ACCEPTANCE
            orch_pending = WebAcceptanceOrchestrator(
                ack_tracks_raised=True,
                report_dir=self.report_dir,
                opt_dir=opt_dir,
                lock_path=lock_path,
                mock=False,
            )
            orch_pending.run_acceptance_suite()
            self.assertEqual(orch_pending.overall_status, "PENDING_PHYSICAL_ACCEPTANCE")

            # A valid static payload is not current-run web evidence.
            orch_recorded = WebAcceptanceOrchestrator(
                ack_tracks_raised=True,
                report_dir=self.report_dir,
                opt_dir=opt_dir,
                lock_path=lock_path,
                mock=False,
                physical_observations=SAMPLE_VALID_OBSERVATIONS,
            )
            self.assertFalse(orch_recorded.run_acceptance_suite())
            self.assertEqual(
                orch_recorded.overall_status, "PENDING_PHYSICAL_ACCEPTANCE"
            )

            # A complete interactive session is bound to the active release.
            orch_accepted = WebAcceptanceOrchestrator(
                ack_tracks_raised=True,
                report_dir=self.report_dir,
                opt_dir=opt_dir,
                lock_path=lock_path,
                mock=False,
                interactive_observations=True,
            )
            with patch.object(
                orch_accepted,
                "prompt_physical_observations",
                return_value=copy.deepcopy(SAMPLE_VALID_OBSERVATIONS),
            ):
                success = orch_accepted.run_acceptance_suite()
            self.assertTrue(success)
            self.assertEqual(orch_accepted.overall_status, "ACCEPTED")
            self.assertEqual(orch_accepted.live_run_evidence["status"], "VERIFIED")
            self.assertEqual(
                orch_accepted.physical_observations["metadata"]["active_release_id"],
                "release-test-01",
            )
            self.assertEqual(mock_mut.call_args.kwargs["allowed_serial_pid"], 1234)

    def test_campaign_checkpoint_and_resume_across_reboot(self) -> None:
        """Verify campaign checkpoint saves before host shutdown and resumes across reboot."""
        opt_dir = os.path.join(self.temp_dir, "opt")
        rel_dir = os.path.join(opt_dir, "release-test")
        os.makedirs(rel_dir, exist_ok=True)
        manifest_path = os.path.join(rel_dir, "release-manifest.txt")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("Release-Id: release-test-01\nTarget-Architecture: aarch64\n")
        current_link = os.path.join(opt_dir, "current")
        os.symlink(rel_dir, current_link)

        lock_path = os.path.join(self.temp_dir, "deployment.lock")
        with open(lock_path, "w", encoding="utf-8"):
            pass
        campaign_path = os.path.join(self.report_dir, "campaign.json")

        # Partial observations missing only host_shutdown
        partial_obs = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS)
        del partial_obs["latencies"]["host_shutdown"]

        # Step 1: Pre-shutdown run saves campaign checkpoint
        orch_pre = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            opt_dir=opt_dir,
            lock_path=lock_path,
            mock=False,
            interactive_observations=True,
            campaign_file=campaign_path,
        )

        with (
            patch(
                "ubuntu_tank.scripts.web_acceptance.check_hardware_mutual_exclusion",
                return_value=(True, []),
            ),
            patch.object(
                orch_pre,
                "check_target_platform",
                return_value=(True, "Raspberry Pi 5"),
            ),
            patch.object(
                orch_pre, "verify_usb_identity", return_value=(True, "")
            ),
            patch.object(
                orch_pre,
                "probe_web_runtime",
                return_value={
                    "release_id": "release-test-01",
                    "web_config_sha256": "hash-001",
                    "service_state": "active",
                    "operator_state": "OWNED_DISARMED",
                    "guard_armed": False,
                    "commanded_motion": "neutral",
                    "linear_speed": 0.0,
                    "angular_speed": 0.0,
                    "battery_voltage_mv": 11500,
                    "motor_fault": False,
                },
            ),
            patch.object(
                orch_pre, "request_safe_stop", return_value=(True, "Stopped")
            ),
            patch.object(
                orch_pre,
                "managed_bridge_pid",
                return_value=1234,
            ),
            patch(
                "ubuntu_tank.scripts.web_acceptance.get_system_boot_id",
                return_value="boot-id-pre-shutdown",
            ),
            patch.object(
                orch_pre,
                "prompt_physical_observations",
                side_effect=lambda *args, **kwargs: (
                    orch_pre.save_campaign_checkpoint(
                        partial_obs, orch_pre._active_session_id
                    ),
                    {
                        "__checkpoint_saved__": True,
                        "campaign_file": campaign_path,
                    },
                )[1],
            ),
        ):
            saved = orch_pre.run_acceptance_suite()
            self.assertTrue(saved)
            self.assertEqual(orch_pre.overall_status, "CAMPAIGN_CHECKPOINT_SAVED")
            self.assertTrue(os.path.isfile(campaign_path))

        with open(campaign_path, "r", encoding="utf-8") as f:
            ckpt_data = json.load(f)
        self.assertEqual(ckpt_data["active_release_id"], "release-test-01")
        self.assertEqual(ckpt_data["boot_id"], "boot-id-pre-shutdown")
        self.assertIn("forward", ckpt_data["partial_observations"]["observed_movements"])
        self.assertNotIn("host_shutdown", ckpt_data["partial_observations"]["latencies"])

        # Step 2: Resuming with wrong release ID fails validation
        orch_resume_bad_release = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            opt_dir=opt_dir,
            lock_path=lock_path,
            mock=False,
            resume_campaign=campaign_path,
        )
        orch_resume_bad_release.environment_metadata["active_release_id"] = (
            "release-different"
        )
        with self.assertRaises(ValueError) as ctx:
            orch_resume_bad_release.load_and_validate_campaign(campaign_path)
        self.assertIn("does not match current system release", str(ctx.exception))

        # Step 3: Resuming without reboot (same boot_id) in live mode fails validation
        orch_resume_no_reboot = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            opt_dir=opt_dir,
            lock_path=lock_path,
            mock=False,
            resume_campaign=campaign_path,
        )
        orch_resume_no_reboot.environment_metadata["active_release_id"] = (
            "release-test-01"
        )
        with (
            patch(
                "ubuntu_tank.scripts.web_acceptance.get_system_boot_id",
                return_value="boot-id-pre-shutdown",
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            orch_resume_no_reboot.load_and_validate_campaign(campaign_path)
        self.assertIn("host has not rebooted", str(ctx.exception))

        # Step 4: Resuming with changed web config fails validation
        orch_resume_bad_config = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            opt_dir=opt_dir,
            lock_path=lock_path,
            mock=False,
            resume_campaign=campaign_path,
        )
        orch_resume_bad_config.environment_metadata["active_release_id"] = (
            "release-test-01"
        )
        with (
            patch(
                "ubuntu_tank.scripts.web_acceptance.get_system_boot_id",
                return_value="boot-id-post-reboot",
            ),
            patch.object(
                orch_resume_bad_config,
                "probe_web_runtime",
                return_value={
                    "release_id": "release-test-01",
                    "web_config_sha256": "different-hash",
                    "guard_armed": False,
                    "commanded_motion": "neutral",
                    "battery_voltage_mv": 11500,
                    "motor_fault": False,
                },
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            orch_resume_bad_config.load_and_validate_campaign(campaign_path)
        self.assertIn("Web configuration hash changed", str(ctx.exception))

        # Step 5: Successful resumption after authentic reboot with verified post-reboot stopped state
        orch_resume = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            opt_dir=opt_dir,
            lock_path=lock_path,
            mock=False,
            resume_campaign=campaign_path,
        )
        completed_obs = copy.deepcopy(SAMPLE_VALID_OBSERVATIONS)

        with (
            patch(
                "ubuntu_tank.scripts.web_acceptance.check_hardware_mutual_exclusion",
                return_value=(True, []),
            ),
            patch.object(
                orch_resume,
                "check_target_platform",
                return_value=(True, "Raspberry Pi 5"),
            ),
            patch.object(
                orch_resume, "verify_usb_identity", return_value=(True, "")
            ),
            patch.object(
                orch_resume,
                "probe_web_runtime",
                return_value={
                    "release_id": "release-test-01",
                    "web_config_sha256": "hash-001",
                    "service_state": "active",
                    "operator_state": "OWNED_DISARMED",
                    "guard_armed": False,
                    "commanded_motion": "neutral",
                    "linear_speed": 0.0,
                    "angular_speed": 0.0,
                    "battery_voltage_mv": 11500,
                    "motor_fault": False,
                },
            ),
            patch.object(
                orch_resume, "request_safe_stop", return_value=(True, "Stopped")
            ),
            patch.object(
                orch_resume,
                "managed_bridge_pid",
                return_value=1234,
            ),
            patch(
                "ubuntu_tank.scripts.web_acceptance.get_system_boot_id",
                return_value="boot-id-post-reboot",
            ),
            patch.object(
                orch_resume,
                "prompt_physical_observations",
                return_value=completed_obs,
            ) as mock_prompt,
        ):
            success = orch_resume.run_acceptance_suite()
            self.assertTrue(success)
            self.assertEqual(orch_resume.overall_status, "ACCEPTED")
            # Verify that prompt_physical_observations was passed the partial_existing observations from checkpoint
            mock_prompt.assert_called_once()
            self.assertEqual(
                mock_prompt.call_args.kwargs["partial_existing"]["metadata"][
                    "physical_observer"
                ],
                "owner",
            )
            self.assertEqual(
                orch_resume.live_run_evidence["campaign_id"],
                ckpt_data["campaign_id"],
            )

    # 11. Structured Report Generation
    def test_json_and_markdown_report_generation(self) -> None:
        """Verify generated JSON and Markdown reports conform to design requirements."""
        orch = WebAcceptanceOrchestrator(
            ack_tracks_raised=True,
            report_dir=self.report_dir,
            mock=True,
            physical_observations=SAMPLE_VALID_OBSERVATIONS,
        )
        orch.run_acceptance_suite()

        json_path = os.path.join(
            self.report_dir, "web-acceptance-report-milestone15.json"
        )
        md_path = os.path.join(self.report_dir, "web-acceptance-report-milestone15.md")

        self.assertTrue(os.path.isfile(json_path))
        self.assertTrue(os.path.isfile(md_path))

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["report_version"], "1.0")
        self.assertEqual(data["milestone"], "15")
        self.assertEqual(data["status"], "MOCK_VERIFICATION_ONLY")
        self.assertTrue(data["safety_acknowledgment"]["ack_tracks_raised"])
        self.assertFalse(data["safety_acknowledgment"]["on_ground_motion_authorized"])
        self.assertEqual(
            len(data["accepted_bounds_ms"]), len(ACCEPTED_WEB_LATENCY_BOUNDS_MS)
        )

        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()

        self.assertIn(
            "Milestone 15: Raised-Track Web Movement & Failure Acceptance Report",
            md_text,
        )
        self.assertIn("Chassis Elevated", md_text)
        self.assertIn("STRICTLY FORBIDDEN", md_text)
        self.assertIn("Web Motion Acceptance (All 4 Directions)", md_text)
        self.assertIn("Web Safety Controls", md_text)
        self.assertIn("Mobile PWA Interaction Acceptance", md_text)
        self.assertIn("Instrumented Failure Stop Latencies", md_text)
        self.assertIn("Physical Acceptance Conclusion", md_text)
        self.assertIn("MOCK_VERIFICATION_ONLY", md_text)

    # 12. CLI entrypoint execution
    def test_cli_requires_ack_tracks_raised(self) -> None:
        """Verify CLI main() returns exit code 1 if --ack-tracks-raised is not passed."""
        with patch.object(
            sys,
            "argv",
            [
                "web_acceptance.py",
                f"--report-dir={self.report_dir}",
            ],
        ):
            code = main()
            self.assertEqual(code, 1)

    def test_cli_with_inline_json_observations(self) -> None:
        """Verify CLI main() parses inline JSON physical observations."""
        obs_json = json.dumps(SAMPLE_VALID_OBSERVATIONS)
        with patch.object(
            sys,
            "argv",
            [
                "web_acceptance.py",
                "--ack-tracks-raised",
                "--mock",
                f"--report-dir={self.report_dir}",
                f"--physical-observations={obs_json}",
            ],
        ):
            code = main()
            self.assertEqual(code, 0)

    def test_cli_with_file_json_observations(self) -> None:
        """Verify CLI main() parses physical observations from file path."""
        obs_file = os.path.join(self.temp_dir, "observations.json")
        with open(obs_file, "w", encoding="utf-8") as f:
            json.dump(SAMPLE_VALID_OBSERVATIONS, f)

        with patch.object(
            sys,
            "argv",
            [
                "web_acceptance.py",
                "--ack-tracks-raised",
                "--mock",
                f"--report-dir={self.report_dir}",
                f"--physical-observations={obs_file}",
            ],
        ):
            code = main()
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
