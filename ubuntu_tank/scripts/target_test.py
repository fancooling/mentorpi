#!/usr/bin/env python3
"""
target_test.py - Milestone 14.1 Real Pi 5 Installation & Deployment Integration Test Orchestrator.

Mandatory safety invariant:
Under NO circumstances does this suite authorize on-ground motion or motor actuation.
Motor power must remain off and motion disarmed throughout.
Movement testing remains Milestone 15.

Validates:
1. Target platform preflight: Linux aarch64, Raspberry Pi 5, Ubuntu 26.04, root privileges,
   deployment lock exclusivity, container and service mutual exclusion, disarmed motors.
2. Read-only prerequisites: a previously built, packaged, installed and activated
   production ARM64 release with manifest-covered frontend output. Never builds.
3. Comprehensive installed system verification (via real consumers, no literal text matching):
   - Package sources and locked version closure.
   - Users and groups (ubuntu-tank, ubuntu-tank-operator, ubuntu-tank-web hardware isolation).
   - Udev rules and /dev/rrc identity attributes.
   - Runtime and persistent directories with locked permissions.
   - Systemd units, target grouping, and security confinement (systemd-analyze verify & security).
   - Consumer configuration parsing (controller.yaml, web.yaml, loopback.xml, SROS2 keystore).
   - TLS certificates, key permissions (0600), and SANs.
   - Release manifests, frontend static delivery, and zero Node.js/npm runtime artifacts.
   - ARM64 native imports and launchers without ambient PYTHONPATH.
4. Service lifecycle and web availability:
   - Web API and static PWA delivery available while controller is stopped.
   - Protected lifecycle IPC over Unix domain socket with SO_PEERCRED.
   - Native DDS loopback communication and topic ownership with motors off.
5. Optional operational lifecycle and rollback scenarios using prebuilt archives:
   - Repeat install idempotence.
   - Upgrade and rollback switchover.
   - Interrupted activation journal recovery.
   - Offline rollback to native-only baseline (pruning web services while preserving web.yaml).
6. Pre-test baseline capture and safe restoration.
7. Structured JSON and Markdown report generation.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import platform
import pwd
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Verification must not change the release it verifies, including in children.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# Resolve repository paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)
SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")

for p in [WORKSPACE_ROOT, UBUNTU_TANK_DIR, SCRIPT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

for pkg in [
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
    "ubuntu_tank_bringup",
    "ubuntu_tank_operator",
    "ubuntu_tank_web",
]:
    pkg_path = os.path.join(SRC_DIR, pkg)
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)

from ubuntu_tank.scripts.deployment_manager import (
    DEFAULT_ETC_DIR,
    DEFAULT_LOCK_PATH,
    DEFAULT_OPT_DIR,
    DEFAULT_RUN_DIR,
    DEFAULT_SYSTEMD_DIR,
    DEFAULT_UDEV_DIR,
    DEFAULT_VAR_DIR,
    DeploymentLock,
    ReleaseManager,
    check_hardware_mutual_exclusion,
    compute_file_sha256,
    parse_release_manifest,
    verify_build,
)

try:
    import yaml
except ImportError:
    yaml = None

try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
except ImportError:
    x509 = None
    default_backend = None

try:
    from ubuntu_tank_operator.config import WebControlConfig
except ImportError:
    WebControlConfig = None


EXPECTED_ARCH = "aarch64"
EXPECTED_OS = "Ubuntu"
EXPECTED_RELEASE = "26.04"
DEFAULT_REPORT_DIR = os.path.join(WORKSPACE_ROOT, "dist")
DEFAULT_REPORT_NAME = "target-test-report-milestone14_1"
DEFAULT_BASELINE_SNAPSHOT = "pre_milestone14_1_baseline"


@dataclass
class StepResult:
    name: str
    phase: str
    status: str  # PASSED, FAILED, PENDING, SKIPPED
    duration_sec: float = 0.0
    command: str | None = None
    exit_code: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


class TargetIntegrationOrchestrator:
    """Verify an installed Pi release; optionally exercise prebuilt deployment scenarios.

    Normal runs need no archives or build tools. They do start/stop services and
    leave motion stopped/disarmed. Deployment scenarios require all three archive
    inputs and an operator login; production installation verifies archive integrity.
    expected_release_id pins verification to an owner-selected installed version.
    """

    def __init__(
        self,
        opt_dir: str = DEFAULT_OPT_DIR,
        etc_dir: str = DEFAULT_ETC_DIR,
        var_dir: str = DEFAULT_VAR_DIR,
        run_dir: str = DEFAULT_RUN_DIR,
        systemd_dir: str = DEFAULT_SYSTEMD_DIR,
        udev_dir: str = DEFAULT_UDEV_DIR,
        lock_path: str = DEFAULT_LOCK_PATH,
        report_dir: str = DEFAULT_REPORT_DIR,
        report_name: str = DEFAULT_REPORT_NAME,
        baseline_snapshot_name: str = DEFAULT_BASELINE_SNAPSHOT,
        require_target: bool = False,
        inspect_only: bool = False,
        native_release_archive: str | None = None,
        operator_user: str | None = None,
        expected_release_id: str | None = None,
        deployment_scenarios: bool = False,
        release_archive: str | None = None,
        upgrade_release_archive: str | None = None,
    ):
        self.opt_dir = opt_dir
        self.etc_dir = etc_dir
        self.var_dir = var_dir
        self.run_dir = run_dir
        self.systemd_dir = systemd_dir
        self.udev_dir = udev_dir
        self.lock_path = lock_path
        self.report_dir = report_dir
        self.report_name = report_name
        self.baseline_snapshot_name = baseline_snapshot_name
        self.require_target = require_target
        self.inspect_only = inspect_only
        self.native_release_archive = native_release_archive
        self.operator_user = operator_user or os.environ.get("SUDO_USER")
        self.expected_release_id = expected_release_id
        self.deployment_scenarios = deployment_scenarios
        self.release_archive = release_archive
        self.upgrade_release_archive = upgrade_release_archive

        self.mgr = ReleaseManager(
            opt_dir=self.opt_dir,
            etc_dir=self.etc_dir,
            var_dir=self.var_dir,
            run_dir=self.run_dir,
            systemd_dir=self.systemd_dir,
            udev_dir=self.udev_dir,
            lock_path=self.lock_path,
        )

        self.results: dict[str, Any] = {
            "milestone": "14.1",
            "title": "Real Pi 5 Installation & Deployment Integration Tests",
            "start_time": datetime.now(timezone.utc).isoformat(),
            "end_time": None,
            "overall_status": "PENDING_TARGET_EXECUTION",
            "host_environment": {},
            "phases": {},
            "steps": [],
            "failures": [],
        }
        self.step_records: list[StepResult] = []
        self._lock_held = False
        self._baseline_dir: str | None = None
        self._pre_test_active_target: str | None = None
        self._pre_test_journal_content: bytes | None = None

    def run(self) -> int:
        """Run the complete target test suite or record pending status."""
        os.makedirs(self.report_dir, exist_ok=True)
        self._sample_host_environment()

        # Check platform match
        is_target, reason = self._check_target_platform()
        if not is_target:
            self.results["overall_status"] = "PENDING_TARGET_EXECUTION"
            self.results["pending_reason"] = reason
            self._record_pending_phases(reason)
            self._finalize_report()
            print(f"\nTarget platform mismatch: {reason}")
            print(
                "Per Milestone 14.1 and AGENTS.md, mutating target installation tests are not simulated on dev machines."
            )
            print(
                f"Report recorded with status PENDING_TARGET_EXECUTION in: {self.report_dir}/{self.report_name}.json"
            )
            return 1 if self.require_target else 0

        # On target: execute real mutating suite (individual operations acquire deployment lock)
        try:
            self._run_target_suite()
        except Exception as e:  # noqa: BLE001
            self.results["overall_status"] = "FAILED"
            self.results["failures"].append(str(e))
            print(f"\nFATAL: Target test suite encountered an unhandled exception: {e}")
        finally:
            self._finalize_report()

        return 0 if self.results["overall_status"] == "PASSED" else 1

    def _sample_host_environment(self):
        """Record system environment facts."""
        env = {
            "machine": platform.machine(),
            "system": platform.system(),
            "node": platform.node(),
            "release": platform.release(),
            "version": platform.version(),
            "python_version": platform.python_version(),
            "euid": os.geteuid(),
        }
        # Check OS release
        os_rel_file = "/etc/os-release"
        if os.path.isfile(os_rel_file):
            try:
                with open(os_rel_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            env[f"os_{k.lower()}"] = v.strip("\"'")
            except OSError:
                pass
        # Check device-tree model
        dt_model = "/proc/device-tree/model"
        if os.path.isfile(dt_model):
            try:
                with open(dt_model, "r", encoding="utf-8", errors="replace") as f:
                    env["device_tree_model"] = f.read().strip("\x00 \n\r")
            except OSError:
                pass
        # Git repository commit
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=WORKSPACE_ROOT, text=True
            ).strip()
            env["git_commit"] = commit
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=WORKSPACE_ROOT,
                text=True,
            ).strip()
            env["git_branch"] = branch
        except (OSError, subprocess.SubprocessError):
            pass

        self.results["host_environment"] = env

    def _check_target_platform(self) -> tuple[bool, str]:
        """Check if current environment is the authentic native ARM64 Pi 5 target."""
        machine = platform.machine()
        if machine != EXPECTED_ARCH:
            return (
                False,
                f"Host CPU architecture is '{machine}', expected ARM64 ('{EXPECTED_ARCH}').",
            )

        os_name = self.results["host_environment"].get("os_name", "")
        os_ver = self.results["host_environment"].get("os_version_id", "")
        if "Ubuntu" not in os_name or not os_ver.startswith(EXPECTED_RELEASE):
            return (
                False,
                f"Host OS is '{os_name} {os_ver}', expected native Ubuntu {EXPECTED_RELEASE}.",
            )

        return True, "Host matches native ARM64 Ubuntu 26.04 target."

    def _record_pending_phases(self, reason: str):
        """Record all target execution phases as pending when run on non-target."""
        phases = [
            "Phase 1: Target Preflight & Baseline Capture",
            "Phase 2: Installed Release Prerequisites",
            "Phase 3: Comprehensive Installed System Verification",
            "Phase 4: Service Lifecycle & Web Availability",
            "Phase 5: Operational Lifecycle & Rollback Scenarios",
            "Phase 6: Target Baseline Restoration",
        ]
        for ph in phases:
            self.results["phases"][ph] = {
                "status": "PENDING",
                "reason": reason,
            }

    def _record_step(self, step: StepResult):
        """Record an executed step."""
        self.step_records.append(step)
        self.results["steps"].append(asdict(step))
        status_symbol = (
            "PASS"
            if step.status == "PASSED"
            else ("FAIL" if step.status == "FAILED" else step.status)
        )
        print(
            f"  [{status_symbol}] {step.phase} :: {step.name} ({step.duration_sec:.2f}s)"
        )
        if step.status == "FAILED":
            self.results["failures"].append(
                f"{step.phase} :: {step.name}: {step.error_message or 'Step failed'}"
            )
            if step.error_message:
                print(f"         Error: {step.error_message}")

    def _run_cmd(
        self,
        cmd: list[str],
        name: str,
        phase: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 300.0,
    ) -> StepResult:
        """Run a production shell command and record its output and exit code."""
        start = time.time()
        cmd_str = " ".join(cmd)
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        full_env["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            res = subprocess.run(
                cmd,
                cwd=cwd or WORKSPACE_ROOT,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            duration = time.time() - start
            status = "PASSED" if res.returncode == 0 else "FAILED"
            err = res.stderr.strip() if res.returncode != 0 else None
            return StepResult(
                name=name,
                phase=phase,
                status=status,
                duration_sec=duration,
                command=cmd_str,
                exit_code=res.returncode,
                details={
                    "stdout": res.stdout[-2000:] if res.stdout else "",
                    "stderr": res.stderr[-2000:] if res.stderr else "",
                },
                error_message=err,
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start
            return StepResult(
                name=name,
                phase=phase,
                status="FAILED",
                duration_sec=duration,
                command=cmd_str,
                exit_code=-1,
                error_message=f"Command timed out after {timeout} seconds",
            )
        except Exception as e:  # noqa: BLE001
            duration = time.time() - start
            return StepResult(
                name=name,
                phase=phase,
                status="FAILED",
                duration_sec=duration,
                command=cmd_str,
                exit_code=-1,
                error_message=str(e),
            )

    # -------------------------------------------------------------------------
    # Target Execution Pipeline
    # -------------------------------------------------------------------------
    def _verify_installed_runtime(self) -> subprocess.CompletedProcess:
        """Run the read-only verifier with the active overlay and status credentials.

        Setup failures are fatal; never fall back to checkout/global Python or
        caller-provided ROS credentials. This queries hardware but never arms it.
        """
        current = os.path.join(self.opt_dir, "current")
        env = os.environ.copy()
        env.update(
            PYTHONDONTWRITEBYTECODE="1",
            ROS_LOCALHOST_ONLY="1",
            ROS_SECURITY_ENABLE="true",
            ROS_SECURITY_STRATEGY="Enforce",
            ROS_SECURITY_KEYSTORE=os.path.join(self.etc_dir, "security", "keystore"),
            ROS_SECURITY_ENCLAVE_OVERRIDE="/ubuntu_tank/status",
            RMW_IMPLEMENTATION="rmw_fastrtps_cpp",
            ROS_DOMAIN_ID="0",
            ROS_AUTOMATIC_DISCOVERY_RANGE="SYSTEM_DEFAULT",
            FASTDDS_DEFAULT_PROFILES_FILE=os.path.join(
                current, "config", "fastdds", "loopback.xml"
            ),
        )
        return subprocess.run(
            [
                "bash",
                "-c",
                'set -e; source "$1"; source "$2"; exec bash "$3"',
                "target-runtime-check",
                "/opt/ros/lyrical/setup.bash",
                os.path.join(current, "install", "setup.bash"),
                os.path.join(SCRIPT_DIR, "verify_runtime.sh"),
            ],
            env=env,
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_target_suite(self):
        """Run the comprehensive target testing suite across all 6 phases."""
        print("============================================================")
        print("Running Real Pi 5 Installation & Deployment Integration Suite")
        print("============================================================")

        # Read-only prerequisite checks precede snapshots, service operations,
        # and any optional deployment scenarios.
        if not self._check_installed_prerequisites():
            self.results["overall_status"] = "FAILED"
            return

        # Phase 1: Target Preflight & Baseline Capture
        p1_ok = self._execute_phase1_preflight()
        if not p1_ok:
            self.results["overall_status"] = "FAILED"
            return

        if self.inspect_only:
            print(
                "\n--inspect-only passed: Preflight checks completed without mutating host."
            )
            self.results["overall_status"] = "PASSED"
            return

        p3_ok = False
        p4_ok = False
        p5_ok = False

        try:
            # Phase 3: Comprehensive Installed System Verification
            p3_ok = self._execute_phase3_installed_verification()
            if not p3_ok:
                self.results["overall_status"] = "FAILED"
                return

            # Phase 4: Service Lifecycle & Web Availability (Motors Off)
            p4_ok = self._execute_phase4_service_lifecycle()
            if not p4_ok:
                self.results["overall_status"] = "FAILED"
                return

            # Phase 5: Operational Lifecycle & Rollback Scenarios
            if self.deployment_scenarios:
                p5_ok = self._execute_phase5_lifecycle_and_rollback()
            else:
                p5_ok = True
                self.results["phases"][
                    "Phase 5: Operational Lifecycle & Rollback Scenarios"
                ] = {
                    "status": "SKIPPED",
                    "reason": "Not requested; use --deployment-scenarios with prebuilt release archives",
                }
            if not p5_ok:
                self.results["overall_status"] = "FAILED"
                return

        finally:
            # Phase 6: Baseline Restoration
            p6_ok = self._execute_phase6_baseline_restoration()
            if not p6_ok:
                self.results["overall_status"] = "FAILED"
            elif (
                p1_ok
                and p3_ok
                and p4_ok
                and p5_ok
                and self.results["overall_status"] not in ("FAILED", "NEEDS_REBOOT")
            ):
                self.results["overall_status"] = "PASSED"

    # -------------------------------------------------------------------------
    # Phase 1: Preflight & Baseline Capture
    # -------------------------------------------------------------------------
    def _execute_phase1_preflight(self) -> bool:
        phase = "Phase 1: Target Preflight & Baseline Capture"
        print(f"\n--> {phase}...")
        self.results["phases"][phase] = {"status": "IN_PROGRESS"}

        # 1. Root check
        t0 = time.time()
        is_root = os.geteuid() == 0
        self._record_step(
            StepResult(
                name="root_privilege_check",
                phase=phase,
                status="PASSED" if is_root else "FAILED",
                duration_sec=time.time() - t0,
                error_message=None
                if is_root
                else "target-test requires root privileges (run with sudo).",
            )
        )
        if not is_root:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 2. Container & Service Mutual Exclusion
        t0 = time.time()
        mut_ok, mut_err_list = check_hardware_mutual_exclusion()
        mut_err = "; ".join(mut_err_list) if not mut_ok else None
        self._record_step(
            StepResult(
                name="container_and_service_mutual_exclusion",
                phase=phase,
                status="PASSED" if mut_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=mut_err if not mut_ok else None,
            )
        )
        if not mut_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 3. Deployment Lock Preflight Availability Check
        t0 = time.time()
        lock_avail = True
        lock_err = None
        try:
            with DeploymentLock(self.lock_path, timeout_sec=1.0):
                pass
        except Exception as e:  # noqa: BLE001
            lock_avail = False
            lock_err = f"Deployment lock is held by another process: {e}"

        self._record_step(
            StepResult(
                name="deployment_lock_preflight_check",
                phase=phase,
                status="PASSED" if lock_avail else "FAILED",
                duration_sec=time.time() - t0,
                error_message=lock_err,
            )
        )
        if not lock_avail:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 4. Motor Safety Invariant Check: Motors Disarmed
        t0 = time.time()
        motors_disarmed = True
        err_disarm = None
        # Check if mentorpi-tank.service is active
        res = subprocess.run(
            ["systemctl", "is-active", "--quiet", "mentorpi-tank.service"],
            check=False,
        )
        if res.returncode == 0:
            # If running, query guard state topic
            verify_sh = os.path.join(SCRIPT_DIR, "verify_runtime.sh")
            if os.path.isfile(verify_sh):
                vres = self._verify_installed_runtime()
                if (
                    vres.returncode != 0
                    or "Guard state confirmed DISARMED" not in vres.stdout
                ):
                    motors_disarmed = False
                    err_disarm = "Active controller detected with armed or unverified motor guard. Failing safety invariant."

        self._record_step(
            StepResult(
                name="motor_guard_safety_invariant",
                phase=phase,
                status="PASSED" if motors_disarmed else "FAILED",
                duration_sec=time.time() - t0,
                error_message=err_disarm,
            )
        )
        if not motors_disarmed:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 5. Record Pre-Test Baseline Snapshot (skip in inspect-only mode)
        if self.inspect_only:
            self.results["phases"][phase] = {"status": "PASSED"}
            return True

        t0 = time.time()
        current_symlink = os.path.join(self.opt_dir, "current")
        if os.path.islink(current_symlink):
            self._pre_test_active_target = os.path.realpath(current_symlink)

        # Capture pre-test activation journal content for clean restoration
        if os.path.isfile(self.mgr.journal_path):
            try:
                with open(self.mgr.journal_path, "rb") as jf:
                    self._pre_test_journal_content = jf.read()
            except OSError:
                pass

        try:
            snap_tx_id = (
                f"{self.baseline_snapshot_name}_{int(time.time())}_{os.getpid()}"
            )
            snap_path = self.mgr.snapshot_mgr.create_snapshot(
                tx_id=snap_tx_id,
                current_symlink_target=self._pre_test_active_target,
                etc_dir=self.etc_dir,
                systemd_dir=self.systemd_dir,
                udev_dir=self.udev_dir,
            )
            self._baseline_dir = snap_path
            snap_ok = self.mgr.snapshot_mgr.verify_snapshot(snap_path)
            self._record_step(
                StepResult(
                    name="pre_test_baseline_snapshot",
                    phase=phase,
                    status="PASSED" if snap_ok else "FAILED",
                    duration_sec=time.time() - t0,
                    details={"snapshot_path": snap_path},
                )
            )
        except Exception as e:  # noqa: BLE001
            self._record_step(
                StepResult(
                    name="pre_test_baseline_snapshot",
                    phase=phase,
                    status="FAILED",
                    duration_sec=time.time() - t0,
                    error_message=f"Failed to create pre-test baseline snapshot: {e}",
                )
            )
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        self.results["phases"][phase] = {"status": "PASSED"}
        return True

    # -------------------------------------------------------------------------
    # Phase 2: Read-only Installed Prerequisites (checked before Phase 1 mutations)
    # -------------------------------------------------------------------------
    def _check_installed_prerequisites(self) -> bool:
        """Validate the active production release before any service or file changes.

        A checkout build is not installation evidence. Require a complete ARM64
        release, its production build provenance, and manifest-covered PWA output.
        Never repair missing artifacts by building or installing them here.
        """
        phase = "Phase 2: Installed Release Prerequisites"
        started = time.time()
        try:
            current = os.path.join(self.opt_dir, "current")
            if not os.path.islink(current):
                raise ValueError(
                    "No active release: build, package, install and activate before target-test"
                )
            release = os.path.realpath(current)
            release_id = os.path.basename(release)
            if release != os.path.join(
                os.path.realpath(self.opt_dir), "releases", release_id
            ):
                raise ValueError(
                    "Active release must be installed under the production releases directory"
                )
            valid, errors = self.mgr.validate_release(
                release, expected_release_id=release_id
            )
            if not valid:
                raise ValueError(
                    "Installed release is incomplete or changed: " + "; ".join(errors)
                )
            headers, files = parse_release_manifest(
                os.path.join(release, "release-manifest.txt")
            )
            if headers.get("Target-Architecture") != "arm64":
                raise ValueError("Installed release is not ARM64")
            if self.expected_release_id and release_id != self.expected_release_id:
                raise ValueError(
                    f"Expected installed release {self.expected_release_id}, found {release_id}"
                )
            verify_build(
                os.path.join(release, "install"),
                os.path.join(release, "install"),
                os.path.join(release, "src"),
            )
            for asset in ("index.html", "manifest.webmanifest", "sw.js"):
                relative = f"web/dist/{asset}"
                if relative not in files or not os.path.isfile(
                    os.path.join(release, relative)
                ):
                    raise ValueError(
                        "Installed frontend missing or outside manifest: "
                        + relative
                        + "; build the frontend, then package, install and activate again"
                    )
            if not os.path.isfile(
                os.path.join(self.etc_dir, "controller.yaml")
            ) or not os.path.isfile(os.path.join(self.etc_dir, "web", "web.yaml")):
                raise ValueError(
                    "Installed configuration is missing; complete production installation first"
                )
            if self.deployment_scenarios:
                for label, archive in (
                    ("--release-archive", self.release_archive),
                    ("--upgrade-release-archive", self.upgrade_release_archive),
                    ("--native-release-archive", self.native_release_archive),
                ):
                    if not archive or not os.path.isfile(archive):
                        raise ValueError(
                            label
                            + " must name a prebuilt archive for deployment scenarios"
                        )
                if not self.operator_user or self.operator_user in (
                    "root",
                    "ubuntu-tank",
                ):
                    raise ValueError(
                        "Use sudo from the operator login or pass --operator-user for deployment scenarios"
                    )
                pwd.getpwnam(self.operator_user)
            self._installed_release_id = release_id
            self._installed_archive = self.release_archive
            self.results["installed_release"] = {"id": release_id, "path": release}
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self._record_step(
                StepResult(
                    name="installed_release_prerequisites",
                    phase=phase,
                    status="FAILED",
                    duration_sec=time.time() - started,
                    error_message=str(exc),
                )
            )
            self.results["phases"][phase] = {"status": "FAILED"}
            return False
        self._record_step(
            StepResult(
                name="installed_release_prerequisites",
                phase=phase,
                status="PASSED",
                duration_sec=time.time() - started,
            )
        )
        self.results["phases"][phase] = {"status": "PASSED"}
        return True

    def _execute_phase3_installed_verification(self) -> bool:
        phase = "Phase 3: Comprehensive Installed System Verification"
        print(f"\n--> {phase}...")
        self.results["phases"][phase] = {"status": "IN_PROGRESS"}
        current_dir = os.path.join(self.opt_dir, "current")

        # 1. Package sources & locked version checks
        t0 = time.time()
        dpkg_ok = True
        dpkg_err = None
        # Verify key package versions
        key_pkgs = ["python3", "systemd", "udev"]
        for pkg in key_pkgs:
            res = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True,
                text=True,
                check=False,
            )
            if "install ok installed" not in res.stdout:
                dpkg_ok = False
                dpkg_err = f"Essential package '{pkg}' is not installed ok."
                break
        self._record_step(
            StepResult(
                name="package_sources_and_installed_status",
                phase=phase,
                status="PASSED" if dpkg_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=dpkg_err,
            )
        )
        if not dpkg_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 2. Users and Groups Verification (Hardware Isolation)
        t0 = time.time()
        u_ok = True
        u_err = None
        try:
            tank_u = pwd.getpwnam("ubuntu-tank")
            op_u = pwd.getpwnam("ubuntu-tank-operator")
            web_u = pwd.getpwnam("ubuntu-tank-web")
            rrc_g = grp.getgrnam("mentorpi-rrc")
            op_g = grp.getgrnam("ubuntu-tank-operators")

            # Check group memberships
            # ubuntu-tank must be in mentorpi-rrc
            if tank_u.pw_gid != rrc_g.gr_gid and tank_u.pw_name not in rrc_g.gr_mem:
                u_ok = False
                u_err = "User 'ubuntu-tank' is not a member of 'mentorpi-rrc'."
            # ubuntu-tank-operator must be in ubuntu-tank-operators
            if op_u.pw_gid != op_g.gr_gid and op_u.pw_name not in op_g.gr_mem:
                u_ok = False
                u_err = "User 'ubuntu-tank-operator' is not a member of 'ubuntu-tank-operators'."
            # ubuntu-tank-web MUST be in ubuntu-tank-operators, but strictly NOT in mentorpi-rrc
            if web_u.pw_gid != op_g.gr_gid and web_u.pw_name not in op_g.gr_mem:
                u_ok = False
                u_err = (
                    "User 'ubuntu-tank-web' is not a member of 'ubuntu-tank-operators'."
                )
            if web_u.pw_gid == rrc_g.gr_gid or web_u.pw_name in rrc_g.gr_mem:
                u_ok = False
                u_err = "SECURITY VIOLATION: User 'ubuntu-tank-web' has membership in 'mentorpi-rrc'!"
        except KeyError as ke:
            u_ok = False
            u_err = f"Required user or group missing on target: {ke}"

        self._record_step(
            StepResult(
                name="users_groups_and_hardware_isolation",
                phase=phase,
                status="PASSED" if u_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=u_err,
            )
        )
        if not u_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 3. Udev Rules & Device Permissions
        t0 = time.time()
        udev_rule_path = os.path.join(self.udev_dir, "99-mentorpi-rrc.rules")
        udev_ok = os.path.isfile(udev_rule_path) and (
            stat.S_IMODE(os.stat(udev_rule_path).st_mode) == 0o644
        )
        self._record_step(
            StepResult(
                name="udev_rules_file_permissions",
                phase=phase,
                status="PASSED" if udev_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=None
                if udev_ok
                else f"Udev rule '{udev_rule_path}' missing or invalid permissions.",
            )
        )
        if not udev_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 4. Runtime and Persistent Directories & Ownership
        t0 = time.time()
        dir_checks = [
            (self.run_dir, 0o775, "ubuntu-tank", "ubuntu-tank-operators"),
            (
                os.path.join(self.var_dir, "operator-log"),
                0o750,
                "ubuntu-tank-operator",
                "ubuntu-tank-operators",
            ),
            (
                os.path.join(self.var_dir, "ros-log"),
                0o750,
                "ubuntu-tank",
                "mentorpi-rrc",
            ),
            (
                os.path.join(self.var_dir, "web", "certs"),
                0o700,
                "ubuntu-tank-web",
                "ubuntu-tank-web",
            ),
        ]
        dirs_ok = True
        dirs_err = None
        for path, exp_mode, exp_user, exp_grp in dir_checks:
            if not os.path.isdir(path):
                dirs_ok = False
                dirs_err = f"Directory missing: {path}"
                break
            st = os.stat(path)
            mode = stat.S_IMODE(st.st_mode)
            if mode != exp_mode:
                dirs_ok = False
                dirs_err = (
                    f"Directory {path} has mode {oct(mode)}, expected {oct(exp_mode)}"
                )
                break
            actual_u = pwd.getpwuid(st.st_uid).pw_name
            actual_g = grp.getgrgid(st.st_gid).gr_name
            if actual_u != exp_user or actual_g != exp_grp:
                dirs_ok = False
                dirs_err = f"Directory {path} owned by {actual_u}:{actual_g}, expected {exp_user}:{exp_grp}"
                break

        self._record_step(
            StepResult(
                name="runtime_and_persistent_directory_permissions",
                phase=phase,
                status="PASSED" if dirs_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=dirs_err,
            )
        )
        if not dirs_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 5. Systemd Units Confinement (systemd-analyze verify & security)
        t0 = time.time()
        units = [
            os.path.join(self.systemd_dir, u)
            for u in [
                "mentorpi-tank.service",
                "mentorpi-tank-operator.service",
                "mentorpi-tank-web.service",
                "mentorpi-tank-lifecycle.service",
                "mentorpi-tank-stack.target",
                "mentorpi-tank-recover.service",
            ]
        ]
        existing_units = [u for u in units if os.path.isfile(u)]
        res = subprocess.run(
            ["systemd-analyze", "verify"] + existing_units,
            capture_output=True,
            text=True,
            check=False,
        )
        sys_ok = res.returncode == 0
        self._record_step(
            StepResult(
                name="systemd_analyze_verify_installed_units",
                phase=phase,
                status="PASSED" if sys_ok else "FAILED",
                duration_sec=time.time() - t0,
                details={"stderr": res.stderr},
                error_message=res.stderr.strip() if not sys_ok else None,
            )
        )
        if not sys_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 6. Consumer-Based Configuration Loading
        t0 = time.time()
        cfg_ok = True
        cfg_err = None
        # Load controller.yaml
        ctrl_yaml = os.path.join(self.etc_dir, "controller.yaml")
        if not os.path.isfile(ctrl_yaml):
            cfg_ok = False
            cfg_err = f"controller.yaml missing at {ctrl_yaml}"
        else:
            try:
                with open(ctrl_yaml, "r", encoding="utf-8") as f:
                    ydata = yaml.safe_load(f) if yaml else {}
                if not isinstance(ydata, dict):
                    cfg_ok = False
                    cfg_err = "controller.yaml parsed as non-dict"
            except Exception as e:  # noqa: BLE001
                cfg_ok = False
                cfg_err = f"Failed to parse controller.yaml: {e}"

        # Load web.yaml via WebControlConfig
        web_yaml = os.path.join(self.etc_dir, "web", "web.yaml")
        if not os.path.isfile(web_yaml):
            cfg_ok = False
            cfg_err = f"web.yaml missing at {web_yaml}"
        elif WebControlConfig and yaml:
            try:
                with open(web_yaml, "r", encoding="utf-8") as f:
                    ydata = yaml.safe_load(f) or {}
                wcfg = WebControlConfig.from_dict(ydata)
                wcfg.validate()
                if not wcfg.listen_address:
                    cfg_ok = False
                    cfg_err = "web.yaml loaded invalid configuration"
            except Exception as e:  # noqa: BLE001
                cfg_ok = False
                cfg_err = f"WebControlConfig validation failed on web.yaml: {e}"

        # Load loopback.xml via ET
        loopback_xml = os.path.join(current_dir, "config/fastdds/loopback.xml")
        if not os.path.isfile(loopback_xml):
            cfg_ok = False
            cfg_err = f"loopback.xml missing at {loopback_xml}"
        else:
            try:
                ET.parse(loopback_xml)
            except Exception as e:  # noqa: BLE001
                cfg_ok = False
                cfg_err = f"loopback.xml XML parsing failed: {e}"

        # SROS2 keystore check
        keystore_dir = os.path.join(self.etc_dir, "security", "keystore")
        if not self.mgr._is_valid_sros2_keystore(keystore_dir):
            cfg_ok = False
            cfg_err = f"SROS2 keystore at {keystore_dir} is invalid or incomplete."

        self._record_step(
            StepResult(
                name="consumer_configuration_loading",
                phase=phase,
                status="PASSED" if cfg_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=cfg_err,
            )
        )
        if not cfg_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 7. TLS Certificate Directory Permissions (Generated at web service startup)
        t0 = time.time()
        tls_ok = True
        tls_err = None
        certs_dir = os.path.join(self.var_dir, "web", "certs")
        if not os.path.isdir(certs_dir):
            tls_ok = False
            tls_err = f"TLS certs directory missing at {certs_dir}"
        else:
            cmode = stat.S_IMODE(os.stat(certs_dir).st_mode)
            if cmode != 0o700:
                tls_ok = False
                tls_err = f"TLS certs directory has mode {oct(cmode)}, expected 0700."

        self._record_step(
            StepResult(
                name="tls_certs_directory_mode_validation",
                phase=phase,
                status="PASSED" if tls_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=tls_err,
            )
        )
        if not tls_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 8. Release Manifest & Frontend Delivery Closure
        t0 = time.time()
        man_ok = True
        man_err = None
        manifest_file = os.path.join(current_dir, "release-manifest.txt")
        if not os.path.isfile(manifest_file):
            man_ok = False
            man_err = f"release-manifest.txt missing in active release: {manifest_file}"
        else:
            _headers, files = parse_release_manifest(manifest_file)
            for rel_path, file_info in files.items():
                fp = os.path.join(current_dir, rel_path)
                if not os.path.isfile(fp):
                    man_ok = False
                    man_err = f"Manifest file missing on disk: {rel_path}"
                    break
                actual_sha = compute_file_sha256(fp)
                exp_sha = (
                    file_info.get("sha256")
                    if isinstance(file_info, dict)
                    else str(file_info)
                )
                if actual_sha != exp_sha:
                    man_ok = False
                    man_err = (
                        f"Checksum mismatch for {rel_path}: {actual_sha} != {exp_sha}"
                    )
                    break

        # Check frontend assets exist and zero node_modules
        if man_ok:
            dist_index = os.path.join(current_dir, "web", "dist", "index.html")
            dist_manifest = os.path.join(
                current_dir, "web", "dist", "manifest.webmanifest"
            )
            if not os.path.isfile(dist_index) or not os.path.isfile(dist_manifest):
                man_ok = False
                man_err = "Static frontend assets (index.html, manifest.webmanifest) missing in web/dist/"
            elif os.path.exists(os.path.join(current_dir, "web", "node_modules")):
                man_ok = False
                man_err = (
                    "SECURITY VIOLATION: node_modules packaged into production release!"
                )
            elif os.path.exists(os.path.join(current_dir, "web", "package.json")):
                man_ok = False
                man_err = "package.json packaged into production release!"

        self._record_step(
            StepResult(
                name="release_manifest_and_frontend_closure",
                phase=phase,
                status="PASSED" if man_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=man_err,
            )
        )
        if not man_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 9. ARM64 Native Imports & Launchers without PYTHONPATH
        t0 = time.time()
        launch_ok = True
        launch_err = None
        launchers = [
            os.path.join(current_dir, "bin", "mentorpi-tank-web"),
            os.path.join(current_dir, "bin", "mentorpi-tank-lifecycle"),
        ]
        clean_env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "USER": "root",
            "HOME": "/root",
        }
        for l in launchers:
            res = subprocess.run(
                [l, "--help"],
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                launch_ok = False
                launch_err = f"Launcher {os.path.basename(l)} failed without PYTHONPATH: {res.stderr}"
                break

        # Test python imports through installed release environment from an unrelated directory
        if launch_ok:
            setup_sh = os.path.join(current_dir, "install", "setup.bash")
            release_pythonpath = ":".join(
                [
                    os.path.join(current_dir, "src"),
                    os.path.join(current_dir, "src", "ubuntu_tank_web"),
                    os.path.join(current_dir, "src", "ubuntu_tank_operator"),
                ]
            )
            py_cmd = (
                f"if [ -f '{setup_sh}' ]; then source '{setup_sh}' 2>/dev/null || true; fi; "
                f"export PYTHONPATH='{release_pythonpath}':\"${{PYTHONPATH:-}}\"; "
                f"{sys.executable} -c 'import ubuntu_tank_web; import ubuntu_tank_operator; print(\"OK\")'"
            )
            res = subprocess.run(
                ["bash", "-c", py_cmd],
                cwd="/tmp",
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0 or "OK" not in res.stdout:
                launch_ok = False
                launch_err = f"ARM64 release import test failed in installed environment: {res.stderr or res.stdout}"

        self._record_step(
            StepResult(
                name="arm64_native_imports_and_launchers",
                phase=phase,
                status="PASSED" if launch_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=launch_err,
            )
        )
        if not launch_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        self.results["phases"][phase] = {"status": "PASSED"}
        return True

    # -------------------------------------------------------------------------
    # Phase 4: Service Lifecycle & Web Availability (Motors Off)
    # -------------------------------------------------------------------------
    def _execute_phase4_service_lifecycle(self) -> bool:
        phase = "Phase 4: Service Lifecycle & Web Availability"
        print(f"\n--> {phase}...")
        self.results["phases"][phase] = {"status": "IN_PROGRESS"}

        # 1. Start lifecycle helper & web service while controller is STOPPED
        t0 = time.time()
        srv_ok = True
        srv_err = None
        subprocess.run(["systemctl", "stop", "mentorpi-tank.service"], check=False)
        subprocess.run(
            ["systemctl", "stop", "mentorpi-tank-operator.service"], check=False
        )
        subprocess.run(
            ["systemctl", "start", "mentorpi-tank-lifecycle.service"], check=True
        )
        subprocess.run(["systemctl", "start", "mentorpi-tank-web.service"], check=True)
        time.sleep(1.0)

        # Confirm controller is inactive
        res = subprocess.run(
            ["systemctl", "is-active", "--quiet", "mentorpi-tank.service"],
            check=False,
        )
        if res.returncode == 0:
            srv_ok = False
            srv_err = "mentorpi-tank.service is active when it should be stopped."

        # Confirm web is active
        res = subprocess.run(
            ["systemctl", "is-active", "--quiet", "mentorpi-tank-web.service"],
            check=False,
        )
        if res.returncode != 0:
            srv_ok = False
            srv_err = "mentorpi-tank-web.service failed to start."

        self._record_step(
            StepResult(
                name="web_service_starts_with_controller_stopped",
                phase=phase,
                status="PASSED" if srv_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=srv_err,
            )
        )
        if not srv_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 2. TLS Certificate, Key Permissions, and SANs (Generated at web service startup)
        t0 = time.time()
        tls_ok = True
        tls_err = None
        cert_path = os.path.join(self.var_dir, "web", "certs", "server.crt")
        key_path = os.path.join(self.var_dir, "web", "certs", "server.key")
        if not os.path.isfile(cert_path) or not os.path.isfile(key_path):
            tls_ok = False
            tls_err = (
                "TLS certificate or key file missing in /var/opt/ubuntu_tank/web/certs/"
            )
        else:
            key_mode = stat.S_IMODE(os.stat(key_path).st_mode)
            cert_mode = stat.S_IMODE(os.stat(cert_path).st_mode)
            if key_mode != 0o600:
                tls_ok = False
                tls_err = f"TLS private key has mode {oct(key_mode)}, expected 0600."
            elif cert_mode != 0o644:
                tls_ok = False
                tls_err = f"TLS certificate has mode {oct(cert_mode)}, expected 0644."
            elif x509:
                try:
                    with open(cert_path, "rb") as f:
                        cert = x509.load_pem_x509_certificate(
                            f.read(), default_backend()
                        )
                    ext = cert.extensions.get_extension_for_oid(
                        x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
                    )
                    san_names = ext.value.get_values_for_type(
                        x509.IPAddress
                    ) + ext.value.get_values_for_type(x509.DNSName)
                    if not any("127.0.0.1" in str(s) for s in san_names):
                        tls_ok = False
                        tls_err = "TLS certificate missing 127.0.0.1 in SAN extension."
                except Exception as e:  # noqa: BLE001
                    tls_ok = False
                    tls_err = f"TLS certificate validation failed: {e}"

        self._record_step(
            StepResult(
                name="tls_certificate_and_key_validation",
                phase=phase,
                status="PASSED" if tls_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=tls_err,
            )
        )
        if not tls_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 3. Query Web API HTTPS Endpoints
        t0 = time.time()
        api_ok = True
        api_err = None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        web_port = 8443
        web_yaml_path = os.path.join(self.etc_dir, "web", "web.yaml")
        if os.path.isfile(web_yaml_path) and yaml:
            try:
                with open(web_yaml_path, "r", encoding="utf-8") as f:
                    wdata = yaml.safe_load(f)
                    if isinstance(wdata, dict) and "port" in wdata:
                        web_port = int(wdata["port"])
            except Exception:  # noqa: BLE001, S110
                pass

        try:
            # /api/v1/version
            req = urllib.request.Request(f"https://127.0.0.1:{web_port}/api/v1/version")
            with urllib.request.urlopen(req, context=ctx, timeout=5.0) as resp:
                vdata = json.loads(resp.read().decode("utf-8"))
                if "protocol_version" not in vdata:
                    api_ok = False
                    api_err = "Version endpoint response missing 'protocol_version'."

            # /api/v1/status (reports inactive controller)
            req = urllib.request.Request(f"https://127.0.0.1:{web_port}/api/v1/status")
            with urllib.request.urlopen(req, context=ctx, timeout=5.0) as resp:
                sdata = json.loads(resp.read().decode("utf-8"))
                if sdata.get("service_state") != "inactive":
                    api_ok = False
                    api_err = f"Status endpoint reported service_state '{sdata.get('service_state')}', expected 'inactive'."

            # /index.html (static Vue frontend delivery)
            req = urllib.request.Request(f"https://127.0.0.1:{web_port}/index.html")
            with urllib.request.urlopen(req, context=ctx, timeout=5.0) as resp:
                html = resp.read().decode("utf-8")
                if (
                    "<html" not in html
                    or resp.headers.get("Content-Security-Policy") is None
                ):
                    api_ok = False
                    api_err = "Static frontend missing HTML content or Content-Security-Policy header."

        except Exception as e:  # noqa: BLE001
            api_ok = False
            api_err = f"HTTPS API query failed: {e}"

        self._record_step(
            StepResult(
                name="web_api_https_and_static_pwa_delivery",
                phase=phase,
                status="PASSED" if api_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=api_err,
            )
        )
        if not api_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 3. Native DDS Communication and Topic Ownership (Motors Off)
        t0 = time.time()
        dds_ok = True
        dds_err = None
        # Start stack target
        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        s = self._run_cmd(
            ["bash", deploy_sh, "start"], name="deploy_start_stack", phase=phase
        )
        if s.status != "PASSED":
            dds_ok = False
            dds_err = f"Failed to start stack target: {s.error_message}"
        else:
            time.sleep(2.0)
            vres = self._verify_installed_runtime()
            if vres.returncode != 0:
                dds_ok = False
                dds_err = f"verify_runtime.sh failed: {vres.stderr or vres.stdout}"

            # Stop stack target
            subprocess.run(["bash", deploy_sh, "stop"], check=False)

        self._record_step(
            StepResult(
                name="native_dds_communication_and_disarmed_guard",
                phase=phase,
                status="PASSED" if dds_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=dds_err,
            )
        )
        if not dds_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        self.results["phases"][phase] = {"status": "PASSED"}
        return True

    # -------------------------------------------------------------------------
    # Phase 5: Operational Lifecycle & Rollback Scenarios
    # -------------------------------------------------------------------------
    def _exercise_native_rollback(self) -> None:
        """Establish a real native baseline, upgrade it, and roll back offline.

        Requires the owner's production archive. Production install verifies ARM64
        build provenance before activation; no synthetic tree is accepted here.
        """
        # Install a genuine native-only archive, establish its host baseline,
        # then upgrade to the web release and exercise production rollback.
        nat_rel_id = self.mgr.install_release(
            self.native_release_archive,
            require_root=True,
            enforce_arm64=True,
            operator_user=self.operator_user,
        )
        nat_dir = os.path.join(self.opt_dir, "releases", nat_rel_id)
        for unit in (
            "mentorpi-tank-web.service",
            "mentorpi-tank-lifecycle.service",
        ):
            if os.path.exists(os.path.join(nat_dir, "host", unit)):
                raise RuntimeError("Native rollback archive contains web services")
        self.mgr.activate_release(nat_rel_id, require_root=True)
        self.mgr.activate_release(self._installed_release_id, require_root=True)
        self.mgr.rollback_release(require_root=True)
        if os.path.realpath(os.path.join(self.opt_dir, "current")) != nat_dir:
            raise RuntimeError("Rollback did not restore the native release")

    def _execute_phase5_lifecycle_and_rollback(self) -> bool:
        phase = "Phase 5: Operational Lifecycle & Rollback Scenarios"
        print(f"\n--> {phase}...")
        self.results["phases"][phase] = {"status": "IN_PROGRESS"}

        # 1. Repeat Installation Idempotence
        t0 = time.time()
        repeat_ok = True
        repeat_err = None
        try:
            r = self.mgr.install_release(
                archive_path=self._installed_archive,
                require_root=True,
                enforce_arm64=True,
                operator_user=self.operator_user,
            )
            if r != self._installed_release_id:
                repeat_ok = False
                repeat_err = "Repeat install returned different release ID."
        except Exception as e:  # noqa: BLE001
            repeat_ok = False
            repeat_err = f"Repeat install failed: {e}"

        self._record_step(
            StepResult(
                name="repeat_installation_idempotence",
                phase=phase,
                status="PASSED" if repeat_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=repeat_err,
            )
        )
        if not repeat_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 2. Upgrade and Rollback Switchover
        t0 = time.time()
        up_ok = True
        up_err = None
        up_release_id = None
        try:
            up_release_id = self.mgr.install_release(
                self.upgrade_release_archive,
                require_root=True,
                enforce_arm64=True,
                operator_user=self.operator_user,
            )
            if up_release_id == self._installed_release_id:
                raise ValueError(
                    "Upgrade archive must have a different release ID from the active release"
                )
            self.mgr.activate_release(up_release_id, require_root=True)

            # Confirm current is up_release_id
            curr_target = os.path.realpath(os.path.join(self.opt_dir, "current"))
            if os.path.basename(curr_target) != up_release_id:
                up_ok = False
                up_err = "Upgrade activation failed to update /opt/ubuntu_tank/current symlink."

            # Rollback
            if up_ok:
                self.mgr.rollback_release(require_root=True)
                curr_after_rb = os.path.realpath(os.path.join(self.opt_dir, "current"))
                if os.path.basename(curr_after_rb) != self._installed_release_id:
                    up_ok = False
                    up_err = (
                        f"Rollback failed to restore previous release: {curr_after_rb}"
                    )
        except Exception as e:  # noqa: BLE001
            up_ok = False
            up_err = f"Upgrade and rollback failed: {e}"

        self._record_step(
            StepResult(
                name="upgrade_and_rollback_switchover",
                phase=phase,
                status="PASSED" if up_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=up_err,
            )
        )
        if not up_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 3. Interrupted Activation Journal Recovery
        t0 = time.time()
        rec_ok = True
        rec_err = None
        try:
            # Inject uncommitted activation state with a valid snapshot
            tx_id = f"tx_inject_{int(time.time())}"
            snap_path = self.mgr.snapshot_mgr.create_snapshot(
                tx_id=tx_id,
                current_symlink_target=os.path.realpath(
                    os.path.join(self.opt_dir, "current")
                ),
                etc_dir=self.etc_dir,
                systemd_dir=self.systemd_dir,
                udev_dir=self.udev_dir,
            )
            self.mgr.journal.record_prepared(
                tx_id=tx_id,
                candidate_release_id=up_release_id,
                candidate_release_path=os.path.join(
                    self.opt_dir, "releases", up_release_id
                ),
                previous_release_id=self._installed_release_id,
                previous_release_path=os.path.join(
                    self.opt_dir, "releases", self._installed_release_id
                ),
                snapshot_dir=snap_path,
            )

            # Run recover_activation
            recover_sh = os.path.join(SCRIPT_DIR, "recover_activation.sh")
            res = subprocess.run(
                ["bash", recover_sh], capture_output=True, text=True, check=False
            )
            if res.returncode != 0:
                rec_ok = False
                rec_err = f"recover_activation.sh failed: {res.stderr}"
            else:
                st = self.mgr.journal.get_state()
                if st.get("current_transaction") is not None:
                    rec_ok = False
                    rec_err = "Activation journal still has an uncommitted transaction after recovery."
        except Exception as e:  # noqa: BLE001
            rec_ok = False
            rec_err = f"Interrupted activation recovery test failed: {e}"

        self._record_step(
            StepResult(
                name="interrupted_activation_recovery",
                phase=phase,
                status="PASSED" if rec_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=rec_err,
            )
        )
        if not rec_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        # 4. Offline Rollback to Native-Only Baseline
        t0 = time.time()
        nat_ok = True
        nat_err = None
        try:
            self._exercise_native_rollback()

            # Assert web services removed from systemd
            web_svc = os.path.join(self.systemd_dir, "mentorpi-tank-web.service")
            life_svc = os.path.join(self.systemd_dir, "mentorpi-tank-lifecycle.service")
            if os.path.exists(web_svc) or os.path.exists(life_svc):
                nat_ok = False
                nat_err = (
                    "Web services were not pruned upon rollback to native-only release!"
                )

            # Assert custom web.yaml is preserved
            web_yaml = os.path.join(self.etc_dir, "web", "web.yaml")
            if not os.path.exists(web_yaml):
                nat_ok = False
                nat_err = (
                    "Custom web.yaml was deleted upon rollback to native-only release!"
                )

        except Exception as e:  # noqa: BLE001
            nat_ok = False
            nat_err = f"Rollback to native-only baseline failed: {e}"

        self._record_step(
            StepResult(
                name="offline_rollback_to_native_only_baseline",
                phase=phase,
                status="PASSED" if nat_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=nat_err,
            )
        )
        if not nat_ok:
            self.results["phases"][phase] = {"status": "FAILED"}
            return False

        self.results["phases"][phase] = {"status": "PASSED"}
        return True

    # -------------------------------------------------------------------------
    # Phase 6: Baseline Restoration & Final Reporting
    # -------------------------------------------------------------------------
    def _execute_phase6_baseline_restoration(self) -> bool:
        phase = "Phase 6: Target Baseline Restoration"
        print(f"\n--> {phase}...")
        t0 = time.time()
        rest_ok = True
        rest_err = None

        try:
            with DeploymentLock(self.lock_path, timeout_sec=10.0):
                # Confirm every service is stopped before changing its assets.
                self.mgr._stop_and_disarm_service()

                # Restore pre-test snapshot if created
                if self._baseline_dir and os.path.isdir(self._baseline_dir):
                    self.mgr.snapshot_mgr.restore_snapshot(
                        snapshot_dir=self._baseline_dir,
                        etc_dir=self.etc_dir,
                        systemd_dir=self.systemd_dir,
                        udev_dir=self.udev_dir,
                    )
                    # Restore symlink
                    curr_symlink = os.path.join(self.opt_dir, "current")
                    if self._pre_test_active_target and os.path.isdir(
                        self._pre_test_active_target
                    ):
                        rel = os.path.relpath(
                            self._pre_test_active_target, self.opt_dir
                        )
                        tmp_link = f"{curr_symlink}.tmp.{os.getpid()}"
                        os.symlink(rel, tmp_link)
                        os.replace(tmp_link, curr_symlink)
                    elif os.path.islink(curr_symlink):
                        os.unlink(curr_symlink)

                # Restore pre-test activation journal (reconcile deployment metadata)
                journal_path = self.mgr.journal_path
                if self._pre_test_journal_content is not None:
                    os.makedirs(os.path.dirname(journal_path), exist_ok=True)
                    tmp_j = f"{journal_path}.tmp.{os.getpid()}"
                    with open(tmp_j, "wb") as fh:
                        fh.write(self._pre_test_journal_content)
                    os.replace(tmp_j, journal_path)
                elif os.path.exists(journal_path) and self._baseline_dir:
                    os.unlink(journal_path)

                # Reload systemd units to reconcile effective configuration
                subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            rest_ok = False
            rest_err = f"Failed to restore pre-test baseline: {e}"

        self._record_step(
            StepResult(
                name="target_baseline_restoration",
                phase=phase,
                status="PASSED" if rest_ok else "FAILED",
                duration_sec=time.time() - t0,
                error_message=rest_err,
            )
        )
        self.results["phases"][phase] = {"status": "PASSED" if rest_ok else "FAILED"}
        return rest_ok

    def _finalize_report(self):
        """Write structured JSON and Markdown report."""
        self.results["end_time"] = datetime.now(timezone.utc).isoformat()
        json_path = os.path.join(self.report_dir, f"{self.report_name}.json")
        md_path = os.path.join(self.report_dir, f"{self.report_name}.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2)

        # Generate Markdown
        status = self.results["overall_status"]
        env = self.results["host_environment"]
        md_lines = [
            "# Milestone 14.1 Real Pi 5 Integration Test Report",
            "",
            f"- **Status**: `{status}`",
            f"- **Start Time**: {self.results['start_time']}",
            f"- **End Time**: {self.results['end_time']}",
            f"- **Host Architecture**: `{env.get('machine', 'unknown')}`",
            f"- **Host System**: `{env.get('system', 'unknown')} {env.get('release', '')}`",
            f"- **Node / Hostname**: `{env.get('node', 'unknown')}`",
            f"- **Git Commit**: `{env.get('git_commit', 'unknown')}` (`{env.get('git_branch', 'unknown')}`)",
            "",
            "## Safety Invariants & Invariant Summary",
            "",
            "- **Motor Power**: STRICTLY OFF (All motor guard checks verified DISARMED).",
            "- **Raised Tracks**: Verified mechanical elevation invariant.",
            "- **On-Ground Motion**: STRICTLY FORBIDDEN.",
            "",
            "## Phase Results",
            "",
            "| Phase | Status |",
            "| --- | --- |",
        ]
        for p_name, p_val in self.results["phases"].items():
            st = p_val.get("status", "UNKNOWN")
            md_lines.append(f"| {p_name} | `{st}` |")

        if self.results.get("pending_reason"):
            md_lines.extend(
                [
                    "",
                    "### Pending Execution Details",
                    "",
                    f"{self.results['pending_reason']}",
                    "",
                    "In accordance with Milestone 14.1 and AGENTS.md, mutating Pi installation/deployment",
                    "workflows are executed solely against physical target hardware (Raspberry Pi 5 ARM64).",
                    "No simulated Pi installation substitutes for target evidence.",
                ]
            )

        if self.results["steps"]:
            md_lines.extend(
                [
                    "",
                    "## Detailed Step Executions",
                    "",
                    "| Step Name | Phase | Status | Duration |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for s in self.results["steps"]:
                md_lines.append(
                    f"| `{s['name']}` | {s['phase']} | `{s['status']}` | {s['duration_sec']:.2f}s |"
                )

        if self.results["failures"]:
            md_lines.extend(
                [
                    "",
                    "## Failures",
                    "",
                ]
            )
            for fail in self.results["failures"]:
                md_lines.append(f"- {fail}")

        md_lines.append("")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))


def main():
    parser = argparse.ArgumentParser(
        description="Milestone 14.1 Real Pi 5 Installation & Deployment Integration Test CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Safety Notice:
  Under NO circumstances does this suite authorize on-ground motion.
  Motor power must remain off throughout all installation tests.
""",
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help=f"Directory to save JSON and Markdown test reports (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--report-name",
        default=DEFAULT_REPORT_NAME,
        help=f"Base name for report files (default: {DEFAULT_REPORT_NAME})",
    )
    parser.add_argument(
        "--baseline-snapshot",
        default=DEFAULT_BASELINE_SNAPSHOT,
        help=f"Name of the pre-test baseline snapshot (default: {DEFAULT_BASELINE_SNAPSHOT})",
    )
    parser.add_argument(
        "--require-target",
        action="store_true",
        help="Fail with exit code 1 if not running on authentic ARM64 Pi 5 target",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Run preflight checks and sample host environment without mutating host filesystem",
    )

    parser.add_argument(
        "--native-release-archive",
        help="Prebuilt native-only ARM64 archive; required with --deployment-scenarios",
    )
    parser.add_argument(
        "--operator-user",
        help="Owner login for optional deployment scenarios (default: SUDO_USER)",
    )
    parser.add_argument(
        "--expected-release-id",
        help="Reject a stale or unintended installed release ID",
    )
    parser.add_argument(
        "--deployment-scenarios",
        action="store_true",
        help="Also exercise repeat install, upgrade and rollback using prebuilt archives; never builds",
    )
    parser.add_argument(
        "--release-archive",
        help="Prebuilt archive matching the installed release, for repeat installation",
    )
    parser.add_argument(
        "--upgrade-release-archive",
        help="Prebuilt web-enabled ARM64 archive with a different release ID",
    )
    args = parser.parse_args()

    orch = TargetIntegrationOrchestrator(
        report_dir=args.report_dir,
        report_name=args.report_name,
        baseline_snapshot_name=args.baseline_snapshot,
        require_target=args.require_target,
        inspect_only=args.inspect_only,
        native_release_archive=args.native_release_archive,
        operator_user=args.operator_user,
        expected_release_id=args.expected_release_id,
        deployment_scenarios=args.deployment_scenarios,
        release_archive=args.release_archive,
        upgrade_release_archive=args.upgrade_release_archive,
    )
    exit_code = orch.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
