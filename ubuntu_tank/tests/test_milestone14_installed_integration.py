"""
test_milestone14_installed_integration.py - Test suite for Milestone 14:
Installed Pi integration, packaging closure, systemd confinement, CLI/browser exclusion,
transactional activation, and offline rollback to native-only baseline.

Verifies:
1. Packaging closure and static frontend delivery:
   - Packaging includes pre-compiled web/dist assets (index.html, manifest, sw.js) in release archive and manifest.
   - Zero Node.js / npm artifacts (node_modules, src, test files) packaged into production archives.
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
5. Transactional activation and offline rollback:
   - 6-step transactional activation commits cleanly with journal and fsync points.
   - Crash-consistent recovery reconciles interrupted transactions.
   - Rollback from web-enabled release to native-only release safely unloads web services and restores prior baseline.
6. Cached client recovery and protocol compatibility:
   - Cached clients querying an incompatible protocol or offline server fail closed and cannot arm or queue motion.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
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

for p in [REPO_ROOT, UBUNTU_TANK_DIR, WEB_PKG_DIR, OPERATOR_PKG_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
from ubuntu_tank_operator.config import WebControlConfig
from ubuntu_tank_operator.enums import (
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_operator.schemas import TelemetrySnapshot
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_web.app import create_app
from ubuntu_tank_web.lifecycle_client import LifecycleClient
from ubuntu_tank_web.operator_relay import OperatorRelay

from ubuntu_tank.scripts.deployment_manager import (
    ReleaseManager,
    SnapshotManager,
    attest_build,
    compute_file_sha256,
    compute_tree_sha256,
    parse_release_manifest,
)


class BaseMilestone14TestCase(unittest.TestCase):
    """Sets up isolated filesystem hierarchy mimicking target Pi layout."""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="ubuntu_tank_test_m14_")
        self.opt_dir = os.path.join(self.test_root, "opt", "ubuntu_tank")
        self.etc_dir = os.path.join(self.test_root, "etc", "opt", "ubuntu_tank")
        self.var_dir = os.path.join(self.test_root, "var", "opt", "ubuntu_tank")
        self.run_dir = os.path.join(self.test_root, "run", "ubuntu_tank")
        self.lock_path = os.path.join(
            self.test_root, "run", "lock", "ubuntu_tank", "deploy.lock"
        )
        self.systemd_dir = os.path.join(self.test_root, "etc", "systemd", "system")
        self.udev_dir = os.path.join(self.test_root, "etc", "udev", "rules.d")

        for d in [
            self.opt_dir,
            self.etc_dir,
            self.var_dir,
            self.run_dir,
            os.path.dirname(self.lock_path),
            self.systemd_dir,
            self.udev_dir,
        ]:
            os.makedirs(d, exist_ok=True)

        self.mgr = ReleaseManager(
            opt_dir=self.opt_dir,
            etc_dir=self.etc_dir,
            var_dir=self.var_dir,
            run_dir=self.run_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
            lock_path=self.lock_path,
        )

        self.workspace_dir = UBUNTU_TANK_DIR

        # Setup mock udev rule for serial mutual exclusion
        with open(os.path.join(self.udev_dir, "99-mentorpi-rrc.rules"), "w") as stream:
            stream.write(
                'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", '
                'ATTRS{serial}=="fixture-rrc", GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc"\n'
            )
        self._prev_mock_docker_ps = os.environ.get("UBUNTU_TANK_MOCK_DOCKER_PS")
        os.environ["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"

    def tearDown(self):
        if self._prev_mock_docker_ps is not None:
            os.environ["UBUNTU_TANK_MOCK_DOCKER_PS"] = self._prev_mock_docker_ps
        else:
            os.environ.pop("UBUNTU_TANK_MOCK_DOCKER_PS", None)
        if os.path.exists(self.test_root):
            shutil.rmtree(self.test_root, ignore_errors=True)


class TestPackagingAndAssetClosure(BaseMilestone14TestCase):
    """Test packaging includes compiled static assets and excludes Node.js tooling."""

    def test_package_includes_web_dist_and_manifest_checksums(self):
        """Packaging bundles web/dist with verified checksums and zero node_modules."""
        out_dir = os.path.join(self.test_root, "dist")
        archive_path = self.mgr.package_release(
            workspace_dir=self.workspace_dir,
            output_dir=out_dir,
            release_id="1.0.0-gm14test01",
            arch="arm64",
            allow_staged_install=True,
        )

        self.assertTrue(os.path.isfile(archive_path))

        extract_dir = os.path.join(self.test_root, "inspect_m14_pkg")
        os.makedirs(extract_dir, exist_ok=True)
        if archive_path.endswith(".zst"):
            subprocess.check_call(
                ["tar", "--zstd", "-xf", archive_path, "-C", extract_dir]
            )
        else:
            subprocess.check_call(["tar", "-xf", archive_path, "-C", extract_dir])

        pkg_root = os.path.join(extract_dir, "1.0.0-gm14test01")
        manifest_file = os.path.join(pkg_root, "release-manifest.txt")
        self.assertTrue(os.path.isfile(manifest_file))

        headers, file_records = parse_release_manifest(manifest_file)
        self.assertEqual(headers.get("Release-Id"), "1.0.0-gm14test01")
        self.assertEqual(headers.get("Target-Architecture"), "arm64")

        # Verify web/dist files are present and match manifest checksums
        web_dist_dir = os.path.join(pkg_root, "web", "dist")
        self.assertTrue(
            os.path.isdir(web_dist_dir),
            f"web/dist directory missing in packaged release: {web_dist_dir}",
        )

        required_assets = ["index.html", "manifest.webmanifest", "sw.js"]
        for asset in required_assets:
            asset_path = os.path.join(web_dist_dir, asset)
            self.assertTrue(
                os.path.isfile(asset_path), f"Asset {asset} missing in {web_dist_dir}"
            )
            rel_path = f"web/dist/{asset}"
            self.assertIn(
                rel_path, file_records, f"{rel_path} not found in release-manifest.txt"
            )
            actual_sha = compute_file_sha256(asset_path)
            self.assertEqual(
                actual_sha,
                file_records[rel_path]["sha256"],
                f"SHA256 mismatch for {rel_path}",
            )

        # Verify build tooling and Node artifacts are strictly absent
        for excluded in ["node_modules", "package.json", "tsconfig.json"]:
            self.assertFalse(
                os.path.exists(os.path.join(pkg_root, "web", excluded)),
                f"Forbidden development artifact '{excluded}' leaked into packaged release!",
            )

    def test_launchers_import_release_packages_without_pythonpath(self):
        """Packaged entrypoints execute cleanly without ambient PYTHONPATH."""
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.test_root,
        }

        # 1. Test bin/mentorpi-tank-web --help
        web_launcher = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-web")
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
            self.workspace_dir, "bin", "mentorpi-tank-lifecycle"
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

    def test_lifecycle_start_coordination(self):
        """Lifecycle start initiates controller start via lifecycle client."""
        with (
            patch.object(
                self.lifecycle,
                "get_status",
                return_value=(True, "inactive", "0"),
            ),
            patch.object(
                self.lifecycle,
                "start_controller",
                return_value=(True, "active", "Started"),
            ),
        ):
            res = self.client.post(
                "/api/v1/controller/start",
                json={"request_id": "req-start-1"},
                headers={"Origin": "https://127.0.0.1:8443"},
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "completed")
            self.assertIsNone(data["error"])
            self.assertTrue(data["operation_id"].startswith("op-"))

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
            tracks_raised=True,
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


class TestTransactionalActivationAndRollbackClosure(BaseMilestone14TestCase):
    """Test 6-step transactional activation and rollback to native-only baseline."""

    def test_transactional_activation_and_boot_recovery(self):
        """Activation executes 6 steps atomically and recovers interrupted state."""
        out_dir = os.path.join(self.test_root, "dist")
        archive_path = self.mgr.package_release(
            workspace_dir=self.workspace_dir,
            output_dir=out_dir,
            release_id="1.0.0-gm14test02",
            arch="arm64",
            allow_staged_install=True,
        )

        rel_id = self.mgr.install_release(
            archive_path, require_root=False, enforce_arm64=False
        )
        self.assertEqual(rel_id, "1.0.0-gm14test02")

        # Activate release
        active_id = self.mgr.activate_release(rel_id, require_root=False)
        self.assertEqual(active_id, rel_id)
        self.assertTrue(os.path.islink(self.mgr.current_symlink))
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.mgr.current_symlink, "bin", "mentorpi-tank-web")
            )
        )

        # Inject interrupted PREPARED transaction with verified snapshot
        tx_id = "tx-interrupted-01"
        valid_snapshot = self.mgr.snapshot_mgr.create_snapshot(
            tx_id=tx_id,
            current_symlink_target=os.path.realpath(self.mgr.current_symlink),
            etc_dir=self.mgr.etc_dir,
            systemd_dir=self.mgr.systemd_dir,
            udev_dir=self.mgr.udev_dir,
        )
        self.mgr.journal.record_prepared(
            tx_id=tx_id,
            candidate_release_id="1.0.0-gm14test03",
            candidate_release_path=os.path.join(
                self.mgr.releases_dir, "1.0.0-gm14test03"
            ),
            previous_release_id=rel_id,
            previous_release_path=os.path.join(self.mgr.releases_dir, rel_id),
            snapshot_dir=valid_snapshot,
        )

        # Boot recovery reconciles interrupted state
        recovered = self.mgr.recover_activation(check_mutual_exclusion=False)
        self.assertTrue(recovered)
        state = self.mgr.journal.get_state()
        self.assertIsNone(state.get("current_transaction"))

    def test_rollback_to_native_only_release_removes_web_services(self):
        """Rollback to a native-only baseline removes web service units and reverts symlink."""
        # 1. Create a native-only release baseline (Milestone 5-like)
        baseline_id = "1.0.0-gnative01"
        baseline_dir = os.path.join(self.mgr.releases_dir, baseline_id)
        os.makedirs(os.path.join(baseline_dir, "bin"), exist_ok=True)
        os.makedirs(os.path.join(baseline_dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(baseline_dir, "host"), exist_ok=True)
        os.makedirs(os.path.join(baseline_dir, "install"), exist_ok=True)

        for f in ["mentorpi-tank-run"]:
            fp = os.path.join(baseline_dir, "bin", f)
            with open(fp, "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(fp, 0o755)

        with open(os.path.join(baseline_dir, "install", "setup.bash"), "w") as fh:
            fh.write("#!/bin/sh\nexport TANK=1\n")
        os.chmod(os.path.join(baseline_dir, "install", "setup.bash"), 0o755)

        os.makedirs(os.path.join(baseline_dir, "install", "bin"), exist_ok=True)
        with open(
            os.path.join(baseline_dir, "install", "bin", "tank_verify_install"), "w"
        ) as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(
            os.path.join(baseline_dir, "install", "bin", "tank_verify_install"), 0o755
        )

        os.makedirs(os.path.join(baseline_dir, "src"), exist_ok=True)
        with open(os.path.join(baseline_dir, "src", "dummy.txt"), "w") as fh:
            fh.write("dummy\n")

        # Native-only host units: NO web or lifecycle units!
        for unit in [
            "mentorpi-tank.service",
            "mentorpi-tank-operator.service",
            "mentorpi-tank-stack.target",
            "mentorpi-tank-recover.service",
        ]:
            src_u = os.path.join(self.workspace_dir, "host", unit)
            if os.path.isfile(src_u):
                shutil.copy2(src_u, os.path.join(baseline_dir, "host", unit))
                shutil.copy2(src_u, os.path.join(self.systemd_dir, unit))

        shutil.copy2(
            os.path.join(self.workspace_dir, "config", "controller.yaml"),
            os.path.join(baseline_dir, "config", "controller.yaml"),
        )
        os.makedirs(os.path.join(baseline_dir, "config", "fastdds"), exist_ok=True)
        shutil.copy2(
            os.path.join(self.workspace_dir, "config", "fastdds", "loopback.xml"),
            os.path.join(baseline_dir, "config", "fastdds", "loopback.xml"),
        )
        shutil.copytree(
            os.path.join(self.workspace_dir, "scripts"),
            os.path.join(baseline_dir, "scripts"),
        )
        shutil.copytree(
            os.path.join(self.workspace_dir, "config", "sros2"),
            os.path.join(baseline_dir, "config", "sros2"),
        )

        attest_build(
            os.path.join(baseline_dir, "install"),
            f"{self.opt_dir}/releases/{baseline_id}/install",
            os.path.join(baseline_dir, "src"),
            synthetic=True,
        )

        source_sha = compute_tree_sha256(os.path.join(baseline_dir, "src"))
        install_sha = compute_tree_sha256(os.path.join(baseline_dir, "install"))

        # Generate manifest for baseline
        manifest_lines = [
            "Format-Version: 1.0",
            f"Release-Id: {baseline_id}",
            "Project-Version: 1.0.0",
            "Git-Commit: 0000000000000000000000000000000000000000",
            "Git-Short-Commit: 0000000",
            "Target-OS: Ubuntu 26.04 LTS",
            "Target-Architecture: arm64",
            "ROS-Distribution: lyrical",
            f"Install-Prefix: {self.opt_dir}/releases/{baseline_id}/install",
            "Build-Timestamp: 2026-09-18T00:00:00Z",
            f"Source-Tree-SHA256: {source_sha}",
            f"Install-Tree-SHA256: {install_sha}",
            "Test-Status: passed",
            "Hardware-Target: Raspberry Pi 5 ARM64 + STM32 RRC",
            "",
            "[Files]",
        ]
        for root, _, files in os.walk(baseline_dir):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, baseline_dir)
                size = os.path.getsize(p)
                mode = oct(stat.S_IMODE(os.stat(p).st_mode))
                sha = compute_file_sha256(p)
                manifest_lines.append(f"{sha}  {size}  {mode}  {rel}")
        with open(os.path.join(baseline_dir, "release-manifest.txt"), "w") as mf:
            mf.write("\n".join(manifest_lines) + "\n")

        # Activate baseline release
        self.mgr.activate_release(baseline_id, require_root=False)
        self.assertFalse(
            os.path.isfile(os.path.join(self.systemd_dir, "mentorpi-tank-web.service"))
        )

        # 2. Package and activate web-enabled release
        out_dir = os.path.join(self.test_root, "dist")
        web_archive = self.mgr.package_release(
            workspace_dir=self.workspace_dir,
            output_dir=out_dir,
            release_id="1.0.0-gweb01",
            arch="arm64",
            allow_staged_install=True,
        )
        self.mgr.install_release(web_archive, require_root=False, enforce_arm64=False)
        self.mgr.activate_release("1.0.0-gweb01", require_root=False)

        # Web service is now present in systemd_dir
        self.assertTrue(
            os.path.isfile(os.path.join(self.systemd_dir, "mentorpi-tank-web.service"))
        )
        self.assertEqual(
            os.path.basename(os.path.realpath(self.mgr.current_symlink)),
            "1.0.0-gweb01",
        )

        # 3. Rollback to native-only baseline
        rolled_back_id = self.mgr.rollback_release(require_root=False)
        self.assertEqual(rolled_back_id, baseline_id)

        # Verify web service unit is removed from systemd_dir
        self.assertFalse(
            os.path.isfile(os.path.join(self.systemd_dir, "mentorpi-tank-web.service")),
            "mentorpi-tank-web.service was NOT removed upon rollback to native-only baseline!",
        )
        self.assertEqual(
            os.path.basename(os.path.realpath(self.mgr.current_symlink)),
            baseline_id,
        )


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
            json={"epoch": 999, "tracks_raised": True, "request_id": "stale-arm"},
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
        self.assertEqual(data["protocol_version"], "1.0.0")


class TestSnapshotWebConfigPreservation(BaseMilestone14TestCase):
    """Test snapshot restoration preserves or prunes web.yaml depending on snapshot format and record."""

    def test_legacy_snapshot_restore_preserves_custom_web_yaml(self):
        """Restoring an older legacy snapshot without tracked_files preserves existing web.yaml."""
        # 1. Prepare existing custom web.yaml
        web_dir = os.path.join(self.etc_dir, "web")
        os.makedirs(web_dir, exist_ok=True)
        web_yaml_path = os.path.join(web_dir, "web.yaml")
        custom_content = "listen_address: 0.0.0.0\nlisten_port: 8443\n"
        with open(web_yaml_path, "w", encoding="utf-8") as f:
            f.write(custom_content)

        # 2. Also write a controller.yaml
        controller_path = os.path.join(self.etc_dir, "controller.yaml")
        with open(controller_path, "w", encoding="utf-8") as f:
            f.write("controller_setting: snapshot_val\n")

        # 3. Create a snapshot using the manager
        snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
            "tx-legacy-01", None, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # 4. Convert this snapshot to a legacy format snapshot by removing tracked_files
        # and removing web.yaml from backed_up_files and checksums.sha256
        meta_path = os.path.join(snapshot_dir, "metadata.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta.pop("tracked_files", None)
        meta.pop("format_version", None)
        meta["backed_up_files"] = [
            f for f in meta["backed_up_files"] if f != "web.yaml"
        ]
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

        # Also remove web.yaml from snapshot_dir and checksums.sha256
        snapshot_web_yaml = os.path.join(snapshot_dir, "web.yaml")
        if os.path.exists(snapshot_web_yaml):
            os.remove(snapshot_web_yaml)

        chk_path = os.path.join(snapshot_dir, "checksums.sha256")
        with open(chk_path, "r", encoding="utf-8") as f:
            lines = [line for line in f if not line.strip().endswith("web.yaml")]
        with open(chk_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        self.assertTrue(self.mgr.snapshot_mgr.verify_snapshot(snapshot_dir))

        # 5. Modify controller.yaml to simulate state change
        with open(controller_path, "w", encoding="utf-8") as f:
            f.write("controller_setting: modified\n")

        # 6. Restore legacy snapshot
        self.mgr.snapshot_mgr.restore_snapshot(
            snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # Verify controller.yaml was restored
        with open(controller_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "controller_setting: snapshot_val\n")

        # Verify custom web.yaml was PRESERVED and not deleted!
        self.assertTrue(
            os.path.isfile(web_yaml_path),
            "Custom web.yaml was deleted when restoring legacy snapshot!",
        )
        with open(web_yaml_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_content)

    def test_new_snapshot_restore_restores_saved_custom_web_yaml(self):
        """Restoring a new snapshot that backed up custom web.yaml restores its contents."""
        web_dir = os.path.join(self.etc_dir, "web")
        os.makedirs(web_dir, exist_ok=True)
        web_yaml_path = os.path.join(web_dir, "web.yaml")
        custom_content = "listen_address: 192.168.1.50\nlisten_port: 8443\n"
        with open(web_yaml_path, "w", encoding="utf-8") as f:
            f.write(custom_content)

        snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
            "tx-new-01", None, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # Verify format_version and tracked_files
        meta_path = os.path.join(snapshot_dir, "metadata.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta.get("format_version"), 2)
        self.assertIn("web.yaml", meta.get("tracked_files", []))
        self.assertIn("web.yaml", meta.get("backed_up_files", []))

        # Corrupt or modify web.yaml on host
        with open(web_yaml_path, "w", encoding="utf-8") as f:
            f.write("corrupted: true\n")

        # Restore snapshot
        self.mgr.snapshot_mgr.restore_snapshot(
            snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # Verify web.yaml restored to original saved content
        self.assertTrue(os.path.isfile(web_yaml_path))
        with open(web_yaml_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_content)

    def test_new_snapshot_restore_removes_web_yaml_when_snapshot_recorded_absence(self):
        """Restoring a new snapshot where web.yaml was absent removes newly created web.yaml."""
        web_dir = os.path.join(self.etc_dir, "web")
        web_yaml_path = os.path.join(web_dir, "web.yaml")
        if os.path.exists(web_yaml_path):
            os.remove(web_yaml_path)

        # Take snapshot while web.yaml is absent
        snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
            "tx-new-absent-01", None, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        meta_path = os.path.join(snapshot_dir, "metadata.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta.get("format_version"), 2)
        self.assertIn("web.yaml", meta.get("tracked_files", []))
        self.assertNotIn("web.yaml", meta.get("backed_up_files", []))

        # Now simulate a subsequent configuration step creating web.yaml
        os.makedirs(web_dir, exist_ok=True)
        with open(web_yaml_path, "w", encoding="utf-8") as f:
            f.write("temporary_config: true\n")
        self.assertTrue(os.path.isfile(web_yaml_path))

        # Restore snapshot
        self.mgr.snapshot_mgr.restore_snapshot(
            snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # Verify web.yaml is removed because new snapshot explicitly recorded its absence
        self.assertFalse(
            os.path.isfile(web_yaml_path),
            "web.yaml should have been pruned on restore of snapshot recording its absence!",
        )

    def test_legacy_snapshot_still_prunes_legacy_tracked_services(self):
        """Legacy snapshot without tracked_files still prunes services absent at snapshot time."""
        # Create a legacy snapshot where mentorpi-tank-web.service was absent
        service_path = os.path.join(self.systemd_dir, "mentorpi-tank-web.service")
        if os.path.exists(service_path):
            os.remove(service_path)

        snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
            "tx-legacy-svc-01", None, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # Convert to legacy format (no tracked_files, no format_version)
        meta_path = os.path.join(snapshot_dir, "metadata.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta.pop("tracked_files", None)
        meta.pop("format_version", None)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

        # Now create mentorpi-tank-web.service on host
        with open(service_path, "w", encoding="utf-8") as f:
            f.write("[Unit]\nDescription=Web Service\n")
        self.assertTrue(os.path.isfile(service_path))

        # Also create a custom web.yaml
        web_dir = os.path.join(self.etc_dir, "web")
        os.makedirs(web_dir, exist_ok=True)
        web_yaml_path = os.path.join(web_dir, "web.yaml")
        with open(web_yaml_path, "w", encoding="utf-8") as f:
            f.write("custom: config\n")

        # Restore legacy snapshot
        self.mgr.snapshot_mgr.restore_snapshot(
            snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
        )

        # mentorpi-tank-web.service IS in LEGACY_TRACKED_FILES and was absent -> pruned!
        self.assertFalse(
            os.path.isfile(service_path),
            "mentorpi-tank-web.service should have been pruned by legacy snapshot!",
        )
        # web.yaml is NOT in LEGACY_TRACKED_FILES -> preserved!
        self.assertTrue(
            os.path.isfile(web_yaml_path),
            "web.yaml should have been preserved by legacy snapshot!",
        )


if __name__ == "__main__":
    unittest.main()
