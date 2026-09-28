"""
test_milestone14_installed_integration.py - Test suite for Milestone 14:
Installed Pi integration, packaging closure, systemd confinement, CLI/browser exclusion,
and protocol compatibility handling.

Verifies:
1. Packaging closure and entrypoint execution:
   - Launchers (mentorpi-tank-web, mentorpi-tank-lifecycle) resolve release packages without PYTHONPATH.
2. Production systemd confinement:
   - Units pass systemd-analyze verify.
   - mentorpi-tank-web.service: non-root user (ubuntu-tank-web), ProtectSystem=strict, DevicePolicy=closed, NoNewPrivileges=yes.
   - mentorpi-tank-lifecycle.service: root user, ProtectSystem=strict, RestrictAddressFamilies=AF_UNIX.
3. Web availability while controller is stopped:
   - Web API starts and serves status and logs even when mentorpi-tank.service is inactive.
   - Lifecycle operations (start/stop) coordinate cleanly with lifecycle helper.
   - Service restart leaves ownership empty and motion disarmed.
4. CLI and browser mutual exclusion:
   - Only one operator can hold active control authority.
   - When web owns control, CLI acquisition is rejected with ALREADY_OWNED.
   - Space/emergency stop by any client halts motion, invalidates control epoch, and forces disarm.
5. Cached client recovery and protocol compatibility:
   - Cached clients querying an incompatible protocol or offline server fail closed and cannot arm or queue motion.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
WEB_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_web")
OPERATOR_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_operator")

for p in [
    os.path.join(os.path.dirname(OPERATOR_PKG_DIR), "ubuntu_tank_supervisor"),
    os.path.join(os.path.dirname(OPERATOR_PKG_DIR), "ubuntu_tank_protocol"),
    REPO_ROOT,
    UBUNTU_TANK_DIR,
    WEB_PKG_DIR,
    OPERATOR_PKG_DIR,
]:
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.enums import (
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_protocol.schemas import TelemetrySnapshot
from ubuntu_tank_web.app import create_app
from ubuntu_tank_web.lifecycle_client import LifecycleClient
from ubuntu_tank_web.operator_relay import OperatorRelay

from ubuntu_tank.scripts.deployment_manager import compute_tree_sha256


class TestPackagingClosure(unittest.TestCase):
    """Test packaging entrypoints resolve release packages without ambient PYTHONPATH."""

    def test_launchers_import_release_packages_without_pythonpath(self):
        """Packaged entrypoints execute cleanly without ambient PYTHONPATH."""
        with tempfile.TemporaryDirectory(prefix="ubuntu_tank_test_launchers_") as td:
            clean_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": td,
            }

            # 1. Test bin/mentorpi-tank-web --help
            web_launcher = os.path.join(UBUNTU_TANK_DIR, "bin", "mentorpi-tank-web")
            res_web = subprocess.run(
                [sys.executable, web_launcher, "--help"],
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                res_web.returncode,
                0,
                f"mentorpi-tank-web launcher failed with clean env: {res_web.stderr}",
            )
            self.assertIn("MentorPi Tank Web Control Service", res_web.stdout)

            # 2. Test bin/mentorpi-tank-lifecycle --help
            lifecycle_launcher = os.path.join(
                UBUNTU_TANK_DIR, "bin", "mentorpi-tank-lifecycle"
            )
            res_lc = subprocess.run(
                [sys.executable, lifecycle_launcher, "--help"],
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                res_lc.returncode,
                0,
                f"mentorpi-tank-lifecycle launcher failed with clean env: {res_lc.stderr}",
            )
            self.assertIn("MentorPi Tank Restricted Lifecycle Helper", res_lc.stdout)


class TestLauncherSourceImmutability(unittest.TestCase):
    """Help/import probes must not modify writable application source trees."""

    def test_repeated_clean_environment_help_preserves_source_hash(self):
        """Run actual launchers without cached bytecode or inherited -B settings."""
        with tempfile.TemporaryDirectory(prefix="mentorpi-launcher-source-") as root:
            os.makedirs(os.path.join(root, "bin"))
            for package in (
                "ubuntu_tank_web",
                "ubuntu_tank_protocol",
                "ubuntu_tank_supervisor",
            ):
                shutil.copytree(
                    os.path.join(UBUNTU_TANK_DIR, "src", package),
                    os.path.join(root, "src", package),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for name in ("mentorpi-tank-web", "mentorpi-tank-lifecycle"):
                launcher = os.path.join(root, "bin", name)
                shutil.copy2(os.path.join(UBUNTU_TANK_DIR, "bin", name), launcher)
                before = compute_tree_sha256(os.path.join(root, "src"))
                for _ in range(2):
                    result = subprocess.run(
                        [sys.executable, launcher, "--help"],
                        env={"PATH": "/usr/bin:/bin"},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        compute_tree_sha256(os.path.join(root, "src")), before
                    )


class TestProductionSystemdConfinement(unittest.TestCase):
    """Test production systemd confinement and sandboxing directives."""

    def setUp(self):
        self.host_dir = os.path.join(UBUNTU_TANK_DIR, "host")

    def test_systemd_analyze_verify_all_units(self):
        """All unit files in host/ pass systemd-analyze verify."""
        units = [
            "mentorpi-tank-web.service",
            "mentorpi-tank-lifecycle.service",
            "mentorpi-tank-operator.service",
            "mentorpi-tank.service",
            "mentorpi-tank-stack.target",
        ]
        unit_paths = [os.path.join(self.host_dir, u) for u in units]

        cmd = ["systemd-analyze", "verify"] + unit_paths
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)

        # Filter out informational / environmental warnings on non-systemd chroots
        errors = []
        for line in (res.stderr or "").splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            # Ignore missing user accounts when running as non-root test
            if "Unknown user" in line_s or "Unknown group" in line_s:
                continue
            if "Failed to parse" in line_s or "error" in line_s.lower():
                errors.append(line_s)

        self.assertEqual(errors, [], f"systemd-analyze reported errors: {errors}")

    def test_systemd_confinement_directives(self):
        """Assert strict sandboxing properties on web and lifecycle units."""
        # Check mentorpi-tank-web.service
        web_unit = os.path.join(self.host_dir, "mentorpi-tank-web.service")
        with open(web_unit, "r", encoding="utf-8") as f:
            web_text = f.read()

        self.assertIn("User=ubuntu-tank-web", web_text)
        self.assertIn("ProtectSystem=strict", web_text)
        self.assertIn("DevicePolicy=closed", web_text)
        self.assertIn("NoNewPrivileges=yes", web_text)
        self.assertIn("CapabilityBoundingSet=", web_text)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", web_text)
        self.assertIn("ReadOnlyPaths=/opt/ubuntu_tank /etc/opt/ubuntu_tank", web_text)

        # Check mentorpi-tank-lifecycle.service
        lc_unit = os.path.join(self.host_dir, "mentorpi-tank-lifecycle.service")
        with open(lc_unit, "r", encoding="utf-8") as f:
            lc_text = f.read()

        self.assertIn("User=root", lc_text)
        self.assertIn("ProtectSystem=strict", lc_text)
        self.assertIn("NoNewPrivileges=yes", lc_text)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", lc_text)
        self.assertIn("ReadOnlyPaths=/opt/ubuntu_tank /etc/opt/ubuntu_tank", lc_text)


class TestLifecycleAndWebAvailability(unittest.TestCase):
    """Test web availability and lifecycle coordination when controller is stopped."""

    def setUp(self):
        self.sm = OperatorStateMachine()
        self.relay = OperatorRelay(socket_path="/tmp/nonexistent_op.sock")
        self.lifecycle = LifecycleClient(socket_path="/tmp/nonexistent_lc.sock")

        # Mock operator relay to use our state machine directly
        self.relay._owner_client = MagicMock()
        self.relay._owner_client.arm.side_effect = lambda **kw: (
            self.sm.request_arm(kw.get("epoch", 1), kw.get("tracks_raised", False))[0],
            None,
            None,
        )
        self.relay._owner_client.stop.side_effect = lambda **kw: (
            self.sm.request_stop(kw.get("epoch", 1))[0],
            None,
        )

        self.app = create_app(
            config=WebControlConfig(allowed_origins=["https://127.0.0.1:8443"]),
            relay=self.relay,
            lifecycle=self.lifecycle,
        )
        self.client = TestClient(self.app)

    def test_web_service_available_when_controller_stopped(self):
        """Web service returns 200 and reports inactive when controller service is stopped."""
        with (
            patch.object(
                self.lifecycle,
                "get_status",
                return_value=(True, "inactive", "0"),
            ),
            patch.object(
                self.relay,
                "get_status",
                return_value={
                    "service_state": "inactive",
                    "operator_state": "NO_OWNER",
                    "guard_armed": False,
                },
            ),
        ):
            res = self.client.get("/api/v1/status")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["service_state"], "inactive")
            self.assertEqual(data["operator_state"], "NO_OWNER")
            self.assertFalse(data["guard_armed"])

    def test_public_start_removed(self):
        """Only combined Take control may initiate startup through the public API."""
        with patch.object(self.lifecycle, "start_controller") as start:
            res = self.client.post(
                "/api/v1/controller/start",
                json={"request_id": "obsolete"},
                headers={"Origin": "https://127.0.0.1:8443"},
            )
            self.assertIn(res.status_code, (404, 405))
            start.assert_not_called()

    def test_service_restart_leaves_ownership_empty_and_disarmed(self):
        """Restarting leaves ownership empty (NO_OWNER) and motion disarmed."""
        fresh_sm = OperatorStateMachine()
        self.assertEqual(fresh_sm.state, OperatorState.NO_OWNER)
        self.assertIsNone(fresh_sm.owner_id)
        self.assertEqual(fresh_sm.active_direction, MotionDirection.NEUTRAL)
        self.assertFalse(fresh_sm.telemetry.guard_armed)


class TestCliAndBrowserMutualExclusion(unittest.TestCase):
    """Test that only one client holds control authority across CLI and browser."""

    def setUp(self):
        self.sm = OperatorStateMachine()
        now = time.monotonic_ns()
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=now,
            guard_armed=False,
            guard_monotonic_ns=now,
        )

    def test_cli_and_browser_mutual_exclusion(self):
        """When browser holds ownership, CLI acquisition is rejected with DEPLOYMENT_BUSY."""
        # 1. Browser acquires ownership
        ok, epoch, _err, _msg = self.sm.acquire(
            "browser-session-1", time.monotonic_ns()
        )
        self.assertTrue(ok)
        self.assertEqual(self.sm.owner_id, "browser-session-1")
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)

        # 2. CLI teleop attempts to acquire ownership while browser holds it
        cli_ok, _cli_epoch, cli_err, cli_msg = self.sm.acquire(
            "cli-teleop", time.monotonic_ns()
        )
        self.assertFalse(cli_ok)
        self.assertEqual(cli_err, WebControlErrorCode.DEPLOYMENT_BUSY)
        self.assertIn("already held", cli_msg)
        self.assertEqual(self.sm.owner_id, "browser-session-1")

        # 3. Arm chassis by browser owner
        arm_ok, _arm_err, _arm_msg = self.sm.arm(
            owner_id="browser-session-1",
            epoch=epoch,
            current_monotonic_ns=time.monotonic_ns(),
            request_id="arm-req-1",
        )
        self.assertTrue(arm_ok)
        self.assertEqual(self.sm.state, OperatorState.ARMING)

        confirm_ok, _confirm_err, _confirm_msg = self.sm.confirm_armed(
            guard_confirmed=True,
            downstream_zero_confirmed=True,
            current_monotonic_ns=time.monotonic_ns(),
            epoch=epoch,
            request_id="arm-req-1",
        )
        self.assertTrue(confirm_ok)
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # 4. Emergency Stop by any client (e.g. CLI or separate observer) halts motion
        self.sm.stop(time.monotonic_ns())
        self.assertTrue(self.sm.disarm_pending)

        # Downstream guard confirms disarm
        self.sm.update_guard_telemetry(False, time.monotonic_ns())
        self.assertFalse(self.sm.telemetry.guard_armed)
        self.assertEqual(self.sm.state, OperatorState.OWNED_DISARMED)


class TestCachedClientRecoveryAndIncompatibleProtocol(unittest.TestCase):
    """Test cached client recovery and protocol compatibility handling."""

    def test_cached_client_cannot_arm_on_incompatible_server(self):
        """Cached client with mismatched protocol or epoch cannot arm or queue movement."""
        relay = OperatorRelay(socket_path="/tmp/nonexistent.sock")
        lifecycle = LifecycleClient(socket_path="/tmp/nonexistent_lc.sock")

        app = create_app(
            config=WebControlConfig(allowed_origins=["https://127.0.0.1:8443"]),
            relay=relay,
            lifecycle=lifecycle,
        )
        client = TestClient(app)

        # Arm attempt without valid ownership must fail closed
        res = client.post(
            "/api/v1/control/arm",
            json={"epoch": 999, "request_id": "stale-arm"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"], WebControlErrorCode.NOT_OWNER.value)

    def test_version_endpoint_reports_protocol_version(self):
        """Version endpoint provides authoritative protocol_version for client check."""
        app = create_app(
            config=WebControlConfig(allowed_origins=["https://127.0.0.1:8443"])
        )
        client = TestClient(app)

        res = client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("protocol_version", data)
        self.assertEqual(data["protocol_version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
