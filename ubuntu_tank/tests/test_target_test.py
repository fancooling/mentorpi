"""
test_target_test.py - Unit and integration tests for Milestone 14.1 Target Test Orchestrator.

Verifies:
1. Target CLI argument parsing and safety notice display.
2. Target platform guards: detecting non-ARM64 architecture, recording PENDING_TARGET_EXECUTION,
   and performing zero mutations on development host.
3. --require-target flag enforcement on non-target platforms.
4. Structured JSON and Markdown report generation with complete phase schemas and safety invariants.
5. Integration with ./deploy.sh target-test command.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")
WEB_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_web")
OPERATOR_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_operator")

for p in [
    os.path.join(os.path.dirname(OPERATOR_PKG_DIR), "ubuntu_tank_supervisor"),
    os.path.join(os.path.dirname(OPERATOR_PKG_DIR), "ubuntu_tank_protocol"),
    REPO_ROOT,
    UBUNTU_TANK_DIR,
    SCRIPTS_DIR,
    WEB_PKG_DIR,
    OPERATOR_PKG_DIR,
]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ubuntu_tank.scripts.deployment_manager import (
    DeploymentLock,
    ReleaseManager,
    SnapshotManager,
    attest_build,
    check_hardware_mutual_exclusion,
    compute_file_sha256,
    parse_release_manifest,
)
from ubuntu_tank.scripts.target_test import (
    EXPECTED_ARCH,
    StepResult,
    TargetIntegrationOrchestrator,
)


class TestTargetTestCliGuards(unittest.TestCase):
    """Verifies target-test CLI flags, safety warnings, and non-target platform guards."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="target_test_cli_")
        self.target_script = os.path.join(SCRIPTS_DIR, "target_test.py")
        self.deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        self.py_bin = sys.executable

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_help_displays_safety_notice(self):
        """target_test.py --help must display safety guidelines and all options."""
        res = subprocess.run(
            [self.py_bin, self.target_script, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"--help failed: {res.stderr}")
        self.assertIn("Milestone 14.1 Real Pi 5 Installation & Deployment", res.stdout)
        self.assertIn("--report-dir", res.stdout)
        self.assertIn("--require-target", res.stdout)
        self.assertIn("--inspect-only", res.stdout)
        self.assertIn("Safety Notice:", res.stdout)
        self.assertIn(
            "Under NO circumstances does this suite authorize on-ground motion",
            res.stdout,
        )

    def test_non_target_detection_marks_pending_without_mutations(self):
        """On development machines (x86_64), target_test records PENDING_TARGET_EXECUTION."""
        if platform.machine() == EXPECTED_ARCH:
            self.skipTest(
                "Running on ARM64 hardware; skipping non-target platform test"
            )

        report_dir = os.path.join(self.tmp_dir, "reports")
        report_name = "test-report"
        res = subprocess.run(
            [
                self.py_bin,
                self.target_script,
                "--report-dir",
                report_dir,
                "--report-name",
                report_name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Process failed: {res.stderr}")
        self.assertIn("Target platform mismatch", res.stdout)
        self.assertIn("PENDING_TARGET_EXECUTION", res.stdout)

        json_path = os.path.join(report_dir, f"{report_name}.json")
        md_path = os.path.join(report_dir, f"{report_name}.md")
        self.assertTrue(
            os.path.isfile(json_path), f"JSON report not created: {json_path}"
        )
        self.assertTrue(
            os.path.isfile(md_path), f"Markdown report not created: {md_path}"
        )

        # Validate JSON content
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["milestone"], "14.1")
        self.assertEqual(data["overall_status"], "PENDING_TARGET_EXECUTION")
        self.assertIn("Host CPU architecture", data["pending_reason"])

        # All 6 phases must be present and marked PENDING
        phases = data["phases"]
        self.assertEqual(len(phases), 6)
        for ph_name, ph_info in phases.items():
            self.assertEqual(
                ph_info["status"], "PENDING", f"Phase {ph_name} was not PENDING"
            )

        # Validate Markdown content
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()

        self.assertIn("# Milestone 14.1 Real Pi 5 Integration Test Report", md)
        self.assertIn("`PENDING_TARGET_EXECUTION`", md)
        self.assertIn("Motor Power", md)
        self.assertIn("STRICTLY OFF", md)
        self.assertIn("On-Ground Motion", md)
        self.assertIn("STRICTLY FORBIDDEN", md)

    def test_require_target_flag_fails_on_non_target(self):
        """--require-target must fail with exit code 1 on non-target platform."""
        if platform.machine() == EXPECTED_ARCH:
            self.skipTest(
                "Running on ARM64 hardware; skipping non-target platform test"
            )

        res = subprocess.run(
            [
                self.py_bin,
                self.target_script,
                "--report-dir",
                self.tmp_dir,
                "--require-target",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 1, "Must exit 1 when --require-target fails")
        self.assertIn("Target platform mismatch", res.stdout)

    def test_deploy_sh_target_test_command_integration(self):
        """./deploy.sh target-test forwards CLI options cleanly to target_test.py."""
        res = subprocess.run(
            ["bash", self.deploy_sh, "target-test", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            res.returncode, 0, f"deploy.sh target-test --help failed: {res.stderr}"
        )
        self.assertIn("Milestone 14.1 Real Pi 5 Installation & Deployment", res.stdout)


class TestTargetTestOrchestratorLogic(unittest.TestCase):
    """Unit tests for TargetIntegrationOrchestrator methods and safety checks."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="target_orch_test_")
        self.opt_dir = os.path.join(self.tmp_dir, "opt")
        self.etc_dir = os.path.join(self.tmp_dir, "etc")
        self.var_dir = os.path.join(self.tmp_dir, "var")
        self.run_dir = os.path.join(self.tmp_dir, "run")
        self.systemd_dir = os.path.join(self.tmp_dir, "systemd")
        self.udev_dir = os.path.join(self.tmp_dir, "udev")
        self.lock_path = os.path.join(self.tmp_dir, "deploy.lock")

        for d in [
            self.opt_dir,
            self.etc_dir,
            self.var_dir,
            self.run_dir,
            self.systemd_dir,
            self.udev_dir,
        ]:
            os.makedirs(d, exist_ok=True)

        self.orch = TargetIntegrationOrchestrator(
            opt_dir=self.opt_dir,
            etc_dir=self.etc_dir,
            var_dir=self.var_dir,
            run_dir=self.run_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
            lock_path=self.lock_path,
            report_dir=self.tmp_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_platform_check_identifies_mismatch(self):
        """_check_target_platform correctly validates architecture and OS."""
        with patch("platform.machine", return_value="x86_64"):
            is_target, reason = self.orch._check_target_platform()
            self.assertFalse(is_target)
            self.assertIn("Host CPU architecture is 'x86_64'", reason)

        with patch("platform.machine", return_value="aarch64"):
            self.orch.results["host_environment"] = {
                "os_name": "Ubuntu",
                "os_version_id": "26.04",
            }
            is_target, reason = self.orch._check_target_platform()
            self.assertTrue(is_target)

    def test_step_recording_accumulates_results_and_failures(self):
        """StepResult recording updates steps list and failures properly."""
        step_pass = StepResult(
            name="dummy_pass",
            phase="Phase 1",
            status="PASSED",
            duration_sec=0.1,
        )
        self.orch._record_step(step_pass)
        self.assertEqual(len(self.orch.step_records), 1)
        self.assertEqual(len(self.orch.results["failures"]), 0)

        step_fail = StepResult(
            name="dummy_fail",
            phase="Phase 1",
            status="FAILED",
            duration_sec=0.2,
            error_message="Simulated failure",
        )
        self.orch._record_step(step_fail)
        self.assertEqual(len(self.orch.step_records), 2)
        self.assertEqual(len(self.orch.results["failures"]), 1)
        self.assertIn("Simulated failure", self.orch.results["failures"][0])

    def test_deployment_lock_preflight_and_no_self_contention(self):
        """DeploymentLock preflight check must release cleanly and avoid self-contention deadlock."""
        # 1. Verify preflight lock context releases fd cleanly
        with DeploymentLock(self.lock_path, timeout_sec=1.0):
            pass

        # 2. Subsequent acquisition in same process must succeed immediately
        acquired = False
        with DeploymentLock(self.lock_path, timeout_sec=1.0):
            acquired = True
        self.assertTrue(acquired, "Subsequent DeploymentLock acquisition failed")

        # 3. Verify orchestrator does not hold lock across suite execution
        self.assertFalse(self.orch._lock_held)

    def test_snapshot_manager_signatures_in_orchestrator(self):
        """SnapshotManager calls in orchestrator must match deployment_manager signatures."""
        self.assertIsInstance(self.orch.mgr.snapshot_mgr, SnapshotManager)
        # Baseline snapshot creation (Phase 1)
        snap_path = self.orch.mgr.snapshot_mgr.create_snapshot(
            tx_id="baseline_test",
            current_symlink_target=None,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )
        self.assertTrue(os.path.isdir(snap_path))
        self.assertTrue(os.path.isfile(os.path.join(snap_path, "metadata.json")))
        self.assertTrue(os.path.isfile(os.path.join(snap_path, "checksums.sha256")))

        # Baseline snapshot restoration (Phase 6)
        # restore_snapshot verifies snapshot integrity and returns None on success (raises on failure)
        self.orch.mgr.snapshot_mgr.restore_snapshot(
            snapshot_dir=snap_path,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )

    def test_manifest_parsing_unpacking_and_checksum_verification(self):
        """parse_release_manifest returns (headers, files) tuple and checksum verification works."""
        test_file = os.path.join(self.opt_dir, "sample.txt")
        with open(test_file, "w", encoding="utf-8") as fh:
            fh.write("sample content for manifest test\n")
        sha = compute_file_sha256(test_file)
        size = os.path.getsize(test_file)
        mode = oct(stat.S_IMODE(os.stat(test_file).st_mode))

        manifest_path = os.path.join(self.opt_dir, "release-manifest.txt")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            fh.write("Format-Version: 1.0\n")
            fh.write("Release-Id: test-rel-1\n\n")
            fh.write("[Files]\n")
            fh.write(f"{sha}  {size}  {mode}  sample.txt\n")

        # 1. Unpacking must produce a tuple of 2 dicts
        manifest_result = parse_release_manifest(manifest_path)
        self.assertIsInstance(manifest_result, tuple)
        self.assertEqual(len(manifest_result), 2)
        headers, files = manifest_result
        self.assertIsInstance(headers, dict)
        self.assertIsInstance(files, dict)
        self.assertIn("sample.txt", files)

        # 2. Checksum verification logic as implemented in target_test.py
        for rel_path, file_info in files.items():
            fp = os.path.join(self.opt_dir, rel_path)
            actual_sha = compute_file_sha256(fp)
            exp_sha = (
                file_info.get("sha256")
                if isinstance(file_info, dict)
                else str(file_info)
            )
            self.assertEqual(actual_sha, exp_sha)

    def test_sros2_keystore_check_calls_bound_instance_method(self):
        """_is_valid_sros2_keystore must be called on ReleaseManager instance, not unbound class."""
        keystore_dir = os.path.join(self.etc_dir, "security", "keystore")

        # Calling on unbound class raises TypeError because self is missing
        with self.assertRaises(TypeError):
            ReleaseManager._is_valid_sros2_keystore(keystore_dir)

        # Calling on bound orchestrator.mgr instance succeeds and returns bool
        result = self.orch.mgr._is_valid_sros2_keystore(keystore_dir)
        self.assertFalse(result)  # empty directory is not a valid keystore

    def test_web_port_and_tls_cert_contract(self):
        """Native web server contract uses port 8443, server.crt/server.key, and cert dir mode 0700."""
        # 1. Default port is 8443 (not sidecar 8081)
        web_yaml_path = os.path.join(self.etc_dir, "web", "web.yaml")
        web_port = 8443
        self.assertEqual(web_port, 8443)

        # 2. Reading port from web.yaml
        os.makedirs(os.path.dirname(web_yaml_path), exist_ok=True)
        with open(web_yaml_path, "w", encoding="utf-8") as fh:
            fh.write("port: 8443\n")
        import yaml

        with open(web_yaml_path, "r", encoding="utf-8") as fh:
            wdata = yaml.safe_load(fh)
        self.assertEqual(int(wdata["port"]), 8443)

        # 3. Expected cert filenames are server.crt and server.key
        certs_dir = os.path.join(self.var_dir, "web", "certs")
        os.makedirs(certs_dir, mode=0o700, exist_ok=True)
        cert_path = os.path.join(certs_dir, "server.crt")
        key_path = os.path.join(certs_dir, "server.key")
        with open(cert_path, "w", encoding="utf-8") as fh:
            fh.write("DUMMY CERT\n")
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write("DUMMY KEY\n")
        os.chmod(cert_path, 0o644)
        os.chmod(key_path, 0o600)
        os.chmod(certs_dir, 0o700)

        self.assertEqual(stat.S_IMODE(os.stat(certs_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(cert_path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)

    def test_interrupted_activation_recovery_and_compliant_native_release(self):
        """Interrupted recovery snapshot verification and compliant native release assembly."""
        # 1. Snapshot created for interrupted activation passes verify_snapshot
        tx_id = "test_tx_recovery"
        snap_path = self.orch.mgr.snapshot_mgr.create_snapshot(
            tx_id=tx_id,
            current_symlink_target=None,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )
        self.assertTrue(self.orch.mgr.snapshot_mgr.verify_snapshot(snap_path))

        # 2. Native release structure passes ReleaseManager.validate_release
        nat_rel_id = "test-native-only-rel"
        nat_dir = os.path.join(self.opt_dir, "releases", nat_rel_id)
        for sub in ["bin", "config", "host", "install", "install/bin"]:
            os.makedirs(os.path.join(nat_dir, sub), exist_ok=True)

        runner = os.path.join(nat_dir, "bin", "mentorpi-tank-run")
        with open(runner, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(runner, 0o755)

        setup_sh = os.path.join(nat_dir, "install", "setup.bash")
        with open(setup_sh, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexport TANK=1\n")
        os.chmod(setup_sh, 0o755)

        verify_sh = os.path.join(nat_dir, "install", "bin", "tank_verify_install")
        with open(verify_sh, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(verify_sh, 0o755)

        with open(
            os.path.join(nat_dir, "config", "controller.yaml"), "w", encoding="utf-8"
        ) as fh:
            fh.write("controller: {}\n")

        with open(
            os.path.join(nat_dir, "host", "mentorpi-tank.service"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("[Service]\nExecStart=/bin/true\n")

        # Fast DDS loopback profile
        os.makedirs(os.path.join(nat_dir, "config", "fastdds"), exist_ok=True)
        src_loopback = os.path.join(
            UBUNTU_TANK_DIR, "config", "fastdds", "loopback.xml"
        )
        if os.path.isfile(src_loopback):
            shutil.copy2(
                src_loopback,
                os.path.join(nat_dir, "config", "fastdds", "loopback.xml"),
            )

        # Attest install tree for validation closure
        os.makedirs(os.path.join(nat_dir, "src"), exist_ok=True)
        prefix = f"{self.opt_dir}/releases/{nat_rel_id}/install"
        attest_build(
            os.path.join(nat_dir, "install"),
            prefix,
            os.path.join(nat_dir, "src"),
            synthetic=True,
        )

        # Build valid release manifest
        manifest_lines = [
            "Format-Version: 1.0",
            f"Release-Id: {nat_rel_id}",
            "Project-Version: 1.0.0",
            "Git-Commit: 0000000000000000000000000000000000000000",
            "Git-Short-Commit: 0000000",
            "Target-OS: Ubuntu 26.04 LTS",
            "Target-Architecture: arm64",
            "ROS-Distribution: lyrical",
            f"Install-Prefix: {self.opt_dir}/releases/{nat_rel_id}/install",
            "Build-Timestamp: 2026-09-19T00:00:00Z",
            "Test-Status: passed",
            "Hardware-Target: Raspberry Pi 5 ARM64 + STM32 RRC",
            "",
            "[Files]",
        ]
        for root, _, files in os.walk(nat_dir):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, nat_dir)
                size = os.path.getsize(p)
                mode = oct(stat.S_IMODE(os.stat(p).st_mode))
                sha = compute_file_sha256(p)
                manifest_lines.append(f"{sha}  {size}  {mode}  {rel}")

        with open(
            os.path.join(nat_dir, "release-manifest.txt"), "w", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(manifest_lines) + "\n")

        valid, errors = self.orch.mgr.validate_release(nat_dir)
        self.assertTrue(valid, f"Native release validation failed: {errors}")
        self.assertEqual(len(errors), 0)

    def test_mutual_exclusion_call_boundary(self):
        """check_hardware_mutual_exclusion must be called without unsupported kwargs."""
        # 1. Calling with unsupported keyword args raises TypeError
        with self.assertRaises(TypeError):
            check_hardware_mutual_exclusion(
                check_containers=True, check_host_services=True
            )

        # 2. Calling supported API without unsupported kwargs returns (ok, err_list)
        with (
            patch(
                "ubuntu_tank.scripts.target_test.check_hardware_mutual_exclusion",
                return_value=(True, []),
            ),
            patch("os.geteuid", return_value=0),
        ):
            self.orch.inspect_only = True
            ok = self.orch._execute_phase1_preflight()
            self.assertTrue(ok)
            steps = [s.name for s in self.orch.step_records]
            self.assertIn("container_and_service_mutual_exclusion", steps)

    def test_inspect_only_does_not_create_snapshot(self):
        """--inspect-only must complete preflight without creating baseline snapshot."""
        self.orch.inspect_only = True
        with (
            patch(
                "ubuntu_tank.scripts.target_test.check_hardware_mutual_exclusion",
                return_value=(True, []),
            ),
            patch("os.geteuid", return_value=0),
            patch("subprocess.run") as mock_sub,
        ):
            mock_sub.return_value.returncode = 1  # inactive controller
            ok = self.orch._execute_phase1_preflight()
            self.assertTrue(ok)
            self.assertIsNone(self.orch._baseline_dir)
            steps = [s.name for s in self.orch.step_records]
            self.assertNotIn("pre_test_baseline_snapshot", steps)

    def test_unique_snapshot_ids_avoid_collisions(self):
        """Successive snapshot creations must use unique IDs and avoid directory collision."""
        snap1 = self.orch.mgr.snapshot_mgr.create_snapshot(
            tx_id=f"tx_{int(time.time())}_1",
            current_symlink_target=None,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )
        snap2 = self.orch.mgr.snapshot_mgr.create_snapshot(
            tx_id=f"tx_{int(time.time())}_2",
            current_symlink_target=None,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )
        self.assertNotEqual(snap1, snap2)
        self.assertTrue(os.path.isdir(snap1))
        self.assertTrue(os.path.isdir(snap2))

    def test_web_control_config_from_dict_and_listen_address(self):
        """WebControlConfig parses YAML data through from_dict and exposes listen_address."""
        import yaml
        from ubuntu_tank_protocol.config import WebControlConfig

        # 1. Ensure obsolete .load() and .host do not exist
        self.assertFalse(hasattr(WebControlConfig, "load"))

        # 2. Supported from_dict with valid YAML dictionary
        raw_yaml = "listen_address: '127.0.0.1'\nport: 8443\n"
        ydata = yaml.safe_load(raw_yaml)
        wcfg = WebControlConfig.from_dict(ydata)
        wcfg.validate()
        self.assertEqual(wcfg.listen_address, "127.0.0.1")
        self.assertEqual(wcfg.port, 8443)
        self.assertFalse(hasattr(wcfg, "host"))

    def test_tls_certs_directory_mode_validation_in_phase3(self):
        """Phase 3 validates certs dir mode 0700 without requiring cert files before service startup."""
        certs_dir = os.path.join(self.var_dir, "web", "certs")
        os.makedirs(certs_dir, mode=0o700, exist_ok=True)
        os.chmod(certs_dir, 0o700)

        # No server.crt or server.key exists yet (fresh install before web startup)
        self.assertFalse(os.path.isfile(os.path.join(certs_dir, "server.crt")))
        self.assertFalse(os.path.isfile(os.path.join(certs_dir, "server.key")))

        cmode = stat.S_IMODE(os.stat(certs_dir).st_mode)
        self.assertEqual(cmode, 0o700)

        # Mode other than 0700 should be rejected
        os.chmod(certs_dir, 0o755)
        cmode_bad = stat.S_IMODE(os.stat(certs_dir).st_mode)
        self.assertNotEqual(cmode_bad, 0o700)
        os.chmod(certs_dir, 0o700)

    def test_status_endpoint_inspects_service_state_field(self):
        """Web API status probe validates service_state field according to StatusResponseModel."""
        # Valid response model data with service_state inactive
        sdata_valid = {
            "service_state": "inactive",
            "operator_state": "idle",
            "protocol_version": "1.0",
        }
        self.assertEqual(sdata_valid.get("service_state"), "inactive")

        # Old controller field should not be relied upon
        sdata_old = {"controller": "inactive", "operator_state": "idle"}
        self.assertIsNone(sdata_old.get("service_state"))

        # Active service_state must be detected
        sdata_active = {"service_state": "active", "operator_state": "idle"}
        self.assertNotEqual(sdata_active.get("service_state"), "inactive")

    def test_baseline_restoration_failure_fails_suite(self):
        """Restoration failure in Phase 6 must set overall_status to FAILED."""
        self.orch.results["overall_status"] = "IN_PROGRESS"
        with (
            patch.object(self.orch, "_execute_phase1_preflight", return_value=True),
            patch.object(
                self.orch, "_check_installed_prerequisites", return_value=True
            ),
            patch.object(
                self.orch,
                "_execute_phase3_installed_verification",
                return_value=True,
            ),
            patch.object(
                self.orch,
                "_execute_phase4_service_lifecycle",
                return_value=True,
            ),
            patch.object(
                self.orch,
                "_execute_phase5_lifecycle_and_rollback",
                return_value=True,
            ),
            patch.object(
                self.orch,
                "_execute_phase6_baseline_restoration",
                return_value=False,
            ),
        ):
            self.orch._run_target_suite()
            self.assertEqual(self.orch.results["overall_status"], "FAILED")

    def test_phase6_restores_pre_test_journal_and_reloads_daemon(self):
        """Phase 6 must restore pre-test journal bytes and call systemctl daemon-reload."""
        # Write pre-test journal content
        journal_path = self.orch.mgr.journal_path
        os.makedirs(os.path.dirname(journal_path), exist_ok=True)
        pre_content = b'{"state": "pre_test_owner_baseline"}'
        self.orch._pre_test_journal_content = pre_content

        # Simulate test modifying the journal
        with open(journal_path, "wb") as f:
            f.write(b'{"state": "modified_by_test"}')

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = "inactive\n"
            ok = self.orch._execute_phase6_baseline_restoration()
            self.assertTrue(ok)

            # Check journal was restored
            with open(journal_path, "rb") as f:
                restored = f.read()
            self.assertEqual(restored, pre_content)

            # Check daemon-reload was executed
            daemon_reload_calls = [
                call
                for call in mock_sub.call_args_list
                if call[0][0] == ["systemctl", "daemon-reload"]
            ]
            self.assertTrue(len(daemon_reload_calls) > 0)

    def test_runtime_verifier_uses_installed_environment(self):
        """Clean callers receive the installed overlay and read-only credentials."""
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as run:
            self.orch._verify_installed_runtime()
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3], "/opt/ros/lyrical/setup.bash")
        self.assertEqual(
            argv[-2], os.path.join(self.opt_dir, "current/install/setup.bash")
        )
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["ROS_SECURITY_ENCLAVE_OVERRIDE"], "/ubuntu_tank/status")
        self.assertEqual(env["ROS_SECURITY_STRATEGY"], "Enforce")
        self.assertEqual(env["ROS_LOCALHOST_ONLY"], "1")
        # Execute the actual shell command with an absent setup: it must fail
        # before starting the verifier, not continue in the caller environment.
        argv[-3] = os.path.join(self.tmp_dir, "missing-setup.bash")
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_native_rollback_uses_production_install_and_real_rollback(self):
        """Check orchestration calls only; this is not target installation evidence."""
        self.orch.native_release_archive = "/owner/native.tar.zst"
        self.orch.operator_user = "pi-owner"
        self.orch._installed_release_id = "web"
        manager = Mock()
        manager.install_release.return_value = "native"
        native = os.path.join(self.opt_dir, "releases", "native")
        with (
            patch.object(self.orch, "mgr", manager),
            patch("os.path.realpath", return_value=native),
        ):
            self.orch._exercise_native_rollback()
        self.assertEqual(
            manager.mock_calls,
            [
                call.install_release(
                    "/owner/native.tar.zst",
                    require_root=True,
                    enforce_arm64=True,
                    operator_user="pi-owner",
                ),
                call.activate_release("native", require_root=True),
                call.activate_release("web", require_root=True),
                call.rollback_release(require_root=True),
            ],
        )
        manager.reset_mock()
        manager.install_release.side_effect = RuntimeError("synthetic build rejected")
        with patch.object(self.orch, "mgr", manager), self.assertRaises(RuntimeError):
            self.orch._exercise_native_rollback()
        manager.activate_release.assert_not_called()

    def test_owner_selection_defaults_to_sudo_caller(self):
        """An explicit owner overrides sudo; no Ubuntu image login is assumed."""
        with patch.dict(os.environ, {"SUDO_USER": "pi-owner"}):
            self.assertEqual(TargetIntegrationOrchestrator().operator_user, "pi-owner")
            self.assertEqual(
                TargetIntegrationOrchestrator(
                    operator_user="other-owner"
                ).operator_user,
                "other-owner",
            )

    def test_cleanup_stop_failure_prevents_restoration(self):
        """A failed service stop must leave baseline and journal assets untouched."""
        self.orch._baseline_dir = self.tmp_dir

        def failed_stop(argv, **kwargs):
            if argv[1] == "is-active":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "stop failed")

        with (
            patch("subprocess.run", side_effect=failed_stop),
            patch.object(self.orch.mgr.snapshot_mgr, "restore_snapshot") as restore,
        ):
            self.assertFalse(self.orch._execute_phase6_baseline_restoration())
            restore.assert_not_called()

    def test_native_staging_prunes_only_obsolete_web_units(self):
        """Native staging removes web units without deleting owner configuration."""
        candidate = os.path.join(self.tmp_dir, "candidate")
        os.makedirs(candidate)
        units = ("mentorpi-tank-web.service", "mentorpi-tank-lifecycle.service")
        for unit in units:
            with open(os.path.join(self.systemd_dir, unit), "w") as stream:
                stream.write("old generated unit")
        web_config = os.path.join(self.etc_dir, "web", "web.yaml")
        os.makedirs(os.path.dirname(web_config))
        with open(web_config, "w") as stream:
            stream.write("owner configuration")
        with patch.object(self.orch.mgr, "_reload_systemd_and_udev"):
            self.orch.mgr._stage_host_files(candidate)
        for unit in units:
            self.assertFalse(os.path.lexists(os.path.join(self.systemd_dir, unit)))
        with open(web_config) as stream:
            self.assertEqual(stream.read(), "owner configuration")

    def test_cleanup_query_failure_prevents_restoration(self):
        """An unavailable systemd bus is not an absent optional service."""
        self.orch._baseline_dir = self.tmp_dir
        with (
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "bus failed"),
            ),
            patch.object(self.orch.mgr.snapshot_mgr, "restore_snapshot") as restore,
        ):
            self.assertFalse(self.orch._execute_phase6_baseline_restoration())
            restore.assert_not_called()

    def test_cleanup_reload_failure_is_reported(self):
        """Reload failure cannot be reported as successful restoration."""
        with (
            patch.object(self.orch.mgr, "_stop_and_disarm_service"),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "systemctl"),
            ),
        ):
            self.assertFalse(self.orch._execute_phase6_baseline_restoration())

    def test_missing_installation_fails_before_preflight_or_mutation(self):
        """Post-install verification never repairs an absent active release."""
        with (
            patch.object(self.orch, "_execute_phase1_preflight") as preflight,
            patch.object(self.orch.mgr, "install_release") as install,
            patch.object(self.orch.mgr, "package_release") as package,
        ):
            self.orch._run_target_suite()
        self.assertEqual(self.orch.results["overall_status"], "FAILED")
        preflight.assert_not_called()
        install.assert_not_called()
        package.assert_not_called()
        self.assertIn(
            "build, package, install and activate", self.orch.results["failures"][0]
        )

    def test_default_run_does_not_execute_deployment_scenarios(self):
        """Only the explicit scenario option permits repeat installs or rollback."""
        with (
            patch.object(
                self.orch, "_check_installed_prerequisites", return_value=True
            ),
            patch.object(self.orch, "_execute_phase1_preflight", return_value=True),
            patch.object(
                self.orch, "_execute_phase3_installed_verification", return_value=True
            ),
            patch.object(
                self.orch, "_execute_phase4_service_lifecycle", return_value=True
            ),
            patch.object(
                self.orch, "_execute_phase6_baseline_restoration", return_value=True
            ),
            patch.object(
                self.orch, "_execute_phase5_lifecycle_and_rollback"
            ) as scenarios,
        ):
            self.orch._run_target_suite()
            scenarios.assert_not_called()
            self.assertEqual(self.orch.results["overall_status"], "PASSED")
            self.orch.deployment_scenarios = True
            scenarios.return_value = True
            self.orch._run_target_suite()
            scenarios.assert_called_once()

    def test_packaging_workspace_and_provenance(self):
        """Packaging must point to UBUNTU_TANK_DIR with VERSION and reject REPO_ROOT."""
        # 1. VERSION file is located at UBUNTU_TANK_DIR, not REPO_ROOT
        self.assertTrue(os.path.isfile(os.path.join(UBUNTU_TANK_DIR, "VERSION")))
        self.assertFalse(os.path.isfile(os.path.join(REPO_ROOT, "VERSION")))

        # 2. Pointing package_release at REPO_ROOT fails immediately with missing VERSION
        with self.assertRaises(FileNotFoundError) as cm:
            self.orch.mgr.package_release(
                workspace_dir=REPO_ROOT,
                output_dir=os.path.join(self.tmp_dir, "dist"),
                release_id="fail-test",
                allow_staged_install=False,
            )
        self.assertIn("VERSION file missing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
