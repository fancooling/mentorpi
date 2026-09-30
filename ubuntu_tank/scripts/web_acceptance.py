#!/usr/bin/env python3
"""
web_acceptance.py - Milestone 15 Raised-Track Web Movement & Failure Acceptance Orchestrator.

Mandatory safety invariant:
Under NO circumstances does this suite authorize on-ground motion.
All actuation tests require --ack-tracks-raised confirming the tank chassis is
mechanically elevated with tracks completely clear of the surface.

Validates:
1. Physical safety acknowledgment: elevated chassis, clear tracks, accessible disconnect.
2. Hardware preflight: USB identity (1a86:55d4), battery voltage (>= 9.6V),
   DeploymentLock exclusivity, container/service mutual exclusion.
3. Web movement acceptance across all four directions (forward, reverse, spin_left, spin_right)
   via buttons and keyboard at conservative speed.
4. Safety controls: button/key release, Space emergency stop, Stop, Release control,
   30-second idle timeout, and 5.0-second continuous hold cap.
5. Instrumented stop latencies across all 8 web failure conditions (Target <= 300.0 ms):
   - loss_of_focus: window blur / focus loss -> local input cleared, lease cancelled
   - tab_close: tab close / visibility hidden -> visibilitychange -> stop/disarm
   - browser_crash: WebSocket drop -> agent drops ownership & stops
   - wifi_loss: Wi-Fi disconnect / ping timeout -> ownership revoked
   - delayed_buffered_packets: delayed packet beyond lease deadline -> lease expired (> 150 ms)
   - web_crash_hang: web service crash / event loop stall -> relay closed -> agent lease expires
   - operator_crash_hang: operator agent crash/hang -> guard freshness timeout (250 ms) -> 4-motor zero
   - reconnect_behavior: client reconnects disarmed, no motion resumed
6. Re-verified affected native downstream failure latencies:
   - guard_freshness_timeout: <= 300.0 ms
   - bridge_crash: <= 250.0 ms
   - service_stop_sigterm: <= 100.0 ms
   - serial_disconnect: <= 600.0 ms
   - host_shutdown: <= 100.0 ms
7. Separate latency breakdown recording: event-to-agent, zero-write, and physical-stop timing.
8. PWA / mobile controls: touch cancellation, app switching, screen lock, resume, updates.
9. Physical observation capture and strict acceptance validation:
   Mocks, request acknowledgments, or serial writes alone NEVER mark physical acceptance passed.
10. Structured JSON and Markdown report generation.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import fcntl
import hashlib
import json
import math
import os
import platform
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import yaml

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
    "ubuntu_tank_operator",
    "ubuntu_tank_protocol",
    "ubuntu_tank_web",
    "controller",
    "ros_robot_controller",
    "ubuntu_tank_bringup",
]:
    pkg_path = os.path.join(SRC_DIR, pkg)
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)

from deployment_manager import (
    DEFAULT_LOCK_PATH,
    check_hardware_mutual_exclusion,
    parse_release_manifest,
)
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

# Hardware & Platform Constants
EXPECTED_ARCH = "aarch64"
EXPECTED_OS_ID = "ubuntu"
EXPECTED_OS_VERSION = "26.04"
EXPECTED_USB_VENDOR = "1a86"
EXPECTED_USB_PRODUCT = "55d4"
MIN_BATTERY_VOLTAGE_MV = 9600  # 9.6V cutoff for 3S LiPo

# Accepted Web Latency Bounds (milliseconds) per Design §7 Milestone 15
ACCEPTED_WEB_LATENCY_BOUNDS_MS: dict[str, float] = {
    # Web / Browser / Network failure modes (Section 7 Target: <= 300 ms)
    "loss_of_focus": 300.0,
    "tab_close": 300.0,
    "browser_crash": 300.0,
    "wifi_loss": 300.0,
    "delayed_buffered_packets": 300.0,
    "web_crash_hang": 300.0,
    "operator_crash_hang": 300.0,
    "reconnect_behavior": 300.0,
    # Re-verified affected native downstream failure modes
    "guard_freshness_timeout": 300.0,
    "bridge_crash": 250.0,
    "service_stop_sigterm": 100.0,
    "serial_disconnect": 600.0,
    "serial_loss": 600.0,
    "host_shutdown": 100.0,
}

REQUIRED_WEB_FAILURE_CONDITIONS = [
    "loss_of_focus",
    "tab_close",
    "browser_crash",
    "wifi_loss",
    "delayed_buffered_packets",
    "web_crash_hang",
    "operator_crash_hang",
    "reconnect_behavior",
    "guard_freshness_timeout",
    "bridge_crash",
    "service_stop_sigterm",
    "serial_disconnect",
    "host_shutdown",
]
REQUIRED_FAILURE_CONDITIONS = REQUIRED_WEB_FAILURE_CONDITIONS

REQUIRED_MOTIONS = ("forward", "reverse", "spin_left", "spin_right")

REQUIRED_SAFETY_CONTROLS = (
    "key_release",
    "space_stop",
    "stop",
    "release_control",
    "idle_timeout",
    "hold_cap",
)
CONTROLS_REQUIRING_DISARM = (
    "space_stop",
    "stop",
    "release_control",
    "idle_timeout",
    "hold_cap",
)

REQUIRED_PWA_CONTROLS = (
    "touch_cancellation",
    "app_switching",
    "screen_lock",
    "resume",
    "disarmed_updates",
)

REQUIRED_INPUT_METHODS = ("button", "keyboard")
REQUIRED_BREAKDOWN_FIELDS = (
    "event_to_agent_ms",
    "zero_write_ms",
    "physical_stop_ms",
)
REQUIRED_CLIENT_VERSION_FIELDS = (
    "desktop_browser",
    "android_pwa",
    "ios_pwa",
)

WEB_LATENCY_MECHANISMS = {
    "loss_of_focus": "Window blur / focus loss -> local input cleared -> zero velocity sent -> lease cancelled",
    "tab_close": "Page unload / visibility hidden -> visibilitychange event -> stop/disarm dispatched",
    "browser_crash": "WebSocket disconnect (EOF/RST) -> agent drops ownership -> safe stop dispatched",
    "wifi_loss": "Network transport loss -> heartbeat/ping timeout -> agent revokes authority",
    "delayed_buffered_packets": "Delayed or buffered packet exceeds lease deadline (> 150 ms) -> agent drops motion",
    "web_crash_hang": "Web process killed or event loop stalled -> relay socket closed -> agent expires motion lease",
    "operator_crash_hang": "Operator agent crash/hang -> controller/guard freshness timeout trips (250 ms) -> 4-motor zero",
    "reconnect_behavior": "Client reconnects after drop -> verifies ownership empty or re-acquired in OWNED_DISARMED, zero motion resumed",
    "guard_freshness_timeout": "MotorGuard 250 ms freshness timeout -> 4-motor zero emitted",
    "bridge_crash": "Bridge child crash -> supervisor detects dead child -> terminates siblings with safe zero",
    "service_stop_sigterm": "Signal handler catches SIGTERM -> zero_motors(count=4) dispatched before service stop",
    "serial_disconnect": "Serial silence watchdog (500 ms) trips -> fatal fault -> port closure & safe zero fallback",
    "serial_loss": "Serial silence watchdog (500 ms) trips -> fatal fault -> port closure & safe zero fallback",
    "host_shutdown": "Systemd host shutdown SIGTERM -> zero_motors(count=4) dispatched before poweroff",
}


def is_valid_duration_ms(val: Any) -> bool:
    """Return True if val is a non-boolean, finite, nonnegative number (int or float)."""
    if val is None or isinstance(val, bool):
        return False
    if not isinstance(val, (int, float)):
        return False
    if math.isnan(val) or math.isinf(val):
        return False
    return val >= 0.0


def is_exact_bool_true(val: Any) -> bool:
    """Return True if and only if val is strictly the Python boolean True."""
    return isinstance(val, bool) and val is True


def get_system_boot_id() -> str:
    """Return the system boot UUID from /proc/sys/kernel/random/boot_id if available."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return ""


