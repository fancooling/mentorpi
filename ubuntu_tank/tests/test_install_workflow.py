"""
Unit test suite for lock verification, upstream inputs, process table scanning, and CLI option guards.
"""

import json
import os
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


class TestHostPreflightProcessSanity(unittest.TestCase):
    """Sanity checks for check_host.sh process table scanning against live environment."""

    def test_non_mock_process_table_scan_clean(self):
        """Non-mocked check_host execution in repo checkout must not flag itself or ancestors."""
        clean_env = {
            k: v for k, v in os.environ.items() if not k.startswith("UBUNTU_TANK_MOCK_")
        }
        res = subprocess.run(
            [CHECK_HOST_BIN, "--json"], env=clean_env, capture_output=True, text=True
        )

        try:
            data = json.loads(res.stdout)
            mex = data.get("mutual_exclusion", {})
            self.assertNotIn(
                "Conflicting ROS/hardware owner process running", res.stderr
            )
            self.assertNotIn("mentorpi", mex.get("conflicting_processes", []))
        except json.JSONDecodeError:
            self.fail(
                f"check_host --json output was not valid JSON:\n{res.stdout}\n{res.stderr}"
            )


class TestVerifyLock(unittest.TestCase):
    """Tests for install_ros2.sh verify-lock."""

    def test_authoritative_lock_passes(self):
        """The committed lock must pass structural verification."""
        res = subprocess.run(
            [INSTALL_ROS2_BIN, "verify-lock"], capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0, "verify-lock failed")
        self.assertIn("PASS: versions.lock verified successfully.", res.stdout)
        self.assertIn("Transitive closure status: complete", res.stdout)

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
            res = subprocess.run(
                [INSTALL_ROS2_BIN, "verify-lock"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Invalid format_version", res.stderr)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class TestUpstreamInputsVerification(unittest.TestCase):
    """Non-mutating tests that verify pinned upstream artifacts exist and match cryptographic hashes."""

    def test_upstream_inputs_exist_and_match_hashes(self):
        """Verify ros2-apt-source deb and all 4 rosdep snapshot files exist upstream and match SHA256."""
        import hashlib
        import urllib.request

        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 1. ros2-apt-source deb
        apt_source = data["ros_apt_source"]
        deb_url = apt_source["url"]
        expected_deb_sha = apt_source["sha256"]

        req1 = urllib.request.Request(
            deb_url, headers={"User-Agent": "MentorPi-Test/1.0"}
        )
        with urllib.request.urlopen(req1, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            deb_bytes = resp.read()
            actual_deb_sha = hashlib.sha256(deb_bytes).hexdigest()
            self.assertEqual(
                actual_deb_sha, expected_deb_sha, f"Hash mismatch for {deb_url}"
            )

        # 2. All 4 rosdep snapshot source files
        rosdep_sources = data["rosdep_sources"]
        for key in ["index_v4", "base", "python", "ruby"]:
            source_entry = rosdep_sources[key]
            s_url = source_entry["url"]
            expected_s_sha = source_entry["sha256"]

            req = urllib.request.Request(
                s_url, headers={"User-Agent": "MentorPi-Test/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.assertEqual(resp.status, 200)
                s_bytes = resp.read()
                actual_s_sha = hashlib.sha256(s_bytes).hexdigest()
                self.assertEqual(
                    actual_s_sha,
                    expected_s_sha,
                    f"Hash mismatch for rosdep source '{key}' ({s_url})",
                )


class TestExternalWorkingDirectory(unittest.TestCase):
    """Tests that deployment and test scripts execute cleanly when invoked from outside repo root."""

    def test_dependency_closure_runs_from_tmp(self):
        """Running test_dependency_closure.sh from /tmp must pass without path errors."""
        script_path = os.path.join(TANK_DIR, "tests", "test_dependency_closure.sh")
        res = subprocess.run([script_path], cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(
            res.returncode, 0, f"Failed with output:\n{res.stdout}\n{res.stderr}"
        )
        self.assertIn(
            "Dependency Closure and Lockfile Verification Gate PASSED!", res.stdout
        )


if __name__ == "__main__":
    unittest.main()
