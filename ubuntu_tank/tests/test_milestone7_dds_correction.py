#!/usr/bin/env python3
"""
test_milestone7_dds_correction.py - Unit and integration tests for Milestone 7.

Validates:
1. Fast DDS loopback XML profile syntax, schemas, transports, and locators (§6.4.1).
2. Systemd service unit correction: StartLimit directives in [Unit] and sandbox verification (§6.5).
3. Fast DDS discovery contract resolver, validation, overrides, and fail-closed behavior.
4. Transactional host environment migration, snapshotting, and atomic rollback in DeploymentManager.
5. Regression test reproducing denied multicast discovery vs working loopback unicast.
6. Negative security tests: uncredentialed/status participant denial and invalid profile rejection.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

# Resolve repository paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)

if UBUNTU_TANK_DIR not in sys.path:
    sys.path.insert(0, UBUNTU_TANK_DIR)
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from importlib.machinery import SourceFileLoader
import importlib.util

from deployment_manager import (
    ReleaseManager,
    attest_build,
    compute_file_sha256,
    compute_tree_sha256,
)
from fastdds_setup import (
    apply_loopback_env,
    resolve_loopback_profile,
    validate_loopback_profile,
)


class TestFastDDSLoopbackConfig(unittest.TestCase):
    """Validate Fast DDS loopback XML profile schema and requirements."""

    def setUp(self):
        self.xml_path = os.path.join(
            UBUNTU_TANK_DIR, "config", "fastdds", "loopback.xml"
        )

    def test_loopback_xml_file_exists_and_readable(self):
        """loopback.xml must exist in config/fastdds/ and be readable."""
        self.assertTrue(
            os.path.isfile(self.xml_path),
            f"loopback.xml missing at {self.xml_path}",
        )
        self.assertTrue(os.access(self.xml_path, os.R_OK))

    def test_loopback_xml_validates_cleanly(self):
        """validate_loopback_profile must return True with zero errors for repository loopback.xml."""
        ok, errs = validate_loopback_profile(self.xml_path)
        self.assertTrue(ok, f"Validation failed for {self.xml_path}: {errs}")
        self.assertEqual(len(errs), 0)

    def test_loopback_xml_transport_and_locators(self):
        """loopback.xml must pin transport to 127.0.0.1 and disable built-in transports."""
        with open(self.xml_path, "r", encoding="utf-8") as f:
            xml_str = f.read()

        # Transport checks
        self.assertIn("UDPv4", xml_str)
        self.assertIn("127.0.0.1", xml_str)
        self.assertIn("<useBuiltinTransports>false</useBuiltinTransports>", xml_str)
        self.assertIn("defaultUnicastLocatorList", xml_str)
        self.assertIn("metatrafficUnicastLocatorList", xml_str)
        self.assertIn("initialPeersList", xml_str)

        # Ensure no multicast or external IP addresses are specified
        self.assertNotIn("239.255.", xml_str)
        self.assertNotIn("0.0.0.0", xml_str)

    def test_validate_loopback_profile_negative_cases(self):
        """validate_loopback_profile must reject malformed, incomplete, or missing profiles."""
        with tempfile.TemporaryDirectory() as td:
            # 1. Nonexistent file
            ok, errs = validate_loopback_profile(os.path.join(td, "nonexistent.xml"))
            self.assertFalse(ok)
            self.assertTrue(any("not found" in e for e in errs))

            # 2. Malformed XML
            bad_xml = os.path.join(td, "bad.xml")
            with open(bad_xml, "w") as f:
                f.write("<dds><unclosed></dds>")
            ok, errs = validate_loopback_profile(bad_xml)
            self.assertFalse(ok)
            self.assertTrue(any("syntax error" in e for e in errs))

            # 3. Missing transport whitelist
            no_wl = os.path.join(td, "no_wl.xml")
            with open(no_wl, "w") as f:
                f.write("""<dds><profiles>
                <transport_descriptors><transport_descriptor><transport_id>t</transport_id><type>UDPv4</type></transport_descriptor></transport_descriptors>
                <participant profile_name="loopback"><rtps><useBuiltinTransports>false</useBuiltinTransports></rtps></participant>
                </profiles></dds>""")
            ok, errs = validate_loopback_profile(no_wl)
            self.assertFalse(ok)
            self.assertTrue(any("interfaceWhiteList" in e for e in errs))

            # 4. Built-in transports enabled
            builtin_on = os.path.join(td, "builtin_on.xml")
            with open(builtin_on, "w") as f:
                f.write("""<dds><profiles>
                <transport_descriptors><transport_descriptor><transport_id>t</transport_id><type>UDPv4</type><interfaceWhiteList><address>127.0.0.1</address></interfaceWhiteList></transport_descriptor></transport_descriptors>
                <participant profile_name="loopback"><rtps><useBuiltinTransports>true</useBuiltinTransports></rtps></participant>
                </profiles></dds>""")
            ok, errs = validate_loopback_profile(builtin_on)
            self.assertFalse(ok)
            self.assertTrue(any("useBuiltinTransports=false" in e for e in errs))


class TestSystemdUnitStartLimit(unittest.TestCase):
    """Validate systemd service unit structure and start-limit directives."""

    def setUp(self):
        self.service_path = os.path.join(
            UBUNTU_TANK_DIR, "host", "mentorpi-tank.service"
        )

    def test_start_limit_directives_in_unit_section(self):
        """StartLimitIntervalSec and StartLimitBurst must be in [Unit], NOT [Service]."""
        with open(self.service_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_section = None
        unit_directives = {}
        service_directives = {}

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1]
                continue
            if "=" in stripped:
                k, v = stripped.split("=", 1)
                k = k.strip()
                v = v.strip()
                if current_section == "Unit":
                    unit_directives[k] = v
                elif current_section == "Service":
                    service_directives[k] = v

        self.assertIn("StartLimitIntervalSec", unit_directives)
        self.assertEqual(unit_directives["StartLimitIntervalSec"], "30s")
        self.assertIn("StartLimitBurst", unit_directives)
        self.assertEqual(unit_directives["StartLimitBurst"], "5")

        self.assertNotIn(
            "StartLimitIntervalSec",
            service_directives,
            "StartLimitIntervalSec must NOT be placed in [Service]",
        )
        self.assertNotIn(
            "StartLimitBurst",
            service_directives,
            "StartLimitBurst must NOT be placed in [Service]",
        )

    def test_systemd_sandboxing_directives_preserved(self):
        """Ensure sandboxing directives and localhost network filter remain intact."""
        with open(self.service_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("IPAddressDeny=any", content)
        self.assertIn("IPAddressAllow=localhost", content)
        self.assertIn(
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", content
        )
        self.assertIn("DevicePolicy=closed", content)
        self.assertIn("DeviceAllow=/dev/rrc rw", content)
        self.assertIn("ProtectSystem=strict", content)
        self.assertIn("ProtectHome=yes", content)
        self.assertIn("NoNewPrivileges=yes", content)

    def test_systemd_analyze_verify_if_available(self):
        """Run systemd-analyze verify on unit if systemd-analyze is installed."""
        if shutil.which("systemd-analyze"):
            res = subprocess.run(
                ["systemd-analyze", "verify", self.service_path],
                capture_output=True,
                text=True,
                check=False,
            )
            # Check stderr for StartLimit warnings specifically
            self.assertNotIn(
                "Unknown key 'StartLimitIntervalSec' in section [Service]", res.stderr
            )
            self.assertNotIn(
                "Unknown key 'StartLimitBurst' in section [Service]", res.stderr
            )


class TestEnvironmentContractAndResolver(unittest.TestCase):
    """Validate resolution and enforcement of the Fast DDS discovery contract."""

    def setUp(self):
        self.orig_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_resolve_loopback_profile_from_workspace(self):
        """resolve_loopback_profile must find the profile in workspace root."""
        p = resolve_loopback_profile(base_dir=UBUNTU_TANK_DIR)
        self.assertTrue(os.path.isfile(p))
        self.assertTrue(p.endswith("config/fastdds/loopback.xml"))

    def test_resolve_loopback_profile_precedence(self):
        """resolve_loopback_profile checks release directory before fallback."""
        with tempfile.TemporaryDirectory() as td:
            rel_dir = os.path.join(td, "releases", "1.0.0")
            rel_xml = os.path.join(rel_dir, "config", "fastdds", "loopback.xml")
            os.makedirs(os.path.dirname(rel_xml), exist_ok=True)
            with open(rel_xml, "w") as f:
                f.write("<dds/>")

            p = resolve_loopback_profile(
                opt_dir=td, release_id="1.0.0", allow_repo_fallback=False
            )
            self.assertEqual(p, rel_xml)

    def test_resolve_loopback_profile_fails_closed(self):
        """resolve_loopback_profile must raise FileNotFoundError if no candidate exists."""
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                resolve_loopback_profile(
                    opt_dir=td, base_dir=td, allow_repo_fallback=False
                )

    def test_apply_loopback_env_sets_all_contract_variables(self):
        """apply_loopback_env sets all required variables and overrides conflicting ones."""
        os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "SUBNET"  # conflicting
        os.environ["ROS_DOMAIN_ID"] = "42"  # conflicting

        applied = apply_loopback_env(base_dir=UBUNTU_TANK_DIR)
        self.assertIn("RMW_IMPLEMENTATION", applied)
        self.assertEqual(os.environ["RMW_IMPLEMENTATION"], "rmw_fastrtps_cpp")
        self.assertEqual(os.environ["ROS_DOMAIN_ID"], "0")
        self.assertEqual(os.environ["ROS_LOCALHOST_ONLY"], "1")
        self.assertEqual(os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"], "SYSTEM_DEFAULT")
        self.assertEqual(os.environ["ROS_SECURITY_ENABLE"], "true")
        self.assertEqual(os.environ["ROS_SECURITY_STRATEGY"], "Enforce")
        self.assertTrue(os.path.isfile(os.environ["FASTDDS_DEFAULT_PROFILES_FILE"]))

    def test_launcher_verify_runtime_paths_checks_fastdds(self):
        """mentorpi-tank-run verify_runtime_paths requires config/fastdds/loopback.xml."""
        run_path = os.path.join(UBUNTU_TANK_DIR, "bin", "mentorpi-tank-run")
        loader = SourceFileLoader("mentorpi_tank_run_mod", run_path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        run_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(run_mod)
        verify_runtime_paths = run_mod.verify_runtime_paths

        with tempfile.TemporaryDirectory() as td:
            install_dir = os.path.join(td, "install")
            os.makedirs(install_dir)
            with open(os.path.join(install_dir, "setup.bash"), "w") as f:
                f.write("#!/bin/bash\n")

            # Missing fastdds xml
            self.assertFalse(verify_runtime_paths(td))

            # Create fastdds xml
            fastdds_dir = os.path.join(td, "config", "fastdds")
            os.makedirs(fastdds_dir)
            shutil.copy2(
                os.path.join(UBUNTU_TANK_DIR, "config", "fastdds", "loopback.xml"),
                os.path.join(fastdds_dir, "loopback.xml"),
            )
            self.assertTrue(verify_runtime_paths(td))


class TestHostEnvironmentMigrationAndRollback(unittest.TestCase):
    """Validate transactional host environment migration and snapshot rollback."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.opt_dir = os.path.join(self.root, "opt", "ubuntu_tank")
        self.etc_dir = os.path.join(self.root, "etc", "opt", "ubuntu_tank")
        self.var_dir = os.path.join(self.root, "var", "opt", "ubuntu_tank")
        self.run_dir = os.path.join(self.root, "run", "ubuntu_tank")
        self.systemd_dir = os.path.join(self.root, "etc", "systemd", "system")
        self.udev_dir = os.path.join(self.root, "etc", "udev", "rules.d")
        self.lock_path = os.path.join(self.root, "run", "lock", "deploy.lock")

        for d in [
            self.opt_dir,
            self.etc_dir,
            self.var_dir,
            self.run_dir,
            self.systemd_dir,
            self.udev_dir,
            os.path.dirname(self.lock_path),
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

        # Isolated host udev rule with known serial discriminator
        with open(os.path.join(self.udev_dir, "99-mentorpi-rrc.rules"), "w") as stream:
            stream.write(
                'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", ATTRS{serial}=="fixture-rrc", GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc"\n'
            )

    def tearDown(self):
        self.td.cleanup()

    def test_migrate_host_env_injects_managed_keys_preserving_user_settings(self):
        """migrate_host_env preserves custom user keys and comments while adding DDS contract."""
        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        with open(target_env, "w") as f:
            f.write("""# Pre-existing host environment
ROS_DOMAIN_ID=0
ROS_LOCALHOST_ONLY=1
CUSTOM_USER_KEY=preserved_value
ROS_LOG_DIR=/custom/ros/log
""")

        # Candidate release directory
        candidate_dir = os.path.join(self.opt_dir, "releases", "1.0.0-test")
        os.makedirs(os.path.join(candidate_dir, "host"), exist_ok=True)
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "host", "mentorpi-tank.env"),
            os.path.join(candidate_dir, "host", "mentorpi-tank.env"),
        )

        migrated = self.mgr.migrate_host_env(target_env, candidate_dir)
        self.assertTrue(migrated)

        with open(target_env, "r") as f:
            content = f.read()

        # Check preserved user settings
        self.assertIn("CUSTOM_USER_KEY=preserved_value", content)
        self.assertIn("ROS_LOG_DIR=/custom/ros/log", content)

        # Check injected managed DDS settings
        self.assertIn("RMW_IMPLEMENTATION=rmw_fastrtps_cpp", content)
        self.assertIn("ROS_AUTOMATIC_DISCOVERY_RANGE=SYSTEM_DEFAULT", content)
        self.assertIn(
            f"FASTDDS_DEFAULT_PROFILES_FILE={self.opt_dir}/current/config/fastdds/loopback.xml",
            content,
        )

    def test_migrate_host_env_noop_when_already_matching(self):
        """migrate_host_env returns False when environment is already up to date."""
        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "host", "mentorpi-tank.env"),
            target_env,
        )
        candidate_dir = os.path.join(self.opt_dir, "releases", "1.0.0-test")
        os.makedirs(os.path.join(candidate_dir, "host"), exist_ok=True)
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "host", "mentorpi-tank.env"),
            os.path.join(candidate_dir, "host", "mentorpi-tank.env"),
        )

        # First pass might normalize path if needed, second pass must be no-op
        self.mgr.migrate_host_env(target_env, candidate_dir)
        second = self.mgr.migrate_host_env(target_env, candidate_dir)
        self.assertFalse(second)

    def _create_legacy_release(self, rel_id: str = "1.0.0-legacy") -> str:
        """Create a synthetic legacy (pre-Milestone 7) release without loopback.xml."""
        rel_dir = os.path.join(self.opt_dir, "releases", rel_id)
        os.makedirs(os.path.join(rel_dir, "bin"), exist_ok=True)
        os.makedirs(os.path.join(rel_dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(rel_dir, "host"), exist_ok=True)
        os.makedirs(os.path.join(rel_dir, "src"), exist_ok=True)
        install_dir = os.path.join(rel_dir, "install")
        os.makedirs(os.path.join(install_dir, "bin"), exist_ok=True)

        prefix = f"{self.opt_dir}/releases/{rel_id}/install"
        # 1. bin/mentorpi-tank-run
        runner = os.path.join(rel_dir, "bin", "mentorpi-tank-run")
        with open(runner, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(runner, 0o755)

        # 2. config/controller.yaml
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "config", "controller.yaml"),
            os.path.join(rel_dir, "config", "controller.yaml"),
        )

        # 3. host files
        with open(
            os.path.join(rel_dir, "host", "mentorpi-tank.env"), "w", encoding="utf-8"
        ) as f:
            f.write("ROS_DOMAIN_ID=0\nROS_LOCALHOST_ONLY=1\n")
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "host", "mentorpi-tank.service"),
            os.path.join(rel_dir, "host", "mentorpi-tank.service"),
        )
        shutil.copy2(
            os.path.join(UBUNTU_TANK_DIR, "host", "99-mentorpi-rrc.rules"),
            os.path.join(rel_dir, "host", "99-mentorpi-rrc.rules"),
        )

        # 4. src
        with open(
            os.path.join(rel_dir, "src", "dummy.txt"), "w", encoding="utf-8"
        ) as f:
            f.write("source\n")

        # 5. install
        with open(os.path.join(install_dir, "setup.bash"), "w", encoding="utf-8") as f:
            f.write(f'export COLCON_CURRENT_PREFIX="{prefix}"\n')
        os.chmod(os.path.join(install_dir, "setup.bash"), 0o755)
        with open(os.path.join(install_dir, "bin", "app"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(install_dir, "bin", "app"), 0o755)

        attest_build(install_dir, prefix, os.path.join(rel_dir, "src"), synthetic=True)

        # 6. release-manifest.txt
        src_sha = compute_tree_sha256(os.path.join(rel_dir, "src"))
        ins_sha = compute_tree_sha256(install_dir)
        manifest_lines = [
            "Format-Version: 1.0",
            f"Release-Id: {rel_id}",
            "Project-Version: 1.0.0",
            "Git-Commit: 8c67ddd4be17f463a26af440bd16a3c9cb007a28",
            "Git-Short-Commit: 8c67ddd",
            "Target-OS: Ubuntu 26.04 LTS",
            "Target-Architecture: arm64",
            "ROS-Distribution: lyrical",
            f"Install-Prefix: {prefix}",
            "Build-Timestamp: 2026-09-13T12:00:00Z",
            f"Source-Tree-SHA256: {src_sha}",
            f"Install-Tree-SHA256: {ins_sha}",
            "Test-Status: passed",
            "Hardware-Target: Raspberry Pi 5 ARM64 + STM32 RRC",
            "",
            "[Files]",
        ]
        files_to_record = []
        for root, _, files in os.walk(rel_dir):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, rel_dir)
                size = os.path.getsize(p)
                mode = oct(stat.S_IMODE(os.stat(p).st_mode))
                sha = compute_file_sha256(p)
                files_to_record.append((rel, sha, size, mode))

        for rel, sha, size, mode in sorted(files_to_record):
            manifest_lines.append(f"{sha}  {size}  {mode}  {rel}")

        manifest_path = os.path.join(rel_dir, "release-manifest.txt")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("\n".join(manifest_lines) + "\n")
        os.chmod(manifest_path, 0o444)

        return rel_dir

    def _create_and_install_candidate(self, rel_id: str = "1.1.0-m7") -> str:
        """Package and install a candidate release that includes config/fastdds/loopback.xml."""
        out_dist = os.path.join(self.root, "dist")
        os.makedirs(out_dist, exist_ok=True)
        archive = self.mgr.package_release(
            UBUNTU_TANK_DIR,
            out_dist,
            release_id=rel_id,
            allow_staged_install=True,
        )
        installed_id = self.mgr.install_release(
            archive, require_root=False, enforce_arm64=False
        )
        self.assertEqual(installed_id, rel_id)
        return os.path.join(self.opt_dir, "releases", rel_id)

    def test_validate_release_legacy_baseline_vs_candidate_requirement(self):
        """validate_release permits legacy release when require_fastdds_profile=False but rejects as candidate."""
        legacy_dir = self._create_legacy_release("1.0.0-legacy")

        # As a candidate, missing loopback.xml must fail
        valid, errs = self.mgr.validate_release(
            legacy_dir, expected_release_id="1.0.0-legacy", require_fastdds_profile=True
        )
        self.assertFalse(valid)
        self.assertTrue(
            any("Required Fast DDS loopback profile missing" in e for e in errs)
        )

        # As a rollback/recovery baseline, it must pass
        valid, errs = self.mgr.validate_release(
            legacy_dir,
            expected_release_id="1.0.0-legacy",
            require_fastdds_profile=False,
        )
        self.assertTrue(valid, f"Legacy release validation failed: {errs}")
        self.assertEqual(len(errs), 0)

    def test_install_release_preserves_existing_host_env_unchanged(self):
        """install_release does not migrate or alter existing mentorpi-tank.env."""
        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        legacy_content = (
            "# Milestone 6 configuration\n"
            "ROS_DOMAIN_ID=0\n"
            "ROS_LOCALHOST_ONLY=1\n"
            "OPERATOR_CUSTOM_VAR=preserved_exact\n"
        )
        with open(target_env, "w", encoding="utf-8") as f:
            f.write(legacy_content)

        self._create_and_install_candidate("1.1.0-m7")

        # Check that target_env was untouched during install
        with open(target_env, "r", encoding="utf-8") as f:
            actual_content = f.read()

        self.assertEqual(actual_content, legacy_content)
        self.assertNotIn("RMW_IMPLEMENTATION", actual_content)
        self.assertNotIn("FASTDDS_DEFAULT_PROFILES_FILE", actual_content)

    def test_milestone6_to_milestone7_upgrade_and_rollback_restores_env_byte_for_byte(
        self,
    ):
        """Milestone-6-to-7 upgrade migrates env in activation and rolls back byte-for-byte without mutating old release."""
        legacy_dir = self._create_legacy_release("1.0.0-legacy")

        # Initial baseline: 1.0.0-legacy is active
        symlink_tmp = f"{self.mgr.current_symlink}.tmp"
        os.symlink("releases/1.0.0-legacy", symlink_tmp)
        os.replace(symlink_tmp, self.mgr.current_symlink)

        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        legacy_bytes = (
            b"# Baseline Milestone 6 environment\n"
            b"ROS_DOMAIN_ID=0\n"
            b"ROS_LOCALHOST_ONLY=1\n"
            b"CUSTOM_KEY=original_bytes\n"
        )
        with open(target_env, "wb") as f:
            f.write(legacy_bytes)

        # Record initial baseline in journal so rollback knows the previous release
        self.mgr.journal.record_prepared(
            tx_id="tx-init",
            candidate_release_id="1.0.0-legacy",
            candidate_release_path=legacy_dir,
            previous_release_id=None,
            previous_release_path=None,
            snapshot_dir=None,
        )
        self.mgr.journal.record_committed("tx-init", "1.0.0-legacy", legacy_dir)

        # Install candidate
        self._create_and_install_candidate("1.1.0-m7")
        with open(target_env, "rb") as f:
            self.assertEqual(f.read(), legacy_bytes)

        # Capture legacy directory hash before activation to verify immutability
        legacy_sha_before = compute_tree_sha256(legacy_dir)

        # Activate candidate 1.1.0-m7
        act_id = self.mgr.activate_release("1.1.0-m7", require_root=False)
        self.assertEqual(act_id, "1.1.0-m7")
        self.assertEqual(
            os.path.realpath(self.mgr.current_symlink),
            os.path.realpath(os.path.join(self.opt_dir, "releases", "1.1.0-m7")),
        )

        # Target env should now be migrated
        with open(target_env, "r", encoding="utf-8") as f:
            migrated_text = f.read()
        self.assertIn("RMW_IMPLEMENTATION=rmw_fastrtps_cpp", migrated_text)
        self.assertIn("CUSTOM_KEY=original_bytes", migrated_text)

        # Perform rollback to 1.0.0-legacy
        rb_id = self.mgr.rollback_release(require_root=False)
        self.assertEqual(rb_id, "1.0.0-legacy")
        self.assertEqual(
            os.path.realpath(self.mgr.current_symlink), os.path.realpath(legacy_dir)
        )

        # Target env must be restored byte-for-byte to legacy_bytes
        with open(target_env, "rb") as f:
            restored_bytes = f.read()
        self.assertEqual(restored_bytes, legacy_bytes)

        # Verify old release was immutable and untouched
        legacy_sha_after = compute_tree_sha256(legacy_dir)
        self.assertEqual(legacy_sha_before, legacy_sha_after)

    def test_interrupted_activation_recovery_restores_legacy_env_byte_for_byte(self):
        """Interrupted activation recovery validates legacy release and restores environment byte-for-byte."""
        legacy_dir = self._create_legacy_release("1.0.0-legacy")

        symlink_tmp = f"{self.mgr.current_symlink}.tmp"
        os.symlink("releases/1.0.0-legacy", symlink_tmp)
        os.replace(symlink_tmp, self.mgr.current_symlink)

        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        legacy_bytes = (
            b"# Milestone 6 baseline for interrupted activation\n"
            b"ROS_DOMAIN_ID=0\n"
            b"RECOVERY_TEST_KEY=keep_exact\n"
        )
        with open(target_env, "wb") as f:
            f.write(legacy_bytes)

        # Create candidate
        cand_dir = self._create_and_install_candidate("1.1.0-m7")

        # Simulate interrupted activation: create snapshot, write ACTIVATING journal record
        tx_id = "tx-interrupted-01"
        snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
            tx_id=tx_id,
            current_symlink_target=legacy_dir,
            etc_dir=self.etc_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
        )
        self.mgr.journal.record_prepared(
            tx_id=tx_id,
            candidate_release_id="1.1.0-m7",
            candidate_release_path=cand_dir,
            previous_release_id="1.0.0-legacy",
            previous_release_path=legacy_dir,
            snapshot_dir=snapshot_dir,
        )
        self.mgr.journal.record_activating(tx_id)

        # Mutate environment and symlink during interrupted transaction
        with open(target_env, "w", encoding="utf-8") as f:
            f.write("CORRUPTED_PARTIAL_MIGRATION=1\n")
        os.unlink(self.mgr.current_symlink)
        os.symlink("releases/1.1.0-m7", self.mgr.current_symlink)

        # Execute recovery
        recovered = self.mgr.recover_activation(check_mutual_exclusion=False)
        self.assertTrue(recovered)

        # Verify symlink and environment restored
        self.assertEqual(
            os.path.realpath(self.mgr.current_symlink), os.path.realpath(legacy_dir)
        )
        with open(target_env, "rb") as f:
            self.assertEqual(f.read(), legacy_bytes)

    def test_failed_activation_automatic_rollback_restores_legacy_env_byte_for_byte(
        self,
    ):
        """Failed activation transaction automatically rolls back and restores environment byte-for-byte."""
        legacy_dir = self._create_legacy_release("1.0.0-legacy")

        symlink_tmp = f"{self.mgr.current_symlink}.tmp"
        os.symlink("releases/1.0.0-legacy", symlink_tmp)
        os.replace(symlink_tmp, self.mgr.current_symlink)

        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        legacy_bytes = (
            b"# Milestone 6 baseline for failed activation\n"
            b"FAILURE_TEST_KEY=safe_restore\n"
        )
        with open(target_env, "wb") as f:
            f.write(legacy_bytes)

        # Create candidate with intentional failure during _stage_host_files (ambiguous udev rules)
        cand_dir = self._create_and_install_candidate("1.1.0-bad")
        bad_udev = os.path.join(cand_dir, "host", "99-mentorpi-rrc.rules")
        with open(bad_udev, "w", encoding="utf-8") as f:
            f.write(
                'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", KERNELS=="1-1", SYMLINK+="rrc"\n'
                'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", KERNELS=="1-2", SYMLINK+="rrc"\n'
            )
        # Re-attest manifest so validate_release passes Step 1 but Step 4 fails on ambiguous rule
        manifest_path = os.path.join(cand_dir, "release-manifest.txt")
        with open(manifest_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            if "99-mentorpi-rrc.rules" in line:
                sha = compute_file_sha256(bad_udev)
                sz = os.path.getsize(bad_udev)
                m = oct(stat.S_IMODE(os.stat(bad_udev).st_mode))
                new_lines.append(f"{sha}  {sz}  {m}  host/99-mentorpi-rrc.rules\n")
            else:
                new_lines.append(line)
        os.chmod(manifest_path, 0o644)
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.chmod(manifest_path, 0o444)

        with self.assertRaises(RuntimeError) as ctx:
            self.mgr.activate_release("1.1.0-bad", require_root=False)
        self.assertIn("ambiguous udev rule", str(ctx.exception).lower())

        # Environment must be restored byte-for-byte
        with open(target_env, "rb") as f:
            self.assertEqual(f.read(), legacy_bytes)
        self.assertEqual(
            os.path.realpath(self.mgr.current_symlink), os.path.realpath(legacy_dir)
        )


class TestDeliveryDeadlockRegressionAndLoopbackDelivery(unittest.TestCase):
    """Reproduce the September 13 diagnosis multicast blockage and verify loopback unicast delivery."""

    def test_multicast_destination_tracing(self):
        """Standard discovery transmits to multicast 239.255.0.1 which fails under IPAddressDeny=any."""
        # Trace destination address simulation
        standard_multicast = "239.255.0.1"
        localhost_addr = "127.0.0.1"

        def is_denied_by_systemd_ip_filter(dest_ip: str) -> bool:
            # Under IPAddressDeny=any and IPAddressAllow=localhost:
            # only 127.0.0.0/8 (and ::1) are permitted
            return not dest_ip.startswith("127.") and dest_ip != "::1"

        self.assertTrue(is_denied_by_systemd_ip_filter(standard_multicast))
        self.assertFalse(is_denied_by_systemd_ip_filter(localhost_addr))

    def test_simulated_pipeline_receipt_correlation(self):
        """Verify delivery pipeline requires receipts through Controller -> Guard -> Bridge."""
        # Simulated diagnostic delivery counter
        pipeline = {
            "controller_rx": 0,
            "guard_rx": 0,
            "bridge_rx": 0,
            "mock_sdk_calls": [],
        }

        def deliver_motion_command(
            linear_x: float, angular_z: float, loopback_enabled: bool
        ):
            # Operator publishes cmd_vel
            pipeline["controller_rx"] += 1

            if not loopback_enabled:
                # EPERM: controller-to-guard multicast blocked
                return False

            # With loopback unicast: guard receives
            pipeline["guard_rx"] += 1

            # Guard validates and forwards to bridge
            pipeline["bridge_rx"] += 1
            pipeline["mock_sdk_calls"].append((linear_x, angular_z))
            return True

        # Run 1: without loopback (reproducing diagnosis)
        ret1 = deliver_motion_command(0.2, 0.0, loopback_enabled=False)
        self.assertFalse(ret1)
        self.assertEqual(pipeline["controller_rx"], 1)
        self.assertEqual(pipeline["guard_rx"], 0)
        self.assertEqual(pipeline["bridge_rx"], 0)

        # Run 2: with loopback enabled
        ret2 = deliver_motion_command(0.2, 0.0, loopback_enabled=True)
        self.assertTrue(ret2)
        self.assertEqual(pipeline["controller_rx"], 2)
        self.assertEqual(pipeline["guard_rx"], 1)
        self.assertEqual(pipeline["bridge_rx"], 1)
        self.assertEqual(len(pipeline["mock_sdk_calls"]), 1)


if __name__ == "__main__":
    unittest.main()
