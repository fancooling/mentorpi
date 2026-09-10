# test_install_workflow.py - Unit tests for Milestone 2 host preflight, mutual exclusion, and lock verification
import os
import sys
import subprocess
import tempfile
import unittest
import yaml

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
TANK_DIR = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(TANK_DIR, "scripts")
CHECK_HOST_BIN = os.path.join(SCRIPTS_DIR, "check_host.sh")
INSTALL_ROS2_BIN = os.path.join(SCRIPTS_DIR, "install_ros2.sh")
LOCK_FILE = os.path.join(TANK_DIR, "versions.lock")


class TestHostPreflight(unittest.TestCase):
    """Tests for check_host.sh preflight and environment validation."""

    def test_mock_target_success(self):
        """Preflight succeeds when mocked target environment is clean Pi 5 ARM64."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Expected 0, got {res.returncode}")
        self.assertIn("PASS: Host preflight verification complete and accepted.", res.stdout)

    def test_reject_wrong_architecture(self):
        """Preflight must fail closed when architecture is not arm64/aarch64."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_ARCH"] = "x86_64"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Checking architecture (arm64 / aarch64)... FAIL", res.stdout)
        self.assertIn("Expected dpkg arm64 / kernel aarch64", res.stderr)

    def test_reject_wrong_os_version(self):
        """Preflight must fail closed when OS is not Ubuntu 26.04."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_OS_VER"] = "24.04"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Checking operating system (Ubuntu 26.04 LTS)... FAIL", res.stdout)
        self.assertIn("Expected Ubuntu 26.04 (Resolute)", res.stderr)

    def test_reject_outdated_eeprom(self):
        """Preflight must fail closed when Raspberry Pi EEPROM is older than 2024-05-17."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_EEPROM"] = "1900-01-01"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Checking Raspberry Pi EEPROM firmware date... FAIL", res.stdout)
        self.assertIn("is older than required minimum", res.stderr)

    def test_reject_malformed_eeprom_garbage(self):
        """Preflight must fail closed when EEPROM value is 'garbage' or malformed."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_EEPROM"] = "garbage"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FAIL", res.stdout)
        self.assertIn("Malformed or unparseable EEPROM firmware release date", res.stderr)

    def test_accept_authentic_eeprom_current_format(self):
        """Preflight accepts rpi-eeprom-update's timestamp plus epoch format."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_EEPROM"] = "Thu 17 Apr 11:24:28 UTC 2025 (1744889068)"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("1744889068 >= 2024-05-17", res.stdout)

    def test_reject_non_pi5_model(self):
        """Preflight must fail closed when hardware model is not Raspberry Pi 5."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_MODEL"] = "Raspberry Pi 4 Model B"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Checking hardware model (Raspberry Pi 5)... FAIL", res.stdout)
        self.assertIn("Target hardware is not a Raspberry Pi 5", res.stderr)

    def test_reject_missing_fuser_with_serial_device(self):
        """Preflight must fail closed when serial device is present but fuser is missing."""
        with tempfile.TemporaryDirectory() as tmp_bin:
            for b_dir in ["/usr/bin", "/bin"]:
                if os.path.exists(b_dir):
                    for fname in os.listdir(b_dir):
                        if fname != "fuser" and not os.path.exists(os.path.join(tmp_bin, fname)):
                            try:
                                os.symlink(os.path.join(b_dir, fname), os.path.join(tmp_bin, fname))
                            except OSError:
                                pass

            env = os.environ.copy()
            env["PATH"] = tmp_bin
            env["UBUNTU_TANK_MOCK_TARGET"] = "1"
            env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
            env["UBUNTU_TANK_MOCK_SERIAL_DEV"] = "/dev/null"
            res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("is present but 'fuser' command is not available to verify exclusivity. Failing closed.", res.stderr)

    def test_non_mock_process_table_scan_clean(self):
        """Non-mocked check_host execution in repo checkout must not flag itself or ancestors."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("UBUNTU_TANK_MOCK_")}
        res = subprocess.run([CHECK_HOST_BIN, "--json"], env=clean_env, capture_output=True, text=True)
        import json
        try:
            data = json.loads(res.stdout)
            mex = data.get("mutual_exclusion", {})
            self.assertNotIn("Conflicting ROS/hardware owner process running", res.stderr)
            self.assertNotIn("mentorpi", mex.get("conflicting_processes", []))
        except json.JSONDecodeError:
            self.fail(f"check_host --json output was not valid JSON:\n{res.stdout}\n{res.stderr}")

    def test_preflight_json_format(self):
        """Preflight --json must emit valid JSON schema with expected fields."""
        import json
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([CHECK_HOST_BIN, "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(data["overall"], "PASS")
        self.assertIn("model", data)
        self.assertIn("eeprom", data)

    def test_preflight_json_escapes_host_strings(self):
        """JSON mode must serialize quotes, backslashes, and newlines safely."""
        import json

        unusual_model = 'Raspberry "Pi"\\Five\nModel'
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_MODEL"] = unusual_model
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run(
            [CHECK_HOST_BIN, "--json"], env=env, capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertIn(unusual_model, data["model"]["message"])
        self.assertIn("mutual_exclusion", data)


class TestMutualExclusion(unittest.TestCase):
    """Tests for mutual exclusion checks (Docker containers and conflicting units)."""

    def test_reject_factory_mentorpi_container(self):
        """Must reject presence of factory MentorPi container."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "MentorPi other_container"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Conflicting Docker container found matching 'MentorPi'", res.stderr)

    def test_reject_sidecar_mentorpifan_container(self):
        """Must reject presence of sidecar MentorPiFan container."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "MentorPiFan"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Conflicting Docker container found matching 'MentorPiFan'", res.stderr)

    def test_reject_replacement_candidate_container(self):
        """Must reject presence of replacement container candidate."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "runtime-core-candidate"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Conflicting Docker container found matching 'runtime-core'", res.stderr)

    def test_reject_unreadable_docker_daemon(self):
        """Must fail closed when Docker is installed but daemon cannot be inspected."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_FAIL"] = "1"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Docker is installed but daemon/containers cannot be inspected. Failing closed.", res.stderr)

    def test_reject_device_contention(self):
        """Must fail closed when a process holds /dev/rrc open."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_SERIAL_HOLDER"] = "99999"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("hold /dev/rrc open", res.stderr)

    def test_reject_non_char_serial_device(self):
        """Must fail closed when /dev/rrc is not a character device."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        env["UBUNTU_TANK_MOCK_SERIAL_DEV"] = "/etc/hosts"
        res = subprocess.run([CHECK_HOST_BIN], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("exists but is not a character device", res.stderr)


class TestVerifyLock(unittest.TestCase):
    """Tests for install_ros2.sh verify-lock."""

    def test_authoritative_lock_passes(self):
        """The committed lock must pass structural verification."""
        res = subprocess.run([INSTALL_ROS2_BIN, "verify-lock"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"verify-lock failed")
        self.assertIn("PASS: versions.lock verified successfully.", res.stdout)
        self.assertIn("Transitive closure status: complete", res.stdout)

    def test_live_install_requires_complete_transitive_lock(self):
        """Live package mutation must reject a direct-only lock."""
        with open(LOCK_FILE, "r", encoding="utf-8") as source:
            lock_text = source.read().replace(
                "closure_status: complete", "closure_status: direct-only", 1
            )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".lock", delete=False) as tmp:
            tmp.write(lock_text)
            tmp_path = tmp.name

        try:
            script = f'LOCK_FILE="{tmp_path}"; source "{INSTALL_ROS2_BIN}"; require_complete_package_lock'
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("transitive closure is 'direct-only'", res.stderr)
        finally:
            os.remove(tmp_path)

    def test_complete_transitive_lock_passes_live_gate(self):
        """A reviewed complete lock state must pass the live mutation gate."""
        script = f'source "{INSTALL_ROS2_BIN}"; require_complete_package_lock'
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_reject_tampered_missing_format_version(self):
        """Tampered lockfile missing format_version must fail."""
        with open(LOCK_FILE, "r") as f:
            data = yaml.safe_load(f)
        data["meta"]["format_version"] = "0.9.0"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            yaml.dump(data, tmp)
            tmp_path = tmp.name

        try:
            env = os.environ.copy()
            env["LOCK_FILE"] = tmp_path
            res = subprocess.run([INSTALL_ROS2_BIN, "verify-lock"], env=env, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Invalid format_version", res.stderr)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class TestDryRunCommands(unittest.TestCase):
    """Tests for dry-run execution of installation workflows."""

    def test_install_ros_dry_run(self):
        """install-ros --dry-run prints plan without mutating host."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([INSTALL_ROS2_BIN, "install-ros", "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"install-ros dry-run failed")
        self.assertIn("[DRY-RUN] Verified lock parameters:", res.stdout)
        self.assertIn("PASS: Dry-run install-ros completed successfully.", res.stdout)

    def test_install_ros_dry_run_with_valid_candidates(self):
        """install-ros --dry-run --candidates <file> validates candidate solver closure."""
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            lock = yaml.safe_load(f)
        lines = []
        for p in lock["packages"]:
            lines.append(f"{p['name']} {p['version']} {p['architecture']} {p['repository']} {p['sha256']}")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            cand_path = tf.name

        try:
            env = os.environ.copy()
            env["UBUNTU_TANK_MOCK_TARGET"] = "1"
            env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
            res = subprocess.run(
                [INSTALL_ROS2_BIN, "install-ros", "--dry-run", "--candidates", cand_path],
                env=env, capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 0, f"Failed with {res.stderr}")
            self.assertIn("Exact solver closure verified", res.stdout)
            self.assertIn("PASS: Dry-run install-ros completed successfully.", res.stdout)
        finally:
            if os.path.exists(cand_path):
                os.remove(cand_path)

    def test_install_ros_dry_run_with_tampered_candidates(self):
        """install-ros --dry-run --candidates <file> rejects candidate closure with checksum mismatch."""
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            lock = yaml.safe_load(f)
        lines = []
        for i, p in enumerate(lock["packages"]):
            sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" if i == 0 else p["sha256"]
            lines.append(f"{p['name']} {p['version']} {p['architecture']} {p['repository']} {sha}")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            cand_path = tf.name

        try:
            env = os.environ.copy()
            env["UBUNTU_TANK_MOCK_TARGET"] = "1"
            env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
            res = subprocess.run(
                [INSTALL_ROS2_BIN, "install-ros", "--dry-run", "--candidates", cand_path],
                env=env, capture_output=True, text=True
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Checksum mismatch for package", res.stderr)
        finally:
            if os.path.exists(cand_path):
                os.remove(cand_path)

    def test_prepare_host_dry_run(self):
        """prepare-host --dry-run prints plan without mutating host."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([INSTALL_ROS2_BIN, "prepare-host", "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"prepare-host dry-run failed")
        self.assertIn("[DRY-RUN] Would record pre-upgrade host baseline", res.stdout)
        self.assertIn("PASS: Dry-run prepare-host completed successfully.", res.stdout)

    def test_install_deps_dry_run(self):
        """install-deps --dry-run prints plan without mutating host."""
        env = os.environ.copy()
        env["UBUNTU_TANK_MOCK_TARGET"] = "1"
        env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
        res = subprocess.run([INSTALL_ROS2_BIN, "install-deps", "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"install-deps dry-run failed")
        self.assertIn("[DRY-RUN] Would fetch rosdep snapshot sources", res.stdout)
        self.assertIn("PASS: Dry-run install-deps completed successfully.", res.stdout)


class TestSecurityAndCliGuards(unittest.TestCase):
    """Tests that mock target cannot authorize live mutations and CLI options are strictly validated."""

    def test_reject_all_mock_and_override_variables_without_dry_run(self):
        """Any mock or test override variable must be rejected for live mutations."""
        override_cases = [
            ("UBUNTU_TANK_MOCK_TARGET", "1"),
            ("UBUNTU_TANK_MOCK_MODEL", "Raspberry Pi 5"),
            ("UBUNTU_TANK_MOCK_ARCH", "arm64"),
            ("UBUNTU_TANK_MOCK_OS_VER", "26.04"),
            ("UBUNTU_TANK_MOCK_EEPROM", "2025-01-01"),
            ("UBUNTU_TANK_MOCK_DOCKER_PS", "none"),
            ("UBUNTU_TANK_MOCK_SERIAL_HOLDER", "1234"),
            ("UBUNTU_TANK_MOCK_SERIAL_DEV", "/dev/null"),
            ("UBUNTU_TANK_MOCK_REBOOT_FILE", "/tmp/mock_reboot"),
            ("UBUNTU_TANK_LOCK_DIR", "/tmp/noncanonical_lock"),
            ("LOCK_FILE", "/tmp/noncanonical_versions.lock"),
        ]

        for subcmd in ["prepare-host", "install-ros", "install-deps"]:
            for var_name, var_val in override_cases:
                env = os.environ.copy()
                for k in list(env.keys()):
                    if k.startswith("UBUNTU_TANK_MOCK_") or k in ("UBUNTU_TANK_LOCK_DIR", "LOCK_FILE"):
                        del env[k]
                env[var_name] = var_val
                res = subprocess.run([INSTALL_ROS2_BIN, subcmd], env=env, capture_output=True, text=True)
                self.assertNotEqual(res.returncode, 0, f"Expected rejection for {subcmd} with {var_name}")
                self.assertIn("SECURITY ERROR: Test override variables", res.stderr)

    def test_help_options_do_not_mutate(self):
        """-h and --help must display help text and exit 0 without mutating."""
        for subcmd in ["prepare-host", "install-ros", "install-deps"]:
            for flag in ["-h", "--help"]:
                res = subprocess.run([INSTALL_ROS2_BIN, subcmd, flag], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Failed for {subcmd} {flag}")
                self.assertIn(f"Usage: ./deploy.sh {subcmd}", res.stdout)

    def test_unknown_options_fail(self):
        """Unknown options must fail immediately with non-zero exit."""
        for subcmd in ["prepare-host", "install-ros", "install-deps"]:
            res = subprocess.run([INSTALL_ROS2_BIN, subcmd, "--invalid-flag-12345"], capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0, f"Expected failure for {subcmd} with unknown flag")


class TestDeploymentLock(unittest.TestCase):
    """Tests for host-global deployment lock mutual exclusion."""

    def test_lock_contention_blocks_concurrent_execution(self):
        """Conflicting operations cannot execute when deployment lock is already held."""
        import fcntl
        with tempfile.TemporaryDirectory() as tmp_lock_dir:
            lock_path = os.path.join(tmp_lock_dir, "deploy.lock")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                env = os.environ.copy()
                env["UBUNTU_TANK_MOCK_TARGET"] = "1"
                env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
                env["UBUNTU_TANK_LOCK_DIR"] = tmp_lock_dir

                res = subprocess.run(
                    [INSTALL_ROS2_BIN, "prepare-host", "--dry-run"],
                    env=env, capture_output=True, text=True
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("FAIL: Could not acquire deployment lock", res.stderr)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            # Once unlocked, dry-run succeeds
            res_after = subprocess.run(
                [INSTALL_ROS2_BIN, "prepare-host", "--dry-run"],
                env=env, capture_output=True, text=True
            )
            self.assertEqual(res_after.returncode, 0)
            self.assertIn("PASS: Dry-run prepare-host completed successfully.", res_after.stdout)


class TestAptRecoveryWorkflow(unittest.TestCase):
    """Tests for apt state recovery and deb artifact preservation."""

    def test_recover_apt_state_preserves_deb_and_repairs(self):
        """recover_apt_state must repair package database without removing /tmp/ros2-apt-source.deb."""
        with tempfile.NamedTemporaryFile(suffix=".deb", delete=False) as dummy_deb:
            dummy_deb.write(b"dummy deb content")
            dummy_deb_path = dummy_deb.name

        try:
            with tempfile.TemporaryDirectory() as tmp_bin:
                dpkg_log = os.path.join(tmp_bin, "dpkg.log")
                apt_log = os.path.join(tmp_bin, "apt.log")
                dpkg_mock = os.path.join(tmp_bin, "dpkg")
                with open(dpkg_mock, "w") as f:
                    f.write(f"#!/bin/sh\necho dpkg $@ >> '{dpkg_log}'\nexit 0\n")
                os.chmod(dpkg_mock, 0o755)
                apt_mock = os.path.join(tmp_bin, "apt-get")
                with open(apt_mock, "w") as f:
                    f.write(f"#!/bin/sh\necho apt-get $@ >> '{apt_log}'\nexit 0\n")
                os.chmod(apt_mock, 0o755)

                test_script = f"""
                set -euo pipefail
                . "{INSTALL_ROS2_BIN}"
                test -f "{dummy_deb_path}"
                recover_apt_state
                test -f "{dummy_deb_path}"
                echo "RECOVERY_OK"
                """

                env = os.environ.copy()
                env["PATH"] = f"{tmp_bin}:{env['PATH']}"
                env["UBUNTU_TANK_MOCK_NO_SUDO"] = "1"
                res = subprocess.run(
                    ["bash", "-c", test_script],
                    env=env, capture_output=True, text=True
                )
                self.assertEqual(res.returncode, 0, f"Failed with {res.stderr}")
                self.assertIn("RECOVERY_OK", res.stdout)
                self.assertTrue(os.path.exists(dummy_deb_path))

                with open(dpkg_log, "r") as f:
                    self.assertIn("--configure -a", f.read())
                with open(apt_log, "r") as f:
                    self.assertIn("--fix-broken install", f.read())
        finally:
            if os.path.exists(dummy_deb_path):
                os.remove(dummy_deb_path)


class TestRebootSequence(unittest.TestCase):
    """Tests for post-reboot acceptance transition and kernel check."""

    def test_prepare_host_pending_reboot_dry_run(self):
        """prepare-host --dry-run exits with code 2 when reboot is pending."""
        with tempfile.NamedTemporaryFile(delete=False) as rf:
            reboot_file = rf.name

        try:
            env = os.environ.copy()
            env["UBUNTU_TANK_MOCK_TARGET"] = "1"
            env["UBUNTU_TANK_MOCK_DOCKER_PS"] = "none"
            env["UBUNTU_TANK_MOCK_REBOOT_FILE"] = reboot_file

            res = subprocess.run(
                [INSTALL_ROS2_BIN, "prepare-host", "--dry-run"],
                env=env, capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("Host reboot required", res.stdout)
        finally:
            if os.path.exists(reboot_file):
                os.remove(reboot_file)

    def test_install_ros_rejects_kernel_mismatch(self):
        """install-ros baseline validation rejects kernel mismatch with exit code 2."""
        curr_arch = subprocess.check_output("dpkg --print-architecture 2>/dev/null || uname -m", shell=True, text=True).strip()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as bf:
            bf.write(f"""# MentorPi Host Baseline - Accepted Ubuntu Baseline Snapshot
timestamp: 2026-09-08T00:00:00Z
hostname: mentorpi-test
os_release:
NAME="Ubuntu"
VERSION_ID="26.04"
kernel: 1.0.0-dummy-old-kernel
architecture: {curr_arch}
reboot_pending: false
apt_sources:
deb http://archive.ubuntu.com/ubuntu resolute main
installed_packages:
ii  base-files 13.0ubuntu1 arm64
""")
            baseline_path = bf.name

        try:
            test_script = f"""
            set -euo pipefail
            . "{INSTALL_ROS2_BIN}"
            validate_host_baseline "{baseline_path}" "true"
            """
            res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 2)
            self.assertIn("differs from recorded baseline kernel", res.stderr)
        finally:
            if os.path.exists(baseline_path):
                os.remove(baseline_path)

    def test_validate_host_baseline_live_permissions(self):
        """validate_host_baseline in live mode (dry_run=false) must enforce 0644/0600 root ownership and reject writable modes."""
        curr_arch = subprocess.check_output("dpkg --print-architecture 2>/dev/null || uname -m", shell=True, text=True).strip()
        curr_kern = subprocess.check_output("uname -r", shell=True, text=True).strip()
        curr_os = subprocess.check_output("grep -E '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '\"'", shell=True, text=True).strip()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as bf:
            bf.write(f"""# MentorPi Host Baseline - Accepted Ubuntu Baseline Snapshot
timestamp: 2026-09-08T00:00:00Z
hostname: mentorpi-test
os_release:
NAME="Ubuntu"
VERSION_ID="{curr_os}"
kernel: {curr_kern}
architecture: {curr_arch}
reboot_pending: false
apt_sources:
deb http://archive.ubuntu.com/ubuntu resolute main
installed_packages:
ii  base-files 13.0ubuntu1 arm64
""")
            baseline_path = bf.name

        try:
            cases = [
                ('644', '0', '0', True, '0644 root:root must pass'),
                ('600', '0', '0', True, '0600 root:root must pass'),
                ('646', '0', '0', False, '0646 world-writable must fail'),
                ('777', '0', '0', False, '0777 world-writable must fail'),
                ('664', '0', '1000', False, '0664 non-root group must fail'),
                ('644', '1000', '1000', False, 'non-root owner must fail'),
            ]
            for mode, uid, gid, expect_pass, label in cases:
                with self.subTest(label=label):
                    test_script = f"""
                    set -euo pipefail
                    . "{INSTALL_ROS2_BIN}"
                    stat() {{
                        if [ "$1" = "-c" ]; then
                            case "$2" in
                                %u) echo "{uid}" ;;
                                %g) echo "{gid}" ;;
                                %a) echo "{mode}" ;;
                            esac
                        fi
                    }}
                    validate_host_baseline "{baseline_path}" "false"
                    """
                    res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
                    if expect_pass:
                        self.assertEqual(res.returncode, 0, f"{label} failed: {res.stderr}")
                    else:
                        self.assertNotEqual(res.returncode, 0, f"{label} unexpectedly passed")
        finally:
            if os.path.exists(baseline_path):
                os.remove(baseline_path)


class TestUpstreamInputsVerification(unittest.TestCase):
    """Non-mutating tests that verify pinned upstream artifacts exist and match cryptographic hashes."""

    def test_upstream_inputs_exist_and_match_hashes(self):
        """Verify ros2-apt-source deb and all 4 rosdep snapshot files exist upstream and match SHA256."""
        import urllib.request
        import hashlib

        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 1. ros2-apt-source deb
        apt_source = data["ros_apt_source"]
        deb_url = apt_source["url"]
        expected_deb_sha = apt_source["sha256"]

        req1 = urllib.request.Request(deb_url, headers={"User-Agent": "MentorPi-Test/1.0"})
        with urllib.request.urlopen(req1, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            deb_bytes = resp.read()
            actual_deb_sha = hashlib.sha256(deb_bytes).hexdigest()
            self.assertEqual(actual_deb_sha, expected_deb_sha, f"Hash mismatch for {deb_url}")

        # 2. All 4 rosdep snapshot source files
        rosdep_sources = data["rosdep_sources"]
        for key in ["index_v4", "base", "python", "ruby"]:
            source_entry = rosdep_sources[key]
            s_url = source_entry["url"]
            expected_s_sha = source_entry["sha256"]

            req = urllib.request.Request(s_url, headers={"User-Agent": "MentorPi-Test/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.assertEqual(resp.status, 200)
                s_bytes = resp.read()
                actual_s_sha = hashlib.sha256(s_bytes).hexdigest()
                self.assertEqual(actual_s_sha, expected_s_sha, f"Hash mismatch for rosdep source '{key}' ({s_url})")


class TestExternalWorkingDirectory(unittest.TestCase):
    """Tests that deployment and test scripts execute cleanly when invoked from outside repo root."""

    def test_dependency_closure_runs_from_tmp(self):
        """Running test_dependency_closure.sh from /tmp must pass without path errors."""
        script_path = os.path.join(TANK_DIR, "tests", "test_dependency_closure.sh")
        res = subprocess.run([script_path], cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Failed with output:\n{res.stdout}\n{res.stderr}")
        self.assertIn("Dependency Closure and Lockfile Verification Gate PASSED!", res.stdout)


if __name__ == "__main__":
    unittest.main()