class WebAcceptanceOrchestrator:
    """Orchestrates Milestone 15 Raised-Track Web Movement and Failure Acceptance."""

    def __init__(
        self,
        ack_tracks_raised: bool = False,
        report_dir: str | None = None,
        report_name: str = "web-acceptance-report-milestone15",
        mock: bool = False,
        require_target: bool = False,
        physical_observations: dict[str, Any] | None = None,
        interactive_observations: bool = False,
        expected_release_id: str | None = None,
        opt_dir: str = "/opt/ubuntu_tank",
        etc_dir: str = "/etc/opt/ubuntu_tank",
        lock_path: str = DEFAULT_LOCK_PATH,
        serial_dev: str = "/dev/rrc",
        resume_campaign: str | None = None,
        campaign_file: str | None = None,
    ):
        self.ack_tracks_raised = ack_tracks_raised
        self.report_dir = report_dir or os.path.join(WORKSPACE_ROOT, "dist")
        self.report_name = report_name
        self.mock = mock
        self.require_target = require_target
        self.physical_observations = physical_observations
        self.interactive_observations = interactive_observations
        self.expected_release_id = expected_release_id
        self.opt_dir = opt_dir
        self.etc_dir = etc_dir
        self.lock_path = lock_path
        self.serial_dev = serial_dev
        self.resume_campaign = resume_campaign
        if self.resume_campaign:
            self.interactive_observations = True
        self.campaign_file = campaign_file or os.path.join(
            self.report_dir, "web-acceptance-campaign.json"
        )

        self.overall_status = "PENDING"
        self.preflight_errors: list[str] = []
        self.observation_errors: list[str] = []
        self.motion_test_results: dict[str, Any] = {}
        self.verified_latencies: dict[str, Any] = {}
        self.environment_metadata: dict[str, Any] = {}
        self.live_run_evidence: dict[str, Any] = {}
        self._lock_fd: int | None = None
        self._live_session_verified = False
        self.start_time = time.time()

    def check_target_platform(self) -> tuple[bool, str]:
        """Check whether execution is on authentic Raspberry Pi 5 ARM64 Ubuntu 26.04."""
        machine = platform.machine()
        if machine != EXPECTED_ARCH:
            return (
                False,
                f"Architecture mismatch: expected {EXPECTED_ARCH}, detected {machine}",
            )

        if not os.path.exists("/etc/os-release"):
            return False, "Missing /etc/os-release"

        os_info: dict[str, str] = {}
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os_info[k] = v.strip("\"'")

        os_id = os_info.get("ID", "").lower()
        if os_id != EXPECTED_OS_ID:
            return (
                False,
                f"OS mismatch: expected {EXPECTED_OS_ID}, detected {os_id}",
            )

        ver = os_info.get("VERSION_ID", "")
        if not ver.startswith(EXPECTED_OS_VERSION):
            return (
                False,
                f"OS version mismatch: expected {EXPECTED_OS_VERSION}, detected {ver}",
            )

        # Check Pi 5 device tree model if readable
        model_file = "/proc/device-tree/model"
        if os.path.exists(model_file):
            try:
                with open(model_file, "r", encoding="utf-8", errors="replace") as f:
                    model = f.read()
                if "Raspberry Pi 5" not in model:
                    return (
                        False,
                        f"Hardware model mismatch: expected Raspberry Pi 5, detected {model.strip()}",
                    )
            except (OSError, UnicodeError) as e:
                return False, f"Could not read device-tree model: {e}"

        return True, f"Authentic target platform: {machine}, {os_id} {ver}"

    @contextlib.contextmanager
    def deployment_read_lock(self):
        """Hold the installed deployment lock shared for the complete live run.

        Acceptance is read-only, so it must coordinate with root-owned deployment
        operations without requiring write access to their lock file. Keeping the
        shared lock through final status verification prevents activation or
        rollback from changing the release while evidence is being collected.
        """
        if not os.path.isfile(self.lock_path):
            raise FileNotFoundError(
                f"Installed deployment lock is missing: {self.lock_path}"
            )
        fd = os.open(self.lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self._lock_fd = fd
            yield
        finally:
            self._lock_fd = None
            os.close(fd)

    def managed_bridge_pid(self) -> int:
        """Return the single live serial bridge PID owned by the managed service.

        The bridge is the expected `/dev/rrc` holder during web acceptance. Its
        PID is accepted only after checking both the systemd cgroup and executable
        identity; every unrelated device holder remains a preflight failure.
        """
        info = subprocess.run(
            [
                "systemctl",
                "show",
                "mentorpi-tank.service",
                "--property=ActiveState",
                "--property=ControlGroup",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=3.0,
        )
        fields = dict(
            line.split("=", 1) for line in info.stdout.splitlines() if "=" in line
        )
        group = fields.get("ControlGroup", "")
        if fields.get("ActiveState") != "active" or not group or group == "/":
            raise RuntimeError(
                "mentorpi-tank.service must be active for web acceptance"
            )

        candidates: set[int] = set()
        cgroup_dir = os.path.join("/sys/fs/cgroup", group.lstrip("/"))
        for directory, _, files in os.walk(cgroup_dir):
            if "cgroup.procs" not in files:
                continue
            with open(
                os.path.join(directory, "cgroup.procs"), encoding="utf-8"
            ) as stream:
                pids = stream.read().split()
            for raw_pid in pids:
                pid = int(raw_pid)
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as stream:
                        args = stream.read().split(b"\0")
                    if not any(
                        arg.endswith(b"/lib/ros_robot_controller/ros_robot_controller")
                        for arg in args
                    ):
                        continue
                    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as stream:
                        groups = [line.strip().split(":", 2)[-1] for line in stream]
                    if any(
                        value == group or value.startswith(group + "/")
                        for value in groups
                    ):
                        candidates.add(pid)
                except FileNotFoundError:
                    continue
        if len(candidates) != 1:
            raise RuntimeError(
                "Expected exactly one running bridge in mentorpi-tank.service"
            )
        return next(iter(candidates))

    def verify_usb_identity(self) -> tuple[bool, str]:
        """Verify `/dev/rrc` is a character device backed by the accepted USB ID."""
        if not os.path.exists(self.serial_dev):
            return False, f"Required serial device is missing: {self.serial_dev}"
        real_dev = os.path.realpath(self.serial_dev)
        try:
            if not stat.S_ISCHR(os.stat(real_dev).st_mode):
                return False, f"Serial path is not a character device: {real_dev}"
        except OSError as exc:
            return False, f"Could not inspect serial device {real_dev}: {exc}"

        tty_name = os.path.basename(real_dev)
        current = os.path.realpath(os.path.join("/sys/class/tty", tty_name, "device"))
        while current and current != "/":
            vendor_path = os.path.join(current, "idVendor")
            product_path = os.path.join(current, "idProduct")
            if os.path.isfile(vendor_path) and os.path.isfile(product_path):
                try:
                    with open(vendor_path, encoding="utf-8") as stream:
                        vendor = stream.read().strip().lower()
                    with open(product_path, encoding="utf-8") as stream:
                        product = stream.read().strip().lower()
                except OSError as exc:
                    return False, f"Could not read USB identity: {exc}"
                if vendor == EXPECTED_USB_VENDOR and product == EXPECTED_USB_PRODUCT:
                    return (
                        True,
                        f"Verified USB identity {vendor}:{product} at {real_dev}",
                    )
                return False, (
                    f"USB identity mismatch at {real_dev}: {vendor}:{product} "
                    f"(expected {EXPECTED_USB_VENDOR}:{EXPECTED_USB_PRODUCT})"
                )
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return False, f"Could not resolve USB identity for {real_dev} through sysfs"

    def _load_web_config(self) -> tuple[WebControlConfig, str]:
        """Load web configuration through its production consumer and hash it."""
        config_path = os.path.join(self.etc_dir, "web", "web.yaml")
        with open(config_path, "rb") as stream:
            raw = stream.read()
        parsed = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(parsed, dict):
            raise TypeError(f"Web configuration must be a mapping: {config_path}")
        return WebControlConfig.from_dict(parsed), hashlib.sha256(raw).hexdigest()

    def _fetch_web_json(self, base_url: str, path: str) -> dict[str, Any]:
        """Fetch a live JSON API response from the installed local web service."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        request = urllib.request.Request(
            base_url + path,
            headers={"Accept": "application/json", "Origin": base_url},
            method="GET",
        )
        with urllib.request.urlopen(request, context=context, timeout=3.0) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise TypeError(f"Web API returned a non-object payload for {path}")
        return payload

    def probe_web_runtime(self) -> dict[str, Any]:
        """Verify the installed HTTPS/API path and return correlated live status."""
        config, config_sha256 = self._load_web_config()
        address = config.listen_address
        if address in ("0.0.0.0", "::", "localhost"):
            address = "127.0.0.1"
        base_url = f"https://{address}:{config.port}"
        version = self._fetch_web_json(base_url, "/api/v1/version")
        status_payload = self._fetch_web_json(base_url, "/api/v1/status")
        active_release = self.environment_metadata.get("active_release_id")
        if active_release and version.get("release_id") != active_release:
            raise RuntimeError(
                "Installed web API release does not match active release: "
                f"{version.get('release_id')!r} != {active_release!r}"
            )
        battery_voltage = status_payload.get("battery_voltage")
        if (
            not isinstance(battery_voltage, (int, float))
            or isinstance(battery_voltage, bool)
            or not math.isfinite(float(battery_voltage))
            or float(battery_voltage) < MIN_BATTERY_VOLTAGE_MV / 1000.0
        ):
            raise RuntimeError(
                "Live web status lacks safe fresh battery telemetry at or above "
                f"{MIN_BATTERY_VOLTAGE_MV / 1000.0:.1f} V"
            )
        return {
            "base_url": base_url,
            "release_id": version.get("release_id"),
            "protocol_version": version.get("protocol_version"),
            "web_config_sha256": config_sha256,
            "service_state": status_payload.get("service_state"),
            "operator_state": status_payload.get("operator_state"),
            "guard_armed": status_payload.get("guard_armed"),
            "linear_speed": status_payload.get("linear_speed"),
            "angular_speed": status_payload.get("angular_speed"),
            "battery_voltage": battery_voltage,
        }

    @staticmethod
    def _status_is_safely_stopped(probe: dict[str, Any]) -> bool:
        """Return whether a live web probe reports stopped and disarmed state."""
        linear = probe.get("linear_speed")
        angular = probe.get("angular_speed")
        numeric_zero = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and abs(float(value)) <= 1e-6
            for value in (linear, angular)
        )
        return (
            probe.get("guard_armed") is not True
            and probe.get("operator_state") in ("NO_OWNER", "OWNED_DISARMED")
            and numeric_zero
        )

    def run_preflight_checks(self) -> tuple[bool, list[str]]:
        """Verify safety acknowledgment, hardware locks, devices, and mutual exclusion."""
        errors: list[str] = []

        # 1. Physical safety acknowledgment is strictly mandatory
        if not self.ack_tracks_raised:
            errors.append(
                "Safety violation: --ack-tracks-raised was NOT provided. "
                "All motion acceptance requires physical confirmation that the tank chassis "
                "is mechanically elevated and tracks are completely clear of any surface."
            )
            return False, errors

        # 2. Check target platform
        is_target, plat_msg = self.check_target_platform()
        if not is_target:
            if self.require_target:
                errors.append(
                    f"--require-target specified but platform check failed: {plat_msg}"
                )
                return False, errors
            self.environment_metadata["platform_note"] = (
                f"Non-target environment detected ({plat_msg}). "
                f"Physical acceptance remains pending execution on target ARM64 Pi 5 hardware."
            )

        # 3. The complete target run must already hold the shared deployment lock.
        if is_target and not self.mock and self._lock_fd is None:
            errors.append(
                "Live acceptance did not retain the shared deployment lock for the full run"
            )

        # 4. Managed service/device ownership and hardware mutual exclusion.
        if is_target and not self.mock:
            bridge_pid = None
            try:
                bridge_pid = self.managed_bridge_pid()
                self.environment_metadata["managed_bridge_pid"] = bridge_pid
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
            ) as exc:
                errors.append(f"Managed bridge verification failed: {exc}")
            mut_ok, mut_errs = check_hardware_mutual_exclusion(
                serial_dev=self.serial_dev,
                allowed_serial_pid=bridge_pid,
            )
            if not mut_ok:
                errors.extend(mut_errs)

            usb_ok, usb_message = self.verify_usb_identity()
            if not usb_ok:
                errors.append(f"USB identity verification failed: {usb_message}")
            else:
                self.environment_metadata["usb_identity"] = usb_message

        # 5. Installed release validation (on target)
        current_link = os.path.join(self.opt_dir, "current")
        if is_target and not self.mock:
            if not os.path.islink(current_link):
                errors.append(f"No active release symlink at {current_link}")
            else:
                manifest_path = os.path.join(current_link, "release-manifest.txt")
                if not os.path.isfile(manifest_path):
                    errors.append(
                        f"Release manifest missing in active release: {manifest_path}"
                    )
                else:
                    try:
                        headers, _ = parse_release_manifest(manifest_path)
                        active_id = headers.get("Release-Id")
                        if not active_id:
                            errors.append(
                                f"Active release manifest lacks Release-Id: {manifest_path}"
                            )
                        if (
                            self.expected_release_id
                            and active_id != self.expected_release_id
                        ):
                            errors.append(
                                f"Active release ID '{active_id}' does not match expected '{self.expected_release_id}'"
                            )
                        if headers.get("Target-Architecture") not in (
                            "arm64",
                            "aarch64",
                        ):
                            errors.append(
                                "Active release manifest does not target ARM64: "
                                f"{headers.get('Target-Architecture')!r}"
                            )
                        self.environment_metadata["active_release_id"] = active_id
                        self.environment_metadata["target_arch"] = headers.get(
                            "Target-Architecture"
                        )
                    except (OSError, ValueError) as exc:
                        errors.append(f"Could not parse active release manifest: {exc}")

        # 6. Verify the installed web service and live battery telemetry through
        # the actual HTTPS/API consumer path after release identity is known.
        if is_target and not self.mock and not errors:
            try:
                probe = self.probe_web_runtime()
                if probe.get("service_state") != "active":
                    errors.append(
                        "mentorpi-tank.service must be active for movement acceptance"
                    )
                else:
                    self.environment_metadata["web_preflight"] = probe
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                errors.append(f"Installed web runtime preflight failed: {exc}")

        self.preflight_errors = errors
        return (len(errors) == 0), errors

    def validate_physical_movement_observations(
        self, obs_movements: Any
    ) -> tuple[bool, list[str]]:
        """Validate observed track movements on elevated chassis across all 4 directions."""
        errors: list[str] = []
        if not obs_movements or not isinstance(obs_movements, dict):
            return False, [
                "Missing 'observed_movements' dictionary in physical observations"
            ]

        for m in REQUIRED_MOTIONS:
            if m not in obs_movements:
                errors.append(f"Missing physical observation for required motion '{m}'")
                continue
            entry = obs_movements[m]
            if not isinstance(entry, dict):
                errors.append(
                    f"Physical observation entry for '{m}' must be a dictionary"
                )
                continue
            if not is_exact_bool_true(entry.get("observed")):
                errors.append(
                    f"Physical motion '{m}' was not observed on raised tracks (must be boolean true)"
                )
            if not is_exact_bool_true(entry.get("direction_matched")):
                errors.append(
                    f"Physical motion '{m}' did not match commanded direction (must be boolean true)"
                )
            if not is_exact_bool_true(entry.get("stopped_after_burst")):
                errors.append(
                    f"Physical motion '{m}' did not come to a complete stop after burst (must be boolean true)"
                )
            methods = entry.get("input_methods")
            if not isinstance(methods, dict):
                errors.append(
                    f"Physical motion '{m}' lacks button and keyboard input-method evidence"
                )
            else:
                for method in REQUIRED_INPUT_METHODS:
                    if not is_exact_bool_true(methods.get(method)):
                        errors.append(
                            f"Physical motion '{m}' was not verified via {method} input"
                        )
            obs_name = entry.get("observer")
            if not obs_name or not isinstance(obs_name, str) or not obs_name.strip():
                errors.append(
                    f"Physical observation '{m}' lacks non-empty observer identity"
                )

        return (len(errors) == 0), errors

    def validate_safety_control_observations(
        self, obs_controls: Any
    ) -> tuple[bool, list[str]]:
        """Validate observed web driving safety controls (direction release, Space, Stop, Release control, timeout, cap)."""
        errors: list[str] = []
        if not obs_controls or not isinstance(obs_controls, dict):
            return False, [
                "Missing 'safety_controls' dictionary in physical observations"
            ]

        for c in REQUIRED_SAFETY_CONTROLS:
            if c not in obs_controls:
                errors.append(f"Missing observation for required safety control '{c}'")
                continue
            entry = obs_controls[c]
            if not isinstance(entry, dict):
                errors.append(f"Safety control entry for '{c}' must be a dictionary")
                continue
            if not is_exact_bool_true(entry.get("verified")):
                errors.append(
                    f"Safety control '{c}' was not verified to halt motion (must be boolean true)"
                )
            if c in CONTROLS_REQUIRING_DISARM:
                if not is_exact_bool_true(entry.get("motion_disarmed")):
                    errors.append(
                        f"Safety control '{c}' did not leave motors in disarmed/stopped state (must be boolean true)"
                    )
            else:
                # Key/button release transitions to ARMED_IDLE with zero velocity.
                # Disarming is not expected. Motion halting is verified via 'verified'.
                # If 'motion_stopped' is explicitly provided, it must be True.
                if "motion_stopped" in entry and not is_exact_bool_true(
                    entry.get("motion_stopped")
                ):
                    errors.append(
                        f"Safety control '{c}' did not physically halt motion (must be boolean true)"
                    )
            if c == "release_control" and not is_exact_bool_true(
                entry.get("controller_inactive")
            ):
                errors.append("Release control did not confirm the controller inactive")
            obs_name = entry.get("observer")
            if not obs_name or not isinstance(obs_name, str) or not obs_name.strip():
                errors.append(f"Safety control '{c}' lacks non-empty observer identity")

        return (len(errors) == 0), errors

    def validate_pwa_control_observations(self, obs_pwa: Any) -> tuple[bool, list[str]]:
        """Validate mobile PWA interaction observations (touch cancel, app switch, screen lock, resume)."""
        errors: list[str] = []
        if not obs_pwa or not isinstance(obs_pwa, dict):
            return False, ["Missing 'pwa_controls' dictionary in physical observations"]

        for p in REQUIRED_PWA_CONTROLS:
            if p not in obs_pwa:
                errors.append(f"Missing observation for required PWA interaction '{p}'")
                continue
            entry = obs_pwa[p]
            if not isinstance(entry, dict):
                errors.append(f"PWA interaction entry for '{p}' must be a dictionary")
                continue
            if not is_exact_bool_true(entry.get("verified")):
                errors.append(
                    f"PWA interaction '{p}' was not verified (must be boolean true)"
                )
            if not is_exact_bool_true(entry.get("no_latched_motion")):
                errors.append(
                    f"PWA interaction '{p}' did not prevent latched motion (must be boolean true)"
                )

        return (len(errors) == 0), errors

    def validate_web_latencies(self, latencies: Any) -> tuple[bool, list[str]]:
        """Validate measured physical stop latencies across all 8 web and 5 native failure modes."""
        errors: list[str] = []
        if not latencies or not isinstance(latencies, dict):
            return False, ["Missing 'latencies' dictionary in physical observations"]

        for cond in REQUIRED_FAILURE_CONDITIONS:
            entry = latencies.get(cond)
            if entry is None and cond == "serial_disconnect":
                entry = latencies.get("serial_loss")
            if entry is None:
                errors.append(
                    f"Missing required latency measurement for failure condition: '{cond}'"
                )
                continue
            if not isinstance(entry, dict):
                errors.append(f"Latency entry for '{cond}' must be a dictionary")
                continue

            measured = entry.get("measured_ms")
            bound = ACCEPTED_WEB_LATENCY_BOUNDS_MS[cond]
            if not is_valid_duration_ms(measured):
                errors.append(
                    f"Latency entry for '{cond}' has invalid duration {measured!r} "
                    f"(must be a non-boolean, finite, nonnegative number)"
                )
            elif measured > bound:
                errors.append(
                    f"Measured physical latency for '{cond}' ({measured} ms) exceeded bound ({bound} ms)"
                )

            if not is_exact_bool_true(entry.get("physical_stop_observed")):
                errors.append(
                    f"Failure condition '{cond}' lacks confirmed physical stop observation"
                )
            evidence_ref = entry.get("raw_evidence")
            if not isinstance(evidence_ref, str) or not evidence_ref.strip():
                errors.append(
                    f"Failure condition '{cond}' lacks a non-empty raw evidence reference"
                )

            # Every acceptance timing requires all three separately measured phases.
            breakdown = entry.get("breakdown")
            if not isinstance(breakdown, dict):
                errors.append(
                    f"Timing breakdown for '{cond}' must be a dictionary with all required phases"
                )
                continue
            breakdown_values: list[float] = []
            for field in REQUIRED_BREAKDOWN_FIELDS:
                val = breakdown.get(field)
                if not is_valid_duration_ms(val):
                    errors.append(
                        f"Timing breakdown field '{field}' for '{cond}' has invalid or missing duration {val!r}"
                    )
                else:
                    breakdown_values.append(float(val))
            if is_valid_duration_ms(measured) and len(breakdown_values) == len(
                REQUIRED_BREAKDOWN_FIELDS
            ):
                tolerance_ms = max(2.0, float(measured) * 0.05)
                if abs(sum(breakdown_values) - float(measured)) > tolerance_ms:
                    errors.append(
                        f"Timing breakdown for '{cond}' does not reconcile with measured total "
                        f"within {tolerance_ms:.1f} ms"
                    )

        return (len(errors) == 0), errors

    def validate_physical_observations(
        self, obs_data: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Validate complete owner physical observations and measurements against required schema."""
        errors: list[str] = []
        if not isinstance(obs_data, dict):
            return False, ["Physical observations payload must be a dictionary"]

        # 1. Validate 4-direction motion
        _, mov_errs = self.validate_physical_movement_observations(
            obs_data.get("observed_movements")
        )
        errors.extend(mov_errs)

        # 2. Validate safety controls
        _, ctrl_errs = self.validate_safety_control_observations(
            obs_data.get("safety_controls")
        )
        errors.extend(ctrl_errs)

        # 3. Validate PWA / mobile controls
        _, pwa_errs = self.validate_pwa_control_observations(
            obs_data.get("pwa_controls")
        )
        errors.extend(pwa_errs)

        # 4. Validate latencies across all failure modes
        _, lat_errs = self.validate_web_latencies(obs_data.get("latencies"))
        errors.extend(lat_errs)

        # 5. Validate observer identity and instruments metadata
        meta = obs_data.get("metadata", {})
        if not isinstance(meta, dict):
            errors.append("Physical observations 'metadata' must be a dictionary")
        else:
            observer = meta.get("physical_observer")
            if not observer or not isinstance(observer, str) or not observer.strip():
                errors.append(
                    "Physical observations metadata missing non-empty 'physical_observer'"
                )
            instruments = meta.get("instruments")
            if (
                not isinstance(instruments, list)
                or not instruments
                or not all(
                    isinstance(item, str) and item.strip() for item in instruments
                )
            ):
                errors.append(
                    "Physical observations metadata requires a non-empty 'instruments' list"
                )
            client_versions = meta.get("client_versions")
            if not isinstance(client_versions, dict):
                errors.append(
                    "Physical observations metadata missing 'client_versions' dictionary"
                )
            else:
                for field in REQUIRED_CLIENT_VERSION_FIELDS:
                    value = client_versions.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(
                            f"Physical observations metadata missing client version '{field}'"
                        )
            network_conditions = meta.get("network_conditions")
            if (
                not isinstance(network_conditions, str)
                or not network_conditions.strip()
            ):
                errors.append(
                    "Physical observations metadata missing non-empty 'network_conditions'"
                )
            raw_evidence = meta.get("raw_evidence")
            if (
                not isinstance(raw_evidence, list)
                or not raw_evidence
                or not all(
                    isinstance(item, str) and item.strip() for item in raw_evidence
                )
            ):
                errors.append(
                    "Physical observations metadata requires non-empty 'raw_evidence' references"
                )
            if not is_exact_bool_true(meta.get("power_disconnect_accessible")):
                errors.append(
                    "Physical observations must confirm the battery disconnect is immediately accessible"
                )

        self.observation_errors = errors
        return (len(errors) == 0), errors

    def prompt_physical_observations(
        self, partial_existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Collect complete current-run physical, client, and timing evidence."""
        partial_meta = (
            (partial_existing or {}).get("metadata", {}) if partial_existing else {}
        )
        partial_movements = (
            (partial_existing or {}).get("observed_movements", {})
            if partial_existing
            else {}
        )
        partial_controls = (
            (partial_existing or {}).get("safety_controls", {})
            if partial_existing
            else {}
        )
        partial_pwa = (
            (partial_existing or {}).get("pwa_controls", {}) if partial_existing else {}
        )
        partial_latencies = (
            (partial_existing or {}).get("latencies", {}) if partial_existing else {}
        )

        def ask_text(prompt: str, default: str | None = None) -> str:
            if default:
                val = input(f"{prompt}[{default}]: ").strip()
                return val if val else default
            value = input(prompt).strip()
            if not value:
                raise ValueError("A non-empty response is required")
            return value

        def ask_yes(prompt: str, default: bool | None = None) -> bool:
            if default is not None:
                d_str = "Y/n" if default else "y/N"
                val = input(f"{prompt} [{d_str}]: ").strip().lower()
                if not val:
                    return default
                return val in ("y", "yes")
            return input(prompt).strip().lower() in ("y", "yes")

        def ask_duration(prompt: str, default: float | None = None) -> float:
            if default is not None:
                val = input(f"{prompt}[{default}]: ").strip()
                if not val:
                    return default
                value = float(val)
            else:
                value = float(ask_text(prompt))
            if not is_valid_duration_ms(value):
                raise ValueError("Duration must be finite and nonnegative")
            return value

        print("\n" + "=" * 60)
        if partial_existing:
            print("Milestone 15: Resuming Web Movement & Failure Acceptance Campaign")
        else:
            print("Milestone 15: Web Movement & Failure Acceptance Entry")
        print("=" * 60)
        print(
            "Use the installed HTTPS page/PWA for every action. Record measurements "
            "from the instruments used during this run; do not reuse an older report."
        )
        try:
            observer = (
                partial_meta.get("physical_observer")
                if partial_existing and partial_meta.get("physical_observer")
                else ask_text("Physical observer name/initials: ")
            )
            if partial_existing and partial_meta.get("instruments"):
                instruments = partial_meta.get("instruments", [])
            else:
                instruments = [
                    item.strip()
                    for item in ask_text(
                        "Measurement instruments used (comma-separated): "
                    ).split(",")
                    if item.strip()
                ]
            if partial_existing and partial_meta.get("raw_evidence"):
                raw_evidence = partial_meta.get("raw_evidence", [])
            else:
                raw_evidence = [
                    item.strip()
                    for item in ask_text(
                        "Raw evidence file/recording references (comma-separated): "
                    ).split(",")
                    if item.strip()
                ]
            network_conditions = (
                partial_meta.get("network_conditions")
                if partial_existing and partial_meta.get("network_conditions")
                else ask_text(
                    "Network conditions (connection, distance, impairment setup): "
                )
            )
            clients = partial_meta.get("client_versions", {})
            desktop_version = (
                clients.get("desktop_browser")
                if partial_existing and clients.get("desktop_browser")
                else ask_text("Desktop browser and version: ")
            )
            android_version = (
                clients.get("android_pwa")
                if partial_existing and clients.get("android_pwa")
                else ask_text("Android device/PWA/browser version: ")
            )
            ios_version = (
                clients.get("ios_pwa")
                if partial_existing and clients.get("ios_pwa")
                else ask_text("iOS device/PWA/browser version: ")
            )
            disconnect_accessible = (
                partial_meta.get("power_disconnect_accessible")
                if partial_existing and "power_disconnect_accessible" in partial_meta
                else ask_yes(
                    "Is the physical battery disconnect immediately accessible? [y/N]: "
                )
            )

            print("\nConfirm raised-track motion through the installed web page:")
            movements: dict[str, Any] = {}
            for motion in REQUIRED_MOTIONS:
                if partial_movements.get(motion):
                    movements[motion] = partial_movements[motion]
                    continue
                print(f"\n--> Motion: {motion.upper()}")
                movements[motion] = {
                    "observed": ask_yes(
                        "  Did the tracks physically move during the bounded bursts? [y/N]: "
                    ),
                    "direction_matched": ask_yes(
                        f"  Did movement match {motion}? [y/N]: "
                    ),
                    "stopped_after_burst": ask_yes(
                        "  Did both tracks physically stop after each burst? [y/N]: "
                    ),
                    "input_methods": {
                        "button": ask_yes(
                            "  Was this direction tested with the on-screen button? [y/N]: "
                        ),
                        "keyboard": ask_yes(
                            "  Was this direction tested with the keyboard? [y/N]: "
                        ),
                    },
                    "observer": observer,
                }

            print("\n--> Verify Web Safety Controls:")
            controls: dict[str, Any] = {}
            for control in REQUIRED_SAFETY_CONTROLS:
                if partial_controls.get(control):
                    controls[control] = partial_controls[control]
                    continue
                if control in CONTROLS_REQUIRING_DISARM:
                    controls[control] = {
                        "verified": ask_yes(
                            f"  Did '{control}' physically halt motion? [y/N]: "
                        ),
                        "motion_disarmed": ask_yes(
                            f"  Did '{control}' leave the controller disarmed? [y/N]: "
                        ),
                        "observer": observer,
                    }
                else:
                    controls[control] = {
                        "verified": ask_yes(
                            f"  Did '{control}' physically halt motion? [y/N]: "
                        ),
                        "motion_stopped": ask_yes(
                            f"  Did '{control}' bring both tracks to a complete stop (armed-idle)? [y/N]: "
                        ),
                        "motion_disarmed": False,
                        "observer": observer,
                    }

                if control == "release_control":
                    controls[control]["controller_inactive"] = ask_yes(
                        "  Did Release control confirm the controller stopped? [y/N]: "
                    )

            print("\n--> Verify Installed PWA / Mobile Controls:")
            pwa: dict[str, Any] = {}
            for interaction in REQUIRED_PWA_CONTROLS:
                if partial_pwa.get(interaction):
                    pwa[interaction] = partial_pwa[interaction]
                    continue
                pwa[interaction] = {
                    "verified": ask_yes(
                        f"  Was PWA interaction '{interaction}' executed? [y/N]: "
                    ),
                    "no_latched_motion": ask_yes(
                        f"  Did '{interaction}' avoid latched motion? [y/N]: "
                    ),
                }

            print("\n--> Enter Instrumented Failure Stop Measurements:")
            latencies: dict[str, Any] = copy.deepcopy(partial_latencies)
            for condition in REQUIRED_FAILURE_CONDITIONS:
                if condition in latencies and latencies[condition].get("measured_ms"):
                    continue

                if condition == "host_shutdown" and not self.resume_campaign:
                    save_ckpt = ask_yes(
                        "\n  [Checkpoint Prompt] Save campaign checkpoint now before executing Pi shutdown/reboot? [y/N]: "
                    )
                    if save_ckpt:
                        partial_obs = {
                            "metadata": {
                                "physical_observer": observer,
                                "instruments": instruments,
                                "raw_evidence": raw_evidence,
                                "network_conditions": network_conditions,
                                "client_versions": {
                                    "desktop_browser": desktop_version,
                                    "android_pwa": android_version,
                                    "ios_pwa": ios_version,
                                },
                                "power_disconnect_accessible": disconnect_accessible,
                            },
                            "observed_movements": movements,
                            "safety_controls": controls,
                            "pwa_controls": pwa,
                            "latencies": latencies,
                        }
                        sess_id = (
                            getattr(self, "_active_session_id", None)
                            or uuid.uuid4().hex
                        )
                        self.save_campaign_checkpoint(partial_obs, sess_id)
                        print(
                            f"\n--> Campaign checkpoint successfully saved to: {self.campaign_file}"
                        )
                        print(
                            "--> You may now execute the host shutdown test (e.g. 'sudo systemctl poweroff')."
                        )
                        print(
                            "--> After the Pi reboots, resume acceptance testing with:"
                        )
                        print(
                            f"    ./ubuntu_tank/deploy.sh web-acceptance --ack-tracks-raised --interactive-observations --resume-campaign {self.campaign_file}\n"
                        )
                        return {
                            "__checkpoint_saved__": True,
                            "campaign_file": self.campaign_file,
                        }

                print(f"\n  Failure condition: {condition}")
                measured = ask_duration(
                    "    Injected-fault to physical-rest total (ms): "
                )
                event_to_agent = ask_duration("    Event-to-agent phase (ms): ")
                zero_write = ask_duration("    Agent-to-zero-write phase (ms): ")
                physical_stop = ask_duration(
                    "    Zero-write-to-physical-rest phase (ms): "
                )
                latencies[condition] = {
                    "measured_ms": measured,
                    "breakdown": {
                        "event_to_agent_ms": event_to_agent,
                        "zero_write_ms": zero_write,
                        "physical_stop_ms": physical_stop,
                    },
                    "physical_stop_observed": ask_yes(
                        "    Was complete physical rest observed? [y/N]: "
                    ),
                    "raw_evidence": ask_text(
                        "    Raw evidence reference for this measurement: "
                    ),
                }
        except (EOFError, KeyboardInterrupt, ValueError) as exc:
            print(f"\nObservation input aborted: {exc}")
            return {}

        return {
            "metadata": {
                "physical_observer": observer,
                "instruments": instruments,
                "raw_evidence": raw_evidence,
                "network_conditions": network_conditions,
                "client_versions": {
                    "desktop_browser": desktop_version,
                    "android_pwa": android_version,
                    "ios_pwa": ios_version,
                },
                "power_disconnect_accessible": disconnect_accessible,
            },
            "observed_movements": movements,
            "safety_controls": controls,
            "pwa_controls": pwa,
            "latencies": latencies,
        }

    def save_campaign_checkpoint(
        self, partial_observations: dict[str, Any], session_id: str
    ) -> str:
        """Persist campaign checkpoint before host shutdown to allow resumption after reboot."""
        os.makedirs(os.path.dirname(os.path.abspath(self.campaign_file)), exist_ok=True)
        active_release = self.environment_metadata.get("active_release_id")
        web_probe = self.probe_web_runtime() if not self.mock else {}
        web_sha256 = (
            web_probe.get("web_config_sha256")
            if not self.mock
            else "mock-sha256-checkpoint"
        )

        campaign_data = {
            "campaign_id": uuid.uuid4().hex,
            "session_id": session_id,
            "active_release_id": active_release,
            "web_config_sha256": web_sha256,
            "boot_id": get_system_boot_id(),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "partial_observations": partial_observations,
        }

        tmp_path = f"{self.campaign_file}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(campaign_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.campaign_file)
        return self.campaign_file

    def load_and_validate_campaign(self, campaign_path: str) -> dict[str, Any]:
        """Load and validate persisted campaign checkpoint against active release and reboot state."""
        if not os.path.isfile(campaign_path):
            raise FileNotFoundError(
                f"Campaign checkpoint file not found: {campaign_path}"
            )

        try:
            with open(campaign_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Failed to read campaign checkpoint JSON '{campaign_path}': {exc}"
            ) from exc

        required_keys = {
            "campaign_id",
            "session_id",
            "active_release_id",
            "web_config_sha256",
            "boot_id",
            "partial_observations",
        }
        missing = required_keys - set(data.keys())
        if missing:
            raise ValueError(
                f"Campaign checkpoint is missing required fields: {sorted(missing)}"
            )

        # 1. Release binding: active release must match checkpoint
        active_release = self.environment_metadata.get("active_release_id")
        ckpt_release = data.get("active_release_id")
        if active_release and ckpt_release and active_release != ckpt_release:
            raise ValueError(
                f"Campaign active_release_id '{ckpt_release}' does not match current system release '{active_release}'"
            )

        # 2. Configuration & reboot verification in live mode
        if not self.mock:
            current_boot = get_system_boot_id()
            ckpt_boot = data.get("boot_id")
            if current_boot and ckpt_boot and current_boot == ckpt_boot:
                raise ValueError(
                    "System boot ID has not changed; host has not rebooted since campaign checkpoint."
                )

            current_web = self.probe_web_runtime()
            ckpt_hash = data.get("web_config_sha256")
            if (
                ckpt_hash
                and current_web.get("web_config_sha256")
                and current_web.get("web_config_sha256") != ckpt_hash
            ):
                raise ValueError(
                    "Web configuration hash changed since campaign checkpoint."
                )

            if not self._status_is_safely_stopped(current_web):
                raise ValueError(
                    "System post-reboot state is not safely stopped and disarmed."
                )

        return data

    def request_safe_stop(self) -> tuple[bool, str]:
        """Request an ownership-independent fail-closed stop through the operator agent."""
        client = OperatorIpcClient()
        try:
            client.connect(timeout_sec=1.0)
            success, message = client.stop(timeout_sec=2.0)
            return success, message or ""
        except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
            return False, str(exc)
        finally:
            client.close()

    def collect_live_session(self) -> dict[str, Any] | None:
        """Collect and bind interactive evidence to this release and live API run."""
        campaign_id: str | None = None
        partial_existing: dict[str, Any] | None = None

        if self.resume_campaign:
            print(
                f"--> Resuming acceptance campaign from checkpoint: {self.resume_campaign}"
            )
            try:
                ckpt = self.load_and_validate_campaign(self.resume_campaign)
            except (ValueError, OSError) as exc:
                print(f"ERROR: Failed to resume campaign: {exc}", file=sys.stderr)
                self.preflight_errors.append(f"Campaign resumption error: {exc}")
                return None
            session_id = ckpt["session_id"]
            campaign_id = ckpt["campaign_id"]
            partial_existing = ckpt.get("partial_observations")
            started_wall = (
                ckpt.get("created_at")
                or datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
        else:
            session_id = uuid.uuid4().hex
            started_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()

        self._active_session_id = session_id
        started_mono = time.monotonic()
        before = self.probe_web_runtime()

        observations = self.prompt_physical_observations(
            partial_existing=partial_existing
        )
        if not observations:
            stop_ok, stop_message = self.request_safe_stop()
            self.live_run_evidence = {
                "session_id": session_id,
                "campaign_id": campaign_id,
                "active_release_id": self.environment_metadata.get("active_release_id"),
                "started_at": started_wall,
                "status": "ABORTED",
                "cleanup_stop_confirmed": stop_ok,
                "cleanup_message": stop_message,
            }
            return None

        if observations.get("__checkpoint_saved__"):
            self.overall_status = "CAMPAIGN_CHECKPOINT_SAVED"
            self.live_run_evidence = {
                "session_id": session_id,
                "campaign_id": campaign_id,
                "active_release_id": self.environment_metadata.get("active_release_id"),
                "started_at": started_wall,
                "status": "CHECKPOINT_SAVED",
                "campaign_file": observations.get("campaign_file"),
            }
            return None

        stop_ok, stop_message = self.request_safe_stop()
        after = self.probe_web_runtime()
        completed_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        active_release = self.environment_metadata.get("active_release_id")
        same_release = (
            before.get("release_id") == active_release
            and after.get("release_id") == active_release
        )
        same_configuration = before.get("web_config_sha256") == after.get(
            "web_config_sha256"
        )
        safely_stopped = self._status_is_safely_stopped(after)
        self._live_session_verified = bool(
            stop_ok and same_release and same_configuration and safely_stopped
        )
        observations.setdefault("metadata", {}).update(
            {
                "session_id": session_id,
                "campaign_id": campaign_id,
                "active_release_id": active_release,
                "web_config_sha256": before.get("web_config_sha256"),
                "started_at": started_wall,
                "completed_at": completed_wall,
            }
        )
        self.live_run_evidence = {
            "session_id": session_id,
            "campaign_id": campaign_id,
            "active_release_id": active_release,
            "started_at": started_wall,
            "completed_at": completed_wall,
            "duration_seconds": round(time.monotonic() - started_mono, 3),
            "web_probe_before": before,
            "web_probe_after": after,
            "same_release": same_release,
            "same_configuration": same_configuration,
            "cleanup_stop_confirmed": stop_ok,
            "cleanup_message": stop_message,
            "safely_stopped": safely_stopped,
            "status": "VERIFIED" if self._live_session_verified else "FAILED",
        }
        return observations

    def generate_json_report(self) -> dict[str, Any]:
        """Generate structured JSON report payload for Milestone 15."""
        duration = time.time() - self.start_time
        metadata = (self.physical_observations or {}).get("metadata", {})
        return {
            "report_version": "1.0",
            "milestone": "15",
            "title": "Raised-Track Web Movement and Failure Acceptance",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "duration_seconds": round(duration, 3),
            "status": self.overall_status,
            "safety_acknowledgment": {
                "ack_tracks_raised": self.ack_tracks_raised,
                "chassis_elevated": self.ack_tracks_raised,
                "power_disconnect_accessible": is_exact_bool_true(
                    metadata.get("power_disconnect_accessible")
                ),
                "on_ground_motion_authorized": False,
            },
            "environment": {
                "mock_mode": self.mock,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "metadata": self.environment_metadata,
            },
            "preflight_errors": self.preflight_errors,
            "observation_errors": self.observation_errors,
            "accepted_bounds_ms": ACCEPTED_WEB_LATENCY_BOUNDS_MS,
            "physical_observations": self.physical_observations,
            "live_run_evidence": self.live_run_evidence,
        }

    def generate_markdown_report(self) -> str:
        """Generate comprehensive Markdown acceptance report."""
        rep = self.generate_json_report()
        status = rep["status"]
        ts = rep["timestamp"]
        ack = rep["safety_acknowledgment"]
        meta = rep["environment"]["metadata"]

        lines = [
            "# Milestone 15: Raised-Track Web Movement & Failure Acceptance Report",
            "",
            f"**Acceptance Status**: `{status}`",
            f"**Timestamp**: {ts}",
            f"**Host Platform**: `{rep['environment']['platform']}` ({rep['environment']['machine']})",
            f"**Mock Mode**: `{rep['environment']['mock_mode']}`",
            "",
            "## 1. Safety Interlocks & Preflight",
            "",
            f"- **Tracks-Raised Acknowledgment**: `{'CONFIRMED' if ack['ack_tracks_raised'] else 'MISSING'}`",
            "- **Chassis Elevated**: Mechanics confirmed tracks elevated clear of surface",
            "- **On-Ground Motion**: STRICTLY FORBIDDEN (Controller & web mode only)",
            f"- **Active Release ID**: `{meta.get('active_release_id', 'N/A')}`",
            "",
        ]

        if self.preflight_errors:
            lines.append("### Preflight Issues")
            for err in self.preflight_errors:
                lines.append(f"- ❌ {err}")
            lines.append("")

        lines.extend(
            [
                "## 2. Web Motion Acceptance (All 4 Directions)",
                "",
                "| Direction | Observed | Direction Matched | Stopped After Burst | Observer |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        obs_mov = (self.physical_observations or {}).get("observed_movements", {})
        for m in REQUIRED_MOTIONS:
            entry = obs_mov.get(m, {})
            o = "✅" if entry.get("observed") else "❌"
            d = "✅" if entry.get("direction_matched") else "❌"
            s = "✅" if entry.get("stopped_after_burst") else "❌"
            who = entry.get("observer", "pending")
            lines.append(f"| `{m}` | {o} | {d} | {s} | {who} |")

        lines.extend(
            [
                "",
                "## 3. Web Safety Controls",
                "",
                "| Safety Control | Verified Halting | Final State | Observer |",
                "| --- | --- | --- | --- |",
            ]
        )
        obs_ctrl = (self.physical_observations or {}).get("safety_controls", {})
        for c in REQUIRED_SAFETY_CONTROLS:
            entry = obs_ctrl.get(c, {})
            v = "✅" if entry.get("verified") else "❌"
            if c in CONTROLS_REQUIRING_DISARM:
                d = "✅ Disarmed" if entry.get("motion_disarmed") else "❌ Not Disarmed"
            else:
                d = (
                    "✅ Armed-Idle"
                    if (entry.get("motion_stopped", True) and entry.get("verified"))
                    else "❌ Moving"
                )
            who = entry.get("observer", "pending")
            lines.append(f"| `{c}` | {v} | {d} | {who} |")

        lines.extend(
            [
                "",
                "## 4. Mobile PWA Interaction Acceptance",
                "",
                "| Interaction | Verified | No Latched Motion |",
                "| --- | --- | --- |",
            ]
        )
        obs_pwa = (self.physical_observations or {}).get("pwa_controls", {})
        for p in REQUIRED_PWA_CONTROLS:
            entry = obs_pwa.get(p, {})
            v = "✅" if entry.get("verified") else "❌"
            n = "✅" if entry.get("no_latched_motion") else "❌"
            lines.append(f"| `{p}` | {v} | {n} |")

        lines.extend(
            [
                "",
                "## 5. Instrumented Failure Stop Latencies (Target: <= 300 ms)",
                "",
                "| Failure Mode | Measured (ms) | Bound (ms) | Event->Agent | Zero Write | Physical Stop | Status |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        obs_lat = (self.physical_observations or {}).get("latencies", {})
        for cond in REQUIRED_FAILURE_CONDITIONS:
            entry = obs_lat.get(cond)
            if entry is None and cond == "serial_disconnect":
                entry = obs_lat.get("serial_loss")
            bound = ACCEPTED_WEB_LATENCY_BOUNDS_MS[cond]
            if entry:
                ms = entry.get("measured_ms", "N/A")
                bk = entry.get("breakdown", {})
                e2a = bk.get("event_to_agent_ms", "-")
                zw = bk.get("zero_write_ms", "-")
                ps = bk.get("physical_stop_ms", "-")
                pass_stat = (
                    "✅ PASS"
                    if (isinstance(ms, (int, float)) and ms <= bound)
                    else "❌ FAIL"
                )
                lines.append(
                    f"| `{cond}` | {ms} | {bound} | {e2a} | {zw} | {ps} | {pass_stat} |"
                )
            else:
                lines.append(
                    f"| `{cond}` | PENDING | {bound} | - | - | - | ⏳ PENDING |"
                )

        lines.extend(
            [
                "",
                "## 6. Live Run Correlation",
                "",
                f"- **Live Session**: `{rep['live_run_evidence'].get('session_id', 'not recorded')}`",
                f"- **Evidence Status**: `{rep['live_run_evidence'].get('status', 'NOT VERIFIED')}`",
                f"- **Release Stable During Run**: `{rep['live_run_evidence'].get('same_release', False)}`",
                f"- **Configuration Stable During Run**: `{rep['live_run_evidence'].get('same_configuration', False)}`",
                f"- **Cleanup Stop Confirmed**: `{rep['live_run_evidence'].get('cleanup_stop_confirmed', False)}`",
                f"- **Final Live State Safely Stopped**: `{rep['live_run_evidence'].get('safely_stopped', False)}`",
                "",
                "## 7. Physical Acceptance Conclusion",
                "",
            ]
        )
        if status == "ACCEPTED":
            lines.append(
                "✅ **ACCEPTED**: All physical motion observations, web safety controls, "
                "PWA interactions, and instrumented failure stop latencies have been confirmed "
                "on the elevated Raspberry Pi 5 chassis and satisfy all Section 7 bounds."
            )
        elif status == "MOCK_VERIFICATION_ONLY":
            lines.append(
                "ℹ️ **MOCK_VERIFICATION_ONLY**: Software orchestration and schema validation completed "
                "under simulation. In accordance with Section 7 and AGENTS.md, mocks NEVER certify "
                "physical acceptance. Target Pi hardware execution remains pending."
            )
        elif status == "PENDING_TARGET_EXECUTION":
            lines.append(
                "⏳ **PENDING_TARGET_EXECUTION**: Executed on development computer (non-target platform). "
                "Physical raised-track web movement acceptance is pending execution on the physical Pi 5."
            )
        elif status == "PENDING_PHYSICAL_ACCEPTANCE":
            lines.append(
                "⏳ **PENDING_PHYSICAL_ACCEPTANCE**: Supplied observations or incomplete prompts "
                "cannot certify the active release. Acceptance requires a complete interactive target "
                "session correlated with the installed web API, current release and configuration, "
                "followed by a confirmed fail-closed stop."
            )
        else:
            lines.append(
                f"❌ **{status}**: Physical acceptance validation did not pass. Refer to error listings above."
            )

        lines.append("")
        return "\n".join(lines)

    def run_acceptance_suite(self) -> bool:
        """Run acceptance while retaining deployment coordination and cleanup."""
        print("=" * 60)
        print("MentorPi Milestone 15: Raised-Track Web Movement & Failure Acceptance")
        print("=" * 60)

        is_target, _ = self.check_target_platform()
        if is_target and not self.mock:
            try:
                with self.deployment_read_lock():
                    return self._run_acceptance_suite_locked(is_target=True)
            except Exception as exc:  # noqa: BLE001 - fail-closed suite boundary
                stop_ok, stop_message = self.request_safe_stop()
                self.preflight_errors.append(f"Live acceptance failed: {exc}")
                if not stop_ok:
                    self.preflight_errors.append(
                        f"Fail-closed cleanup stop could not be confirmed: {stop_message}"
                    )
                self.overall_status = "FAILED"
                self._write_reports()
                return False
        return self._run_acceptance_suite_locked(is_target=is_target)

    def _run_acceptance_suite_locked(self, *, is_target: bool) -> bool:
        """Run the acceptance sequence under its full-run coordination lock."""

        # 1. Preflight
        pre_ok, pre_errs = self.run_preflight_checks()
        if not pre_ok:
            print("\n❌ Preflight failed with errors:")
            for e in pre_errs:
                print(f"   - {e}")
            self.overall_status = "FAILED"
            self._write_reports()
            return False

        if not is_target:
            if self.mock:
                print("\n[Notice] Running in mock/simulation verification mode.")
            else:
                print(
                    "\n[Notice] Non-target platform detected. Safe non-target reporting active."
                )

        # 2. Collect or load physical observations
        if self.interactive_observations and is_target and not self.mock:
            interactive_data = self.collect_live_session()
            if self.overall_status == "CAMPAIGN_CHECKPOINT_SAVED":
                print(
                    f"\nAcceptance paused: Campaign checkpoint saved at {self.campaign_file}."
                )
                return True
            if interactive_data:
                self.physical_observations = interactive_data

        # 3. Validate observations if provided
        obs_valid = False
        if self.physical_observations:
            obs_valid, obs_errs = self.validate_physical_observations(
                self.physical_observations
            )
            if not obs_valid:
                print("\n❌ Physical observations validation failed:")
                for e in obs_errs:
                    print(f"   - {e}")
            else:
                print(
                    "\n✅ Physical observations and measured latencies verified against Section 7 schema."
                )

        # 4. Determine final status
        # Safety rule: mock mode or non-target platform can NEVER certify physical acceptance
        if self.mock:
            self.overall_status = "MOCK_VERIFICATION_ONLY" if obs_valid else "FAILED"
        elif not is_target:
            self.overall_status = "PENDING_TARGET_EXECUTION"
        elif not self.ack_tracks_raised:
            self.overall_status = "FAILED"
        elif obs_valid and self._live_session_verified:
            self.overall_status = "ACCEPTED"
        else:
            self.overall_status = "PENDING_PHYSICAL_ACCEPTANCE"

        print(f"\nFinal Acceptance Status: {self.overall_status}")
        self._write_reports()
        return self.overall_status in (
            "ACCEPTED",
            "MOCK_VERIFICATION_ONLY",
            "PENDING_TARGET_EXECUTION",
        )

    def _write_reports(self) -> None:
        """Write JSON and Markdown acceptance reports to report directory."""
        os.makedirs(self.report_dir, exist_ok=True)
        json_path = os.path.join(self.report_dir, f"{self.report_name}.json")
        md_path = os.path.join(self.report_dir, f"{self.report_name}.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.generate_json_report(), f, indent=2)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self.generate_markdown_report())

        print("Acceptance reports written to:")
        print(f"  - JSON: {json_path}")
        print(f"  - Markdown: {md_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MentorPi Milestone 15 Raised-Track Web Movement & Failure Acceptance Orchestrator."
    )
    parser.add_argument(
        "--ack-tracks-raised",
        action="store_true",
        help="Acknowledge chassis is mechanically elevated and tracks are completely clear of surface.",
    )
    parser.add_argument(
        "--require-target",
        action="store_true",
        help="Fail with non-zero exit code if not running on authentic Raspberry Pi 5 ARM64 hardware.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in software simulation verification mode (never certifies physical acceptance).",
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default=os.path.join(WORKSPACE_ROOT, "dist"),
        help="Directory to store acceptance reports (default: dist/).",
    )
    parser.add_argument(
        "--report-name",
        type=str,
        default="web-acceptance-report-milestone15",
        help="Base name for acceptance reports (default: web-acceptance-report-milestone15).",
    )
    parser.add_argument(
        "--physical-observations",
        type=str,
        help=(
            "Path or raw JSON containing observations for schema/mock verification; "
            "this input alone can never certify a target release."
        ),
    )
    parser.add_argument(
        "--interactive-observations",
        action="store_true",
        help=(
            "Run the only certifying target workflow: collect current physical/instrument evidence "
            "and correlate it with the live installed web API."
        ),
    )
    parser.add_argument(
        "--expected-release-id",
        type=str,
        help="Verify against an expected release identifier.",
    )
    parser.add_argument(
        "--resume-campaign",
        type=str,
        help=(
            "Resume an interactive acceptance campaign from a checkpoint saved "
            "before Pi shutdown/reboot."
        ),
    )
    parser.add_argument(
        "--campaign-file",
        type=str,
        help="Path where campaign checkpoint should be saved/loaded.",
    )

    args = parser.parse_args()

    # Safety Notice
    print("=" * 60)
    print("Safety Notice:")
    print("Under NO circumstances does this tool authorize on-ground motion.")
    print("All tests require explicit confirmation that tracks are elevated.")
    print("=" * 60)

    obs_data = None
    if args.physical_observations:
        raw = args.physical_observations.strip()
        if os.path.isfile(raw):
            try:
                with open(raw, "r", encoding="utf-8") as f:
                    obs_data = json.load(f)
            except (OSError, UnicodeError, json.JSONDecodeError) as e:
                print(
                    f"ERROR: Failed to load physical observations file '{raw}': {e}",
                    file=sys.stderr,
                )
                return 1
        else:
            try:
                obs_data = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as e:
                print(
                    f"ERROR: Failed to parse physical observations JSON string: {e}",
                    file=sys.stderr,
                )
                return 1

    orch = WebAcceptanceOrchestrator(
        ack_tracks_raised=args.ack_tracks_raised,
        report_dir=args.report_dir,
        report_name=args.report_name,
        mock=args.mock,
        require_target=args.require_target,
        physical_observations=obs_data,
        interactive_observations=args.interactive_observations,
        expected_release_id=args.expected_release_id,
        resume_campaign=args.resume_campaign,
        campaign_file=args.campaign_file,
    )

    success = orch.run_acceptance_suite()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
