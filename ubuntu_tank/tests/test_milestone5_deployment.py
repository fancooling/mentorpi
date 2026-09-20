"""
test_milestone5_deployment.py - Unit and integration tests for Milestone 5:
Systemd unit confinement, configuration schema validation and migration,
udev rule generation and hardware identity binding, deployment lock contention,
supervisor credential freshness, startup coordination, and teleop settings.
"""

# ruff: noqa: E402 - ROS test doubles must be installed before package imports.

import copy
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# Lightweight mock ROS 2 and Launch infrastructure for hardware-free unit tests
class MockPackage(MagicMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__path__ = []


class MockParameter:
    def __init__(self, value):
        self.value = value


class MockNode:
    def __init__(self, name="mock_node", **kwargs):
        self.name = name
        self._params = {}
        self.subscriptions = []
        self.publishers = []
        self.services = []
        self.timers = []
        self._logger = MagicMock()

    def declare_parameter(self, name, default_value):
        self._params[name] = default_value
        return MockParameter(default_value)

    def get_parameter(self, name):
        return MockParameter(self._params.get(name))

    def create_publisher(self, msg_type, topic, qos):
        pub = MagicMock(topic=topic, msg_type=msg_type)
        self.publishers.append(pub)
        return pub

    def create_subscription(self, msg_type, topic, callback, qos):
        sub = MagicMock(topic=topic, msg_type=msg_type, callback=callback)
        self.subscriptions.append(sub)
        return sub

    def create_service(self, srv_type, srv_name, callback):
        srv = MagicMock(srv_name=srv_name, srv_type=srv_type, callback=callback)
        self.services.append(srv)
        return srv

    def create_timer(self, period, callback):
        t = MagicMock(period=period, callback=callback)
        self.timers.append(t)
        return t

    def get_clock(self):
        clk = MagicMock()
        clk.now.return_value.to_msg.return_value = MagicMock()
        return clk

    def get_logger(self):
        return self._logger

    def destroy_node(self):
        pass


for _mod_name in [
    "std_srvs",
    "std_srvs.srv",
    "sensor_msgs",
    "sensor_msgs.msg",
    "std_msgs",
    "std_msgs.msg",
    "nav_msgs",
    "nav_msgs.msg",
    "geometry_msgs",
    "geometry_msgs.msg",
    "ros_robot_controller_msgs",
    "ros_robot_controller_msgs.srv",
    "ros_robot_controller_msgs.msg",
    "rclpy",
    "rclpy.node",
    "rclpy.qos",
    "launch",
    "launch.actions",
    "launch.events",
    "launch.event_handlers",
    "launch.substitutions",
    "launch_ros",
    "launch_ros.actions",
]:
    sys.modules.setdefault(_mod_name, MockPackage())

sys.modules["rclpy.node"].Node = MockNode

from ubuntu_tank.scripts.config_migration import (
    DEFAULTS_V1_0,
    downgrade_config,
    migrate_config,
    validate_config,
)
from ubuntu_tank.scripts.deployment_manager import (
    DeploymentLock,
    ReleaseManager,
    check_hardware_mutual_exclusion,
    compute_file_sha256,
    parse_release_manifest,
)


class BaseDeploymentTestCase(unittest.TestCase):
    """Sets up an isolated filesystem hierarchy mimicking /opt, /etc, /var, /run."""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="ubuntu_tank_test_m5_")
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

        self.repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        self.workspace_dir = os.path.join(self.repo_root, "ubuntu_tank")

        # A previously verified identity is part of this isolated host fixture.
        with open(os.path.join(self.udev_dir, "99-mentorpi-rrc.rules"), "w") as stream:
            stream.write(
                'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", ATTRS{serial}=="fixture-rrc", GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc"\n'
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


class TestConfigurationSchemaMigrationAndDowngrade(unittest.TestCase):
    """Test schema verification, forward migration, and downgrade preservation."""

    def test_validate_default_config(self):
        """Default controller.yaml passes validation."""
        ok, errs = validate_config(DEFAULTS_V1_0)
        self.assertTrue(ok, f"Default config failed validation: {errs}")

    def test_validate_rejects_unsafe_parameters(self):
        """Validation rejects non-positive speeds, invalid serial paths, and lease >= timeout."""
        bad_cfg = copy.deepcopy(DEFAULTS_V1_0)
        bad_cfg["teleop"]["lease_duration_sec"] = 0.300
        bad_cfg["motor_guard"]["timeout_sec"] = 0.250
        ok, errs = validate_config(bad_cfg)
        self.assertFalse(ok)
        self.assertTrue(any("lease_duration_sec" in e for e in errs))

        bad_cfg2 = copy.deepcopy(DEFAULTS_V1_0)
        bad_cfg2["serial_bridge"]["serial_device"] = "COM1"
        ok2, errs2 = validate_config(bad_cfg2)
        self.assertFalse(ok2)
        self.assertTrue(any("serial_device" in e for e in errs2))

    def test_forward_migration_preserves_calibrations(self):
        """Migrating to v1.1 preserves user calibrations and adds new schema defaults."""
        custom_cfg = copy.deepcopy(DEFAULTS_V1_0)
        custom_cfg["controller"]["wheelbase"] = 0.1420
        custom_cfg["controller"]["correction_factor"]["left"] = 1.05

        migrated = migrate_config(custom_cfg, target_version="1.1")
        self.assertEqual(migrated["controller"]["wheelbase"], 0.1420)
        self.assertEqual(migrated["controller"]["correction_factor"]["left"], 1.05)
        self.assertEqual(migrated["supervisor"]["watchdog_ping_sec"], 0.500)
        self.assertEqual(migrated["serial_bridge"]["telemetry_rate_hz"], 50.0)

    def test_downgrade_preserves_calibrations(self):
        """Downgrading from v1.1 to v1.0 strips v1.1 additions while preserving user calibrations."""
        v1_1_cfg = copy.deepcopy(DEFAULTS_V1_0)
        v1_1_cfg["supervisor"]["watchdog_ping_sec"] = 0.500
        v1_1_cfg["serial_bridge"]["telemetry_rate_hz"] = 50.0
        v1_1_cfg["controller"]["wheelbase"] = 0.1450

        downgraded = downgrade_config(v1_1_cfg, target_version="1.0")
        self.assertEqual(downgraded["controller"]["wheelbase"], 0.1450)
        self.assertNotIn("watchdog_ping_sec", downgraded["supervisor"])
        self.assertNotIn("telemetry_rate_hz", downgraded["serial_bridge"])


class TestSystemdUnitAndConfinementDirectives(BaseDeploymentTestCase):
    """Test systemd unit definitions and confinement directives against design requirements."""

    def setUp(self):
        super().setUp()
        self.service_path = os.path.join(
            self.repo_root, "ubuntu_tank", "host", "mentorpi-tank.service"
        )
        self.recover_service_path = os.path.join(
            self.repo_root, "ubuntu_tank", "host", "mentorpi-tank-recover.service"
        )
        self.rules_path = os.path.join(
            self.repo_root, "ubuntu_tank", "host", "99-mentorpi-rrc.rules"
        )
        self.tmpfiles_path = os.path.join(
            self.repo_root, "ubuntu_tank", "host", "ubuntu-tank.conf"
        )

    def _effective_systemd_service(self, systemd_analyze, root, unit_name):
        """Return systemd's effective properties and commands for one staged service."""
        env = os.environ.copy()
        env["SYSTEMD_LOG_LEVEL"] = "debug"
        result = subprocess.run(
            [systemd_analyze, f"--root={root}", "verify", unit_name],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        output = result.stdout + result.stderr
        marker = (
            f"-> Unit {unit_name}:"
            if f"-> Unit {unit_name}:" in output
            else f"→ Unit {unit_name}:"
        )
        self.assertIn(marker, output)
        unit_dump = output.split(marker, 1)[1]
        next_unit = unit_dump.find("\n\t-> Unit ")
        if next_unit < 0:
            next_unit = unit_dump.find("\n\t→ Unit ")
        if next_unit >= 0:
            unit_dump = unit_dump[:next_unit]

        properties = {}
        commands = []
        for raw_line in unit_dump.splitlines():
            line = raw_line.strip()
            if line.startswith("Command Line: "):
                commands.append(line.removeprefix("Command Line: "))
            elif ": " in line:
                name, value = line.split(": ", 1)
                properties[name] = value
        return properties, commands

    @staticmethod
    def _write_systemd_override(staged, unit_name, contents):
        """Install a generated drop-in used to mutation-test effective unit behavior."""
        drop_in_dir = staged / f"{unit_name}.d"
        drop_in_dir.mkdir(parents=True, exist_ok=True)
        override = drop_in_dir / "test-override.conf"
        override.write_text(contents, encoding="utf-8")
        return override

    @staticmethod
    def _has_controller_supervision_contract(properties):
        """Report whether effective systemd properties retain fail-closed supervision."""
        return (
            properties.get("Type") == "notify"
            and properties.get("NotifyAccess") == "main"
            and properties.get("WatchdogSec") not in (None, "0", "infinity")
            and properties.get("Restart") == "on-failure"
        )

    def test_mentorpi_tank_service_confinement_directives(self):
        """mentorpi-tank.service is accepted by systemd-analyze and enforces valid unit structure."""
        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        host_dir = Path(self.repo_root, "ubuntu_tank", "host")
        unit_names = ("mentorpi-tank.service", "mentorpi-tank-recover.service")
        base_targets = (
            "network.target",
            "multi-user.target",
            "sysinit.target",
            "basic.target",
        )
        system_unit_dir = Path("/usr/lib/systemd/system")

        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            staged = root / "etc/systemd/system"
            staged.mkdir(parents=True)
            for u in unit_names:
                shutil.copy2(host_dir / u, staged / u)
            for t in base_targets:
                if (system_unit_dir / t).is_file():
                    shutil.copy2(system_unit_dir / t, staged / t)

            (root / "opt/ubuntu_tank/current/bin").mkdir(parents=True)
            (root / "opt/ubuntu_tank/libexec").mkdir(parents=True)
            shutil.copy2(
                "/bin/true", root / "opt/ubuntu_tank/current/bin/mentorpi-tank-run"
            )
            shutil.copy2(
                "/bin/true", root / "opt/ubuntu_tank/libexec/recover-activation"
            )

            # Positive behavioral verification: units form a valid configuration
            res = subprocess.run(
                [systemd_analyze, f"--root={root}", "verify", "mentorpi-tank.service"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            properties, _ = self._effective_systemd_service(
                systemd_analyze, root, "mentorpi-tank.service"
            )
            self.assertTrue(
                self._has_controller_supervision_contract(properties),
                f"Effective controller supervision is unsafe: {properties}",
            )

            supervision_mutations = {
                "watchdog disabled": "[Service]\nWatchdogSec=0\n",
                "notify protocol disabled": "[Service]\nType=simple\n",
                "main-process notification ownership disabled": (
                    "[Service]\nNotifyAccess=all\n"
                ),
                "failure restart disabled": "[Service]\nRestart=no\n",
            }
            for description, override_contents in supervision_mutations.items():
                with self.subTest(supervision_mutation=description):
                    override = self._write_systemd_override(
                        staged, "mentorpi-tank.service", override_contents
                    )
                    mutated, _ = self._effective_systemd_service(
                        systemd_analyze, root, "mentorpi-tank.service"
                    )
                    self.assertFalse(
                        self._has_controller_supervision_contract(mutated),
                        f"Unsafe mutation was accepted: {description}",
                    )
                    override.unlink()

            # Negative behavioral verification: dependency on recover service is enforced
            os.remove(staged / "mentorpi-tank-recover.service")
            res_missing = subprocess.run(
                [systemd_analyze, f"--root={root}", "verify", "mentorpi-tank.service"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res_missing.returncode, 0)
            self.assertIn("mentorpi-tank-recover.service", res_missing.stderr)

            # Negative behavioral verification: malformed confinement setting is rejected
            shutil.copy2(
                host_dir / "mentorpi-tank-recover.service",
                staged / "mentorpi-tank-recover.service",
            )
            with open(host_dir / "mentorpi-tank.service", "r", encoding="utf-8") as f:
                bad_content = f.read().replace(
                    "ProtectSystem=strict", "ProtectSystem=invalid_setting"
                )
            with open(staged / "mentorpi-tank.service", "w", encoding="utf-8") as f:
                f.write(bad_content)
            res_bad = subprocess.run(
                [systemd_analyze, f"--root={root}", "verify", "mentorpi-tank.service"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertIn("Failed to parse", res_bad.stderr + res_bad.stdout)

            # Behavioral property verification: systemd-analyze security evaluates effective device and user policy
            sec_out = subprocess.check_output(
                [
                    systemd_analyze,
                    "security",
                    "--offline=true",
                    str(host_dir / "mentorpi-tank.service"),
                    "--json=pretty",
                ],
                text=True,
            )
            sec_data = {item.get("json_field"): item for item in json.loads(sec_out)}
            # Device access policy: must have device ACL explicitly permitting /dev/rrc:rw
            self.assertIn(
                "/dev/rrc:rw",
                sec_data.get("DeviceAllow", {}).get("description", ""),
            )
            # Service identity: must enforce static non-root user identity
            self.assertIn(
                "static non-root user",
                sec_data.get("UserOrDynamicUser", {}).get("description", ""),
            )
            # Private devices must be disabled (False) to permit DeviceAllow access to /dev/rrc
            self.assertFalse(sec_data.get("PrivateDevices", {}).get("set", True))

            # Mutation regression: removing DeviceAllow=/dev/rrc rw fails device access check
            with open(host_dir / "mentorpi-tank.service", "r", encoding="utf-8") as f:
                service_lines = f.readlines()
            no_dev_content = "".join(
                line for line in service_lines if not line.startswith("DeviceAllow=")
            )
            with tempfile.NamedTemporaryFile("w", suffix=".service") as tf:
                tf.write(no_dev_content)
                tf.flush()
                mut_sec_out = subprocess.check_output(
                    [
                        systemd_analyze,
                        "security",
                        "--offline=true",
                        tf.name,
                        "--json=pretty",
                    ],
                    text=True,
                )
                mut_sec_data = {
                    item.get("json_field"): item for item in json.loads(mut_sec_out)
                }
                self.assertNotIn(
                    "/dev/rrc:rw",
                    mut_sec_data.get("DeviceAllow", {}).get("description", ""),
                )

            # Mutation regression: removing User= fails non-root identity check
            no_user_content = "".join(
                line
                for line in service_lines
                if not line.startswith("User=") and not line.startswith("Group=")
            )
            with tempfile.NamedTemporaryFile("w", suffix=".service") as tf:
                tf.write(no_user_content)
                tf.flush()
                user_sec_out = subprocess.check_output(
                    [
                        systemd_analyze,
                        "security",
                        "--offline=true",
                        tf.name,
                        "--json=pretty",
                    ],
                    text=True,
                )
                user_sec_data = {
                    item.get("json_field"): item for item in json.loads(user_sec_out)
                }
                self.assertIn(
                    "root user",
                    user_sec_data.get("UserOrDynamicUser", {}).get("description", ""),
                )

    def test_mentorpi_tank_recover_service_directives(self):
        """Execute systemd's effective recovery command and require transaction repair."""
        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        host_dir = Path(self.repo_root, "ubuntu_tank", "host")
        base_targets = (
            "network.target",
            "multi-user.target",
            "sysinit.target",
            "basic.target",
        )
        system_unit_dir = Path("/usr/lib/systemd/system")

        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            staged = root / "etc/systemd/system"
            staged.mkdir(parents=True)
            shutil.copy2(
                host_dir / "mentorpi-tank-recover.service",
                staged / "mentorpi-tank-recover.service",
            )
            for t in base_targets:
                if (system_unit_dir / t).is_file():
                    shutil.copy2(system_unit_dir / t, staged / t)

            (root / "opt/ubuntu_tank/libexec").mkdir(parents=True)
            shutil.copy2(
                "/bin/true", root / "opt/ubuntu_tank/libexec/recover-activation"
            )

            res = subprocess.run(
                [
                    systemd_analyze,
                    f"--root={root}",
                    "verify",
                    "mentorpi-tank-recover.service",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            properties, commands = self._effective_systemd_service(
                systemd_analyze, root, "mentorpi-tank-recover.service"
            )
            self.assertEqual(properties.get("Type"), "oneshot")
            self.assertEqual(properties.get("User"), "root")
            self.assertEqual(properties.get("Group"), "root")
            self.assertEqual(properties.get("RemainAfterExit"), "no")
            self.assertEqual(len(commands), 1, commands)

            archive = self.mgr.package_release(
                self.workspace_dir,
                os.path.join(self.test_root, "dist"),
                "1.0.0-grecover-unit",
                allow_staged_install=True,
            )
            self.mgr.install_release(archive, require_root=False, enforce_arm64=False)

            def local_command(command_line):
                argv = shlex.split(command_line)
                production_prefix = "/opt/ubuntu_tank"
                if argv[0].startswith(production_prefix + "/"):
                    relative = os.path.relpath(argv[0], production_prefix)
                    argv[0] = os.path.join(self.opt_dir, relative)
                return argv

            def seed_interrupted_activation(tx_id):
                snapshot_dir = self.mgr.snapshot_mgr.create_snapshot(
                    tx_id=tx_id,
                    current_symlink_target=None,
                    etc_dir=self.etc_dir,
                    systemd_dir=self.systemd_dir,
                    udev_dir=self.udev_dir,
                )
                self.mgr.journal.record_prepared(
                    tx_id=tx_id,
                    candidate_release_id="candidate",
                    candidate_release_path="/candidate",
                    previous_release_id=None,
                    previous_release_path=None,
                    snapshot_dir=snapshot_dir,
                )

            recovery_args = [
                "--opt-dir",
                self.opt_dir,
                "--etc-dir",
                self.etc_dir,
                "--var-dir",
                self.var_dir,
                "--systemd-dir",
                self.systemd_dir,
                "--udev-dir",
                self.udev_dir,
                "--lock-path",
                self.lock_path,
            ]
            recovery_env = os.environ.copy()
            recovery_env["PATH"] = os.path.dirname(sys.executable)

            seed_interrupted_activation("tx-effective-command")
            recover = subprocess.run(
                local_command(commands[0]) + recovery_args,
                capture_output=True,
                text=True,
                check=False,
                env=recovery_env,
            )
            self.assertEqual(recover.returncode, 0, recover.stderr)
            self.assertIsNone(self.mgr.journal.get_state()["current_transaction"])

            override = self._write_systemd_override(
                staged,
                "mentorpi-tank-recover.service",
                "[Service]\n"
                "ExecStart=\n"
                "ExecStart=/opt/ubuntu_tank/libexec/recover-activation --help\n",
            )
            _, no_op_commands = self._effective_systemd_service(
                systemd_analyze, root, "mentorpi-tank-recover.service"
            )
            self.assertEqual(len(no_op_commands), 1, no_op_commands)
            seed_interrupted_activation("tx-no-op-command")
            no_op = subprocess.run(
                local_command(no_op_commands[0]) + recovery_args,
                capture_output=True,
                text=True,
                check=False,
                env=recovery_env,
            )
            self.assertEqual(no_op.returncode, 0, no_op.stderr)
            self.assertIsNotNone(
                self.mgr.journal.get_state()["current_transaction"],
                "The --help mutation must not satisfy recovery postconditions",
            )
            override.unlink()

            # Negative behavioral verification: malformed directive is rejected
            with open(
                host_dir / "mentorpi-tank-recover.service", "r", encoding="utf-8"
            ) as f:
                bad_content = f.read().replace("Type=oneshot", "Type=invalid_type")
            with open(
                staged / "mentorpi-tank-recover.service", "w", encoding="utf-8"
            ) as f:
                f.write(bad_content)
            res_bad = subprocess.run(
                [
                    systemd_analyze,
                    f"--root={root}",
                    "verify",
                    "mentorpi-tank-recover.service",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertIn("Failed to parse", res_bad.stderr + res_bad.stdout)

    def test_udev_rule_is_accepted_by_official_parser(self):
        """udevadm accepts the shipped rule and rejects a malformed generated rule."""
        udevadm = shutil.which("udevadm")
        if not udevadm:
            self.skipTest("udevadm unavailable")

        res = subprocess.run(
            [udevadm, "verify", "--resolve-names=never", self.rules_path],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Success: 1", res.stdout)

        # Behavioral negative test: invalid udev rule syntax fails udevadm verify
        with tempfile.NamedTemporaryFile("w", suffix=".rules") as bad_rules:
            bad_rules.write("INVALID_UDEV_DIRECTIVE==foo\n")
            bad_rules.flush()
            bad_res = subprocess.run(
                [udevadm, "verify", "--resolve-names=never", bad_rules.name],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(bad_res.returncode, 0)
            self.assertIn("Fail:    1", bad_res.stdout)

    def test_target_udev_rule_creates_restricted_rrc_device(self):
        """Validate live udev effects on the matching tty device, or remain pending."""
        import grp

        udevadm = shutil.which("udevadm")
        if not udevadm:
            self.skipTest("udevadm unavailable")

        tty_syspath = None
        for tty_entry in Path("/sys/class/tty").glob("ttyACM*"):
            resolved = tty_entry.resolve()
            for parent in (resolved, *resolved.parents):
                vendor_path = parent / "idVendor"
                product_path = parent / "idProduct"
                try:
                    identity = (
                        vendor_path.read_text(encoding="utf-8").strip(),
                        product_path.read_text(encoding="utf-8").strip(),
                    )
                except (OSError, UnicodeDecodeError):
                    continue
                if identity == ("1a86", "55d4"):
                    tty_syspath = resolved
                    break
            if tty_syspath is not None:
                break

        if tty_syspath is None:
            self.skipTest(
                "target RRC ttyACM device unavailable; live udev behavior remains pending"
            )

        live_test = subprocess.run(
            [udevadm, "test", "--action=add", str(tty_syspath)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(live_test.returncode, 0, live_test.stderr)

        info = subprocess.run(
            [udevadm, "info", "--query=property", "--path", str(tty_syspath)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(info.returncode, 0, info.stderr)
        properties = dict(
            line.split("=", 1) for line in info.stdout.splitlines() if "=" in line
        )
        self.assertIn("/dev/rrc", shlex.split(properties.get("DEVLINKS", "")))
        self.assertEqual(properties.get("ID_MM_PORT_IGNORE"), "1")

        rrc_path = "/dev/rrc"
        self.assertTrue(os.path.islink(rrc_path), "/dev/rrc symlink was not created")
        self.assertEqual(
            os.path.realpath(rrc_path),
            f"/dev/{tty_syspath.name}",
            "/dev/rrc points to the wrong tty device",
        )
        device_stat = os.stat(rrc_path)
        self.assertEqual(stat.S_IMODE(device_stat.st_mode), 0o660)
        self.assertEqual(grp.getgrgid(device_stat.st_gid).gr_name, "mentorpi-rrc")

    def test_tmpfiles_conf_definitions(self):
        """ubuntu-tank.conf applies directory hierarchy and permissions via systemd-tmpfiles."""
        tmpfiles_cmd = shutil.which("systemd-tmpfiles")
        if not tmpfiles_cmd:
            self.skipTest("systemd-tmpfiles unavailable")

        with tempfile.TemporaryDirectory() as td:
            conf_dir = os.path.join(td, "etc", "tmpfiles.d")
            os.makedirs(conf_dir, exist_ok=True)
            with open(self.tmpfiles_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            numeric_lines = []
            for line in lines:
                if line.strip() and not line.startswith("#"):
                    parts = line.split()
                    # Use current numeric uid/gid for rootless tmpfiles execution
                    parts[3] = str(os.getuid())
                    parts[4] = str(os.getgid())
                    numeric_lines.append(" ".join(parts) + "\n")
                else:
                    numeric_lines.append(line)
            staged_conf = os.path.join(conf_dir, "ubuntu-tank.conf")
            with open(staged_conf, "w", encoding="utf-8") as f:
                f.writelines(numeric_lines)

            res = subprocess.run(
                [tmpfiles_cmd, "--create", f"--root={td}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            # Observable filesystem behavior
            run_path = os.path.join(td, "run/ubuntu_tank")
            lock_path = os.path.join(td, "run/lock/ubuntu_tank")
            ros_log_path = os.path.join(td, "var/opt/ubuntu_tank/ros-log")
            snap_path = os.path.join(td, "var/opt/ubuntu_tank/deployment/snapshots")

            self.assertTrue(os.path.isdir(run_path))
            self.assertEqual(stat.S_IMODE(os.stat(run_path).st_mode), 0o775)

            self.assertTrue(os.path.isdir(lock_path))
            self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o755)

            self.assertTrue(os.path.isdir(ros_log_path))
            self.assertEqual(stat.S_IMODE(os.stat(ros_log_path).st_mode), 0o750)

            self.assertTrue(os.path.isdir(snap_path))
            self.assertEqual(stat.S_IMODE(os.stat(snap_path).st_mode), 0o700)


class TestDeploymentLockContention(BaseDeploymentTestCase):
    """Test lock contention and non-blocking timeout handling."""

    def test_lock_contention_fails_closed(self):
        """A second deployment operation fails closed when the lock is held."""
        lock1 = DeploymentLock(self.lock_path, timeout_sec=0.2)
        lock2 = DeploymentLock(self.lock_path, timeout_sec=0.2)

        with lock1:
            with self.assertRaises(TimeoutError) as ctx:
                with lock2:
                    pass
            self.assertIn("held by another process", str(ctx.exception))


class TestNonInteractiveRunner(BaseDeploymentTestCase):
    """Test bin/mentorpi-tank-run CLI options."""

    def test_runner_help_and_verify(self):
        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")
        self.assertTrue(os.path.isfile(runner_path))
        self.assertTrue(os.access(runner_path, os.X_OK))

        # Help test
        res_help = subprocess.run(
            [runner_path, "--help"], capture_output=True, text=True
        )
        self.assertEqual(res_help.returncode, 0)
        self.assertIn("MentorPi Tank Native Controller Service Runner", res_help.stdout)

        # Dry-run test
        res_dry = subprocess.run(
            [runner_path, "--dry-run"], capture_output=True, text=True
        )
        self.assertEqual(res_dry.returncode, 0)
        self.assertIn("[mentorpi-tank-run:dry-run]", res_dry.stdout)


class TestReviewRemediations(BaseDeploymentTestCase):
    """Regression tests verifying remediations for all 6 P1 review findings."""

    def test_p1_finding3_host_safety_configuration_applied_to_launch_args(self):
        """
        Finding 3: Validate host controller.yaml and pass parameters into ROS launch arguments.
        Invalid configuration aborts startup.
        """
        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")
        custom_cfg = os.path.join(self.etc_dir, "custom_controller.yaml")
        cfg_data = copy.deepcopy(DEFAULTS_V1_0)
        cfg_data["controller"]["wheelbase"] = 0.1650
        cfg_data["controller"]["track_width"] = 0.1750
        cfg_data["controller"]["wheel_diameter"] = 0.0750
        cfg_data["controller"]["correction_factor"]["left"] = 1.08
        cfg_data["controller"]["correction_factor"]["right"] = 0.94
        cfg_data["teleop"]["lease_duration_sec"] = 0.100
        cfg_data["motor_guard"]["max_rps"] = 0.50
        cfg_data["motor_guard"]["timeout_sec"] = 0.250
        cfg_data["serial_bridge"]["serial_device"] = "/dev/custom_rrc"
        cfg_data["serial_bridge"]["baud_rate"] = 921600
        with open(custom_cfg, "w") as f:
            yaml.safe_dump(cfg_data, f)

        env = os.environ.copy()
        env["UBUNTU_TANK_CONFIG"] = custom_cfg

        res_dry = subprocess.run(
            [runner_path, "--dry-run"], env=env, capture_output=True, text=True
        )
        self.assertEqual(res_dry.returncode, 0, f"Failed dry-run: {res_dry.stderr}")
        out = res_dry.stdout
        self.assertIn("max_rps:=0.5", out)
        self.assertIn("guard_timeout_sec:=0.25", out)
        self.assertIn("serial_device:=/dev/custom_rrc", out)
        self.assertIn("baud_rate:=921600", out)
        self.assertIn("wheelbase:=0.165", out)
        self.assertIn("track_width:=0.175", out)
        self.assertIn("wheel_diameter:=0.075", out)
        self.assertIn("left_correction_factor:=1.08", out)
        self.assertIn("right_correction_factor:=0.94", out)

        # Verify invalid configuration (e.g. lease >= timeout) causes fatal exit
        invalid_cfg = os.path.join(self.etc_dir, "invalid_controller.yaml")
        with open(invalid_cfg, "w") as f:
            f.write(
                "version: '1.0'\n"
                "motor_guard:\n"
                "  max_rps: 2.0\n"
                "  timeout_sec: 0.200\n"
                "teleop:\n"
                "  lease_duration_sec: 0.250\n"
                "supervisor:\n"
                "  watchdog_ping_sec: 0.500\n"
            )
        env["UBUNTU_TANK_CONFIG"] = invalid_cfg
        res_bad = subprocess.run(
            [runner_path, "--dry-run"], env=env, capture_output=True, text=True
        )
        self.assertNotEqual(res_bad.returncode, 0)
        self.assertIn("failed validation", res_bad.stderr)

    def test_p1_finding5_stop_and_disarm_verifies_inactivity(self):
        """
        Finding 5: _stop_and_disarm_service verifies inactivity and fails if service remains active.
        """

        def mock_run_stop_fail(cmd, *args, **kwargs):
            if "is-active" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="active\n")
            if "stop" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=1, stderr="Failed to stop service\n"
                )
            return subprocess.CompletedProcess(cmd, returncode=0)

        with patch("shutil.which", return_value="/bin/systemctl"):
            with patch("subprocess.run", side_effect=mock_run_stop_fail):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mgr._stop_and_disarm_service(timeout_sec=0.1)
                self.assertIn("Failed to execute 'systemctl stop", str(ctx.exception))

        def mock_run_remains_active(cmd, *args, **kwargs):
            if "is-active" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="active\n")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("shutil.which", return_value="/bin/systemctl"):
            with patch("subprocess.run", side_effect=mock_run_remains_active):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mgr._stop_and_disarm_service(timeout_sec=0.1)
                self.assertIn("remains active after stop timeout", str(ctx.exception))

    def test_p1_finding6_propagate_unexpected_child_termination(self):
        """
        Finding 6: Propagate unexpected child termination as non-zero supervisor failure.
        Verify exit codes 17, 23, unexpected 0 exit, and graceful SIGTERM shutdown (exit 0).
        """
        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")
        base_env = os.environ.copy()
        base_env["UBUNTU_TANK_GUARD_SOCK"] = os.path.join(self.run_dir, "guard_hb.sock")
        base_env["UBUNTU_TANK_BRIDGE_SOCK"] = os.path.join(
            self.run_dir, "bridge_hb.sock"
        )
        base_env["ROS_LOG_DIR"] = os.path.join(self.var_dir, "ros-log")

        # 6a. Child exits with 17
        env17 = base_env.copy()
        env17["_UBUNTU_TANK_TEST_CHILD_CMD"] = "bash -c 'exit 17'"
        res17 = subprocess.run([runner_path], env=env17, capture_output=True, text=True)
        self.assertEqual(res17.returncode, 17)
        self.assertIn("terminated unexpectedly with code 17", res17.stderr)

        # 6b. Child exits with 23
        env23 = base_env.copy()
        env23["_UBUNTU_TANK_TEST_CHILD_CMD"] = "bash -c 'exit 23'"
        res23 = subprocess.run([runner_path], env=env23, capture_output=True, text=True)
        self.assertEqual(res23.returncode, 23)
        self.assertIn("terminated unexpectedly with code 23", res23.stderr)

        # 6c. Unexpected child exit 0 without signal must still fail closed (code 1)
        env0 = base_env.copy()
        env0["_UBUNTU_TANK_TEST_CHILD_CMD"] = "bash -c 'exit 0'"
        res0 = subprocess.run([runner_path], env=env0, capture_output=True, text=True)
        self.assertEqual(res0.returncode, 1)
        self.assertIn("terminated unexpectedly with code 0", res0.stderr)

        # 6d. Graceful signal termination exits 0
        env_sleep = base_env.copy()
        env_sleep["_UBUNTU_TANK_TEST_CHILD_CMD"] = "sleep 10"
        proc = subprocess.Popen(
            [runner_path], env=env_sleep, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Wait until runner has launched child and entered supervisor loop
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < 10.0:
            line = proc.stdout.readline()
            if b"Launching ROS graph" in line:
                break
            if proc.poll() is not None:
                break
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5.0)
        self.assertEqual(
            proc.returncode,
            0,
            f"Expected 0 on graceful SIGTERM, got {proc.returncode}. Stderr: {stderr.decode()}",
        )
        self.assertIn("Shutdown complete", stdout.decode())


class TestReviewFindingsRound2(BaseDeploymentTestCase):
    """
    Comprehensive regression and verification test suite for the 7 P1 review findings in review.md:
    1. Interrupted rollback is journaled and recoverable without mixed state.
    2. Retried activation reconciles or rejects pending recovery record.
    3. Start is serialized with deployment and gated on clean transaction state.
    4. Deactivating is waited on until complete; query errors and ambiguous status are rejected.
    5. Supervisor rejects wrong-PID, wrong-UID, stale, future, negative, and non-finite heartbeats.
    6. Unreadable or corrupt journals fail closed, blocking startup without altering assets.
    7. Udev serial identity is preserved across activation; ambiguous rules are rejected.
    """

    def test_finding3_startup_blocked_by_lock_and_transaction_gate(self):
        """
        Finding 3: Controller startup via mentorpi-tank-run and deploy.sh start is blocked
        while deployment lock is held or when an uncommitted transaction is pending.
        mentorpi-tank-recover.service must have RemainAfterExit=no.
        """
        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")

        # 3a. Verify startup blocked when DeploymentLock is held
        with DeploymentLock(self.mgr.lock_path):
            env_locked = os.environ.copy()
            env_locked["UBUNTU_TANK_LOCK_FILE"] = self.mgr.lock_path
            env_locked["UBUNTU_TANK_JOURNAL_FILE"] = self.mgr.journal.journal_path
            res = subprocess.run(
                [runner_path, "--dry-run"],
                env=env_locked,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Deployment lock", res.stderr)
            self.assertIn("held by another process", res.stderr)

        # 3b. Verify startup blocked when uncommitted transaction is pending
        tx_id = "tx-gate-test"
        self.mgr.journal.record_prepared(
            tx_id=tx_id,
            candidate_release_id="rel-cand",
            candidate_release_path="/cand/path",
            previous_release_id=None,
            previous_release_path=None,
            snapshot_dir="/snap/path",
        )
        env_pending = os.environ.copy()
        env_pending["UBUNTU_TANK_LOCK_FILE"] = self.mgr.lock_path
        env_pending["UBUNTU_TANK_JOURNAL_FILE"] = self.mgr.journal.journal_path
        res_pending = subprocess.run(
            [runner_path, "--dry-run"], env=env_pending, capture_output=True, text=True
        )
        self.assertEqual(res_pending.returncode, 1)
        self.assertIn("Uncommitted activation transaction", res_pending.stderr)

    def test_finding4_deactivating_waits_and_query_errors_rejected(self):
        """
        Finding 4: _stop_and_disarm_service handles 'deactivating' status by polling until inactive/failed,
        and rejects empty responses or ambiguous states with failure.
        """
        # 4a. Poll through deactivating until inactive
        status_seq = ["deactivating", "deactivating", "inactive"]
        call_idx = 0

        def mock_is_active(cmd, *args, **kwargs):
            nonlocal call_idx
            curr = status_seq[min(call_idx, len(status_seq) - 1)]
            call_idx += 1
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=f"{curr}\n")

        with patch("shutil.which", return_value="/bin/systemctl"):
            with patch("subprocess.run", side_effect=mock_is_active):
                # Must complete without error
                self.mgr._stop_and_disarm_service(timeout_sec=2.0)
                self.assertGreaterEqual(call_idx, 3)

        # 4b. Empty status from query failure must raise RuntimeError
        def mock_empty_query(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="")

        with patch("shutil.which", return_value="/bin/systemctl"):
            with patch("subprocess.run", side_effect=mock_empty_query):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mgr._stop_and_disarm_service()
                self.assertIn("empty response or query failure", str(ctx.exception))

        # 4c. Ambiguous status must raise RuntimeError
        def mock_ambiguous_query(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="maintenance\n"
            )

        with patch("shutil.which", return_value="/bin/systemctl"):
            with patch("subprocess.run", side_effect=mock_ambiguous_query):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mgr._stop_and_disarm_service()
                self.assertIn(
                    "Ambiguous or unexpected service status", str(ctx.exception)
                )

    def test_finding5_supervisor_credential_and_monotonic_freshness(self):
        """
        Finding 5: Supervisor validates Linux SCM_CREDENTIALS (PID, UID) and monotonic freshness.
        Wrong-PID, wrong-UID, stale, future, negative, and non-finite datagrams are rejected.
        """
        import importlib.util
        import struct
        from importlib.machinery import SourceFileLoader

        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")
        spec = importlib.util.spec_from_loader(
            "mentorpi_tank_run", SourceFileLoader("mentorpi_tank_run", runner_path)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        validate_heartbeat = mod.validate_heartbeat
        parse_timestamp = mod.parse_timestamp

        # 5a. parse_timestamp checks
        self.assertAlmostEqual(parse_timestamp(b"123.456789\n"), 123.456789, places=5)
        self.assertIsNone(parse_timestamp(b"-100.0\n"))
        self.assertIsNone(parse_timestamp(b"nan\n"))
        self.assertIsNone(parse_timestamp(b"inf\n"))
        self.assertIsNone(parse_timestamp(b"-inf\n"))
        self.assertIsNone(parse_timestamp(b"malformed"))

        # 5b. Credential and freshness validation
        expected_uid = 1000
        expected_pid = 2001
        now = 50.0
        deadline = 1.0

        def make_ancdata(pid, uid, gid=1000):
            cmsg_data = struct.pack("iii", pid, uid, gid)
            import socket

            return [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, cmsg_data)]

        # Valid heartbeat
        valid_msg = b"49.800000\n"
        ok, ts, reason = validate_heartbeat(
            msg=valid_msg,
            ancdata=make_ancdata(expected_pid, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertTrue(ok)
        self.assertAlmostEqual(ts, 49.8, places=5)

        # Reject wrong UID
        ok, ts, reason = validate_heartbeat(
            msg=valid_msg,
            ancdata=make_ancdata(expected_pid, 9999),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("UID 9999 does not match", reason)

        # Reject wrong PID
        ok, ts, reason = validate_heartbeat(
            msg=valid_msg,
            ancdata=make_ancdata(8888, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("PID 8888 does not match", reason)

        # Reject missing credentials
        ok, ts, reason = validate_heartbeat(
            msg=valid_msg,
            ancdata=[],
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("missing peer UID", reason)

        # Reject stale timestamp (age 2.0s > deadline 1.0s)
        stale_msg = b"48.000000\n"
        ok, ts, reason = validate_heartbeat(
            msg=stale_msg,
            ancdata=make_ancdata(expected_pid, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("exceeds deadline", reason)

        # Reject negative timestamp (-100.0)
        neg_msg = b"-100.000000\n"
        ok, ts, reason = validate_heartbeat(
            msg=neg_msg,
            ancdata=make_ancdata(expected_pid, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("Payload rejection", reason)

        # Reject future timestamp
        future_msg = b"60.000000\n"
        ok, ts, reason = validate_heartbeat(
            msg=future_msg,
            ancdata=make_ancdata(expected_pid, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=None,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("in the future", reason)

        # Reject out-of-order/replayed timestamp
        replayed_msg = b"49.500000\n"
        ok, ts, reason = validate_heartbeat(
            msg=replayed_msg,
            ancdata=make_ancdata(expected_pid, expected_uid),
            sender_name="guard",
            expected_pid=expected_pid,
            expected_uid=expected_uid,
            now_mono=now,
            last_accepted_ts=49.8,
            deadline_sec=deadline,
        )
        self.assertFalse(ok)
        self.assertIn("older than last accepted", reason)

    def test_finding6_corrupted_or_unreadable_journal_fails_closed(self):
        """
        Finding 6: Distinguish first installation from corrupt/unreadable journals.
        Truncated JSON, invalid schema, and read failures must fail closed without mutating assets.
        """
        # 6a. First installation (file does not exist): clean default
        if os.path.exists(self.mgr.journal.journal_path):
            os.unlink(self.mgr.journal.journal_path)
        state = self.mgr.journal.get_state()
        self.assertIsNone(state.get("current_transaction"))
        self.assertEqual(state.get("format_version"), "1.0")

        # 6b. Truncated JSON
        os.makedirs(os.path.dirname(self.mgr.journal.journal_path), exist_ok=True)
        with open(self.mgr.journal.journal_path, "w", encoding="utf-8") as f:
            f.write('{"format_version": "1.0", "active_release_id": ')
        with self.assertRaises(RuntimeError) as ctx:
            self.mgr.journal.get_state()
        self.assertIn("corrupted or unreadable", str(ctx.exception))
        # recover_activation fails closed
        self.assertFalse(self.mgr.recover_activation())

        # 6c. Invalid structure (missing format_version / history)
        with open(self.mgr.journal.journal_path, "w", encoding="utf-8") as f:
            f.write('{"something_else": 123}')
        with self.assertRaises(RuntimeError) as ctx:
            self.mgr.journal.get_state()
        self.assertIn("invalid schema or structure", str(ctx.exception))
        self.assertFalse(self.mgr.recover_activation())

    def test_finding7_udev_serial_identity_preserved_and_ambiguity_rejected(self):
        """
        Finding 7: Activation preserves existing host-specific ATTRS{serial} discriminator.
        Ambiguous udev rules matching multiple hardware devices without discriminator are rejected.
        """
        # Mock 2 connected USB serial adapters with same vendor/product
        dev1 = {
            "idVendor": "1a86",
            "idProduct": "55d4",
            "serial": "CH343_TANK_CONTROLLER",
        }
        dev2 = {
            "idVendor": "1a86",
            "idProduct": "55d4",
            "serial": "CH343_OTHER_ADAPTER",
        }

        # Host existing udev rule has discriminator for dev1
        host_rule = (
            'SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", '
            'ATTRS{serial}=="CH343_TANK_CONTROLLER", GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc", '
            'ENV{ID_MM_PORT_IGNORE}="1"\n'
        )
        host_udev_file = os.path.join(self.udev_dir, "99-mentorpi-rrc.rules")
        with open(host_udev_file, "w", encoding="utf-8") as f:
            f.write(host_rule)

        # Candidate release has generic template without serial discriminator
        candidate_rule = (
            'SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", '
            'GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc", ENV{ID_MM_PORT_IGNORE}="1"\n'
        )
        merged = ReleaseManager.merge_udev_rule(candidate_rule, host_rule)
        self.assertIn('ATTRS{serial}=="CH343_TANK_CONTROLLER"', merged)

        # Verify only dev1 matches the merged rule
        self.assertIn(dev1["serial"], merged)
        self.assertNotIn(dev2["serial"], merged)

        # Verify ambiguity rejection when multiple devices exist and rule lacks discriminator
        ambiguous, reason = ReleaseManager.is_ambiguous_udev_rule(
            candidate_rule, detected_devices=[dev1, dev2]
        )
        self.assertTrue(ambiguous)
        self.assertIn("discriminator", reason)

        # Verify wildcard discriminator is rejected
        wildcard_rule = candidate_rule.replace(
            'ATTRS{idProduct}=="55d4",', 'ATTRS{idProduct}=="55d4", ATTRS{serial}=="*",'
        )
        ambiguous_wc, reason_wc = ReleaseManager.is_ambiguous_udev_rule(wildcard_rule)
        self.assertTrue(ambiguous_wc)
        self.assertIn("wildcards", reason_wc)


class TestReviewFindingsRound3(BaseDeploymentTestCase):
    """
    Regression tests covering Milestone 5 Review Round 3 Findings (P1 x 3):
    - Finding 1: Build packaged install tree at its production prefix, free of leaked checkout paths,
                 and verify executable resolution and imports with checkout directory removed.
    - Finding 2: Interrupted host installation resumes host provisioning on retry, preserving existing
                 calibration without activating or starting the service.
    - Finding 3: Authoritative paths used for startup deployment lock and activation journal,
                 coordinating startup with deployment without deadlocking recovery, and rejecting
                 unreadable/corrupted journal state.
    """

    def test_finding3_startup_uses_authoritative_paths_and_serializes(self):
        """
        Finding 3: Startup coordination uses authoritative paths:
        /run/lock/ubuntu_tank/deploy.lock and /var/opt/ubuntu_tank/deployment/activation-journal.
        Startup transition acquires shared lock, rejecting startup if exclusive lock is held,
        failing closed on corrupt/unreadable journals, and preventing races during activation.
        """
        runner_path = os.path.join(self.workspace_dir, "bin", "mentorpi-tank-run")

        # 3a. Verify authoritative default constants
        from ubuntu_tank.scripts.deployment_manager import (
            DEFAULT_JOURNAL_PATH,
            DEFAULT_LOCK_PATH,
        )

        self.assertEqual(DEFAULT_LOCK_PATH, "/run/lock/ubuntu_tank/deploy.lock")
        self.assertEqual(
            DEFAULT_JOURNAL_PATH, "/var/opt/ubuntu_tank/deployment/activation-journal"
        )

        # 3b. Verify startup fails closed when deployment lock is held
        with DeploymentLock(self.mgr.lock_path):
            env_test = os.environ.copy()
            env_test["UBUNTU_TANK_LOCK_FILE"] = self.mgr.lock_path
            env_test["UBUNTU_TANK_JOURNAL_FILE"] = self.mgr.journal.journal_path
            res = subprocess.run(
                [runner_path, "--dry-run"], env=env_test, capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Deployment lock at", res.stderr)
            self.assertIn("held by another process", res.stderr)

        # 3c. Verify startup fails closed on corrupt journal
        os.makedirs(os.path.dirname(self.mgr.journal.journal_path), exist_ok=True)
        with open(self.mgr.journal.journal_path, "w", encoding="utf-8") as f:
            f.write("{corrupt_json_payload")
        env_test = os.environ.copy()
        env_test["UBUNTU_TANK_LOCK_FILE"] = self.mgr.lock_path
        env_test["UBUNTU_TANK_JOURNAL_FILE"] = self.mgr.journal.journal_path
        res_corrupt = subprocess.run(
            [runner_path, "--dry-run"], env=env_test, capture_output=True, text=True
        )
        self.assertEqual(res_corrupt.returncode, 1)
        self.assertIn("Failed to read activation journal", res_corrupt.stderr)

        # 3d. Verify startup fails closed on invalid schema
        with open(self.mgr.journal.journal_path, "w", encoding="utf-8") as f:
            f.write('{"missing_fields": true}')
        res_schema = subprocess.run(
            [runner_path, "--dry-run"], env=env_test, capture_output=True, text=True
        )
        self.assertEqual(res_schema.returncode, 1)
        self.assertIn("has invalid schema", res_schema.stderr)

        # 3e. Verify activation holding lock prevents controller launch during asset changes
        out_dist = os.path.join(self.test_root, "dist")
        archive = self.mgr.package_release(
            self.workspace_dir, out_dist, "1.0.0-startrace", allow_staged_install=True
        )
        self.mgr.install_release(archive, require_root=False)

        # Simulate activation holding deploy.lock and staging assets
        with DeploymentLock(self.mgr.lock_path):
            res_launch = subprocess.run(
                [runner_path, "--dry-run"], env=env_test, capture_output=True, text=True
            )
            self.assertEqual(res_launch.returncode, 1)
            self.assertIn(
                "Cannot start controller during deployment", res_launch.stderr
            )


class TestReviewFindingsRound4(BaseDeploymentTestCase):
    """
    Regression tests covering Milestone 5 Review Round 4 Findings (P1 x 4):
    - Finding 1: Reject unsafe release IDs before deleting staging directories.
                 Validate IDs as single path-safe components and verify staging containment.
                 Test absolute paths, traversal, and symlink escapes, asserting no deletion occurs.
    - Finding 2: Constrain disposable-build cleanup to disposable directories.
                 Reject workspace roots, ancestors, system directories, and symlink escapes.
                 Verify rejected inputs preserve sentinel files.
    - Finding 3: Remove the executable trailing EOF from build_disposable_root.sh.
                 Test successful builder invocation through packaging subprocess path,
                 checking both exit status 0 and archive creation.
    - Finding 4: Build inside disposable root at actual production prefix.
                 Verify dry-run plan specifies container isolation root and internal production prefix,
                 verify rootfs architecture check rejects non-ARM64 / non-Ubuntu,
                 and verify hardware-free ROS artifact architecture and installed imports
                 after removing access to checkout and build root.
    """

    def test_finding1_reject_unsafe_release_id_and_staging_containment(self):
        """
        Finding 1: Validate release IDs as single path-safe components and enforce
        staging containment before mutation. Verify traversal, absolute paths, and
        symlink escapes are rejected without deleting target files.
        """
        tmp_ws = tempfile.mkdtemp(prefix="ubuntu_tank_test_f1_")
        try:
            # Create a mock workspace with src and VERSION
            ws_src = os.path.join(tmp_ws, "src")
            os.makedirs(ws_src, exist_ok=True)
            sentinel_path = os.path.join(ws_src, "sentinel.txt")
            with open(sentinel_path, "w", encoding="utf-8") as f:
                f.write("PROTECTED_WORKSPACE_SOURCE")

            os.makedirs(os.path.join(tmp_ws, "bin"), exist_ok=True)
            os.makedirs(os.path.join(tmp_ws, "config"), exist_ok=True)
            os.makedirs(os.path.join(tmp_ws, "host"), exist_ok=True)
            os.makedirs(os.path.join(tmp_ws, "scripts"), exist_ok=True)
            with open(os.path.join(tmp_ws, "VERSION"), "w", encoding="utf-8") as f:
                f.write("1.0.0\n")

            out_dist = os.path.join(self.test_root, "dist_f1")
            os.makedirs(out_dist, exist_ok=True)

            # 1a. Traversal attack: package --release-id ../../src
            with self.assertRaises(ValueError) as ctx:
                self.mgr.package_release(
                    tmp_ws, out_dist, release_id="../../src", allow_staged_install=True
                )
            self.assertIn("must be a single path component", str(ctx.exception))
            # Verify sentinel file in src was NOT deleted
            self.assertTrue(os.path.isfile(sentinel_path))
            with open(sentinel_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "PROTECTED_WORKSPACE_SOURCE")

            # 1b. Absolute path escape: package --release-id /tmp/foo
            abs_target = os.path.join(tmp_ws, "abs_escape")
            os.makedirs(abs_target, exist_ok=True)
            abs_sentinel = os.path.join(abs_target, "keep.txt")
            with open(abs_sentinel, "w", encoding="utf-8") as f:
                f.write("DO_NOT_DELETE")

            with self.assertRaises(ValueError) as ctx:
                self.mgr.package_release(
                    tmp_ws, out_dist, release_id=abs_target, allow_staged_install=True
                )
            self.assertIn("must be a single path component", str(ctx.exception))
            self.assertTrue(os.path.isfile(abs_sentinel))

            # 1c. Symlink escape in staging
            staging_parent = os.path.join(tmp_ws, ".work", "staging")
            os.makedirs(staging_parent, exist_ok=True)
            symlink_escape = os.path.join(staging_parent, "symlink_rel")
            os.symlink(ws_src, symlink_escape)

            with self.assertRaises(ValueError) as ctx:
                self.mgr.package_release(
                    tmp_ws,
                    out_dist,
                    release_id="symlink_rel",
                    allow_staged_install=True,
                )
            self.assertIn("is a symlink; refusing mutation", str(ctx.exception))
            self.assertTrue(os.path.isfile(sentinel_path))

            # 1d. validate_release_id unit checks
            self.assertEqual(
                ReleaseManager.validate_release_id("1.0.0-g1234567"), "1.0.0-g1234567"
            )
            self.assertEqual(
                ReleaseManager.validate_release_id("release_1.2.3-alpha"),
                "release_1.2.3-alpha",
            )
            for bad_id in [
                ".",
                "..",
                "foo/bar",
                "foo\\bar",
                "foo\0bar",
                "/abs",
                "-bad",
                "with spaces",
                "",
            ]:
                with self.assertRaises(ValueError):
                    ReleaseManager.validate_release_id(bad_id)
        finally:
            if os.path.exists(tmp_ws):
                shutil.rmtree(tmp_ws)

    def test_finding2_constrain_disposable_build_cleanup(self):
        """
        Finding 2: Constrain disposable-build cleanup to disposable directories.
        Reject workspace roots, ancestors, system directories, and symlink escapes.
        Verify rejected inputs preserve sentinel files.
        """
        builder_script = os.path.join(
            self.workspace_dir, "scripts", "build_disposable_root.sh"
        )
        self.assertTrue(os.path.isfile(builder_script))

        tmp_test = tempfile.mkdtemp(prefix="ubuntu_tank_test_f2_")
        try:
            ws_mock = os.path.join(tmp_test, "ws_mock")
            os.makedirs(os.path.join(ws_mock, "src"), exist_ok=True)
            sentinel_file = os.path.join(ws_mock, "src", "sentinel.txt")
            with open(sentinel_file, "w", encoding="utf-8") as f:
                f.write("CRITICAL_SOURCE_DATA")
            with open(os.path.join(ws_mock, "VERSION"), "w", encoding="utf-8") as f:
                f.write("1.0.0\n")

            # 2a. Reject cleaning workspace root
            res = subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    ws_mock,
                    "--clean",
                    "--build-root",
                    ws_mock,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Refusing to clean workspace root", res.stderr)
            self.assertTrue(os.path.isfile(sentinel_file))

            # 2b. Reject cleaning workspace ancestor
            res = subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    ws_mock,
                    "--clean",
                    "--build-root",
                    tmp_test,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Refusing to clean workspace ancestor", res.stderr)
            self.assertTrue(os.path.isfile(sentinel_file))

            # 2c. Reject cleaning system directories
            for sys_dir in ["/", "/home", "/usr", "/var", "/tmp"]:
                res = subprocess.run(
                    [
                        builder_script,
                        "--workspace",
                        ws_mock,
                        "--clean",
                        "--build-root",
                        sys_dir,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("Refusing to clean broad or system directory", res.stderr)

            # 2d. Reject symlink escape
            symlink_target = os.path.join(tmp_test, "symlink_build_root")
            os.symlink(ws_mock, symlink_target)
            res = subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    ws_mock,
                    "--clean",
                    "--build-root",
                    symlink_target,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Refusing to clean symlinked target", res.stderr)
            self.assertTrue(os.path.isfile(sentinel_file))

            # 2e. Valid disposable subtree under .work is accepted
            valid_disp = os.path.join(ws_mock, ".work", "build_root")
            os.makedirs(valid_disp, exist_ok=True)
            disp_sentinel = os.path.join(valid_disp, "delete_me.txt")
            with open(disp_sentinel, "w", encoding="utf-8") as f:
                f.write("disposable")

            res = subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    ws_mock,
                    "--clean",
                    "--build-root",
                    valid_disp,
                    "--allow-staged-install",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertFalse(os.path.exists(disp_sentinel))
            self.assertTrue(os.path.isfile(sentinel_file))
        finally:
            if os.path.exists(tmp_test):
                shutil.rmtree(tmp_test)


class TestReviewFindingsRound5(BaseDeploymentTestCase):
    """
    Dedicated regression test suite covering Review Round 5 findings:
    1. Production builds must not pass unsupported colcon option (--no-symlink-install).
       Exercise both generated colcon commands against the locked colcon argument parser.
    2. Packaging must not select empty install trees left by builder or failed builds.
       Copy completed output explicitly from rootfs to target build root;
       Simulate compilation producing recognizable files only inside rootfs and assert they reach archive;
       Repeat after failed build and require packaging to fail;
       Verify release validation rejects empty/incomplete install trees.
    """

    def test_finding1_colcon_options_and_parser_verification(self):
        """
        Finding 1: Verify colcon build commands in build_disposable_root.sh do not pass
        the unsupported '--no-symlink-install' option, and parse cleanly against the
        upstream colcon build verb argument parser definition across dry-run,
        systemd-nspawn, and chroot production build branches.
        """
        import argparse
        import shlex

        builder_script = os.path.join(
            self.workspace_dir, "scripts", "build_disposable_root.sh"
        )

        # 1a. Define locked upstream colcon build argument parser
        # (models colcon_core.verb.build upstream argument definitions)
        def create_colcon_build_parser():
            parser = argparse.ArgumentParser(prog="colcon build", add_help=False)
            parser.add_argument("--base-paths", nargs="*")
            parser.add_argument("--build-base")
            parser.add_argument("--install-base")
            parser.add_argument("--merge-install", action="store_true")
            parser.add_argument("--symlink-install", action="store_true")
            parser.add_argument("--packages-select", nargs="*")
            parser.add_argument("--packages-skip", nargs="*")
            return parser

        colcon_parser = create_colcon_build_parser()

        # Demonstrate that passing --no-symlink-install causes colcon argument parsing failure
        with self.assertRaises(SystemExit):
            colcon_parser.parse_args(
                ["--install-base", "/opt/test", "--no-symlink-install"]
            )

        # 1b. Validate dry-run output
        res_dry = subprocess.run(
            [
                builder_script,
                "--workspace",
                self.workspace_dir,
                "--release-id",
                "1.0.0-testcolcon",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_dry.returncode, 0)
        self.assertNotIn("--no-symlink-install", res_dry.stdout)

        tokens_dry = None
        for line in res_dry.stdout.splitlines():
            if "colcon build" in line:
                cmd_part = line.split("colcon build", 1)[1].strip()
                tokens_dry = shlex.split(cmd_part)
                parsed_args = colcon_parser.parse_args(tokens_dry)
                self.assertFalse(parsed_args.symlink_install)
                self.assertTrue(parsed_args.merge_install)
                self.assertEqual(
                    parsed_args.install_base,
                    "/opt/ubuntu_tank/releases/1.0.0-testcolcon/install",
                )
        self.assertIsNotNone(tokens_dry, "Dry-run colcon command not found")

        # 1c. Validate production systemd-nspawn and chroot branches via test harness shims
        with tempfile.TemporaryDirectory() as td:
            # Create verified arm64 rootfs fixture
            rootfs = os.path.join(td, "rootfs")
            os.makedirs(os.path.join(rootfs, "etc"), exist_ok=True)
            os.makedirs(os.path.join(rootfs, "bin"), exist_ok=True)
            os.makedirs(os.path.join(rootfs, "var/lib/dpkg"), exist_ok=True)
            os.makedirs(os.path.join(rootfs, "opt/ros/lyrical"), exist_ok=True)
            with open(os.path.join(rootfs, "etc", "os-release"), "w") as f:
                f.write('ID=ubuntu\nVERSION_ID="26.04"\n')
            with open(os.path.join(rootfs, "var/lib/dpkg/arch"), "w") as f:
                f.write("arm64\n")
            with open(os.path.join(rootfs, "opt/ros/lyrical/setup.bash"), "w") as f:
                f.write("#!/bin/bash\n")
            shutil.copy2("/bin/sh", os.path.join(rootfs, "bin", "sh"))

            # Branch 1: systemd-nspawn execution capture
            shim_dir_nspawn = os.path.join(td, "shims_nspawn")
            os.makedirs(shim_dir_nspawn, exist_ok=True)
            nspawn_cap = os.path.join(td, "nspawn_cmd.txt")
            with open(os.path.join(shim_dir_nspawn, "systemd-nspawn"), "w") as f:
                f.write(f"""#!/bin/bash
for ((i=1;i<=$#;i++)); do
  if [ "${{!i}}" = "-c" ]; then
    next=$((i+1))
    echo "${{!next}}" > "{nspawn_cap}"
  fi
done
exit 0
""")
            os.chmod(os.path.join(shim_dir_nspawn, "systemd-nspawn"), 0o755)
            with open(os.path.join(shim_dir_nspawn, "sudo"), "w") as f:
                f.write("""#!/bin/bash
while [[ "$1" == -* ]]; do shift; done
"$@"
""")
            os.chmod(os.path.join(shim_dir_nspawn, "sudo"), 0o755)

            env_nspawn = dict(os.environ)
            env_nspawn["PATH"] = f"{shim_dir_nspawn}:{env_nspawn['PATH']}"
            env_nspawn.pop("_UBUNTU_TANK_TEST_BUILD_CMD", None)

            subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    self.workspace_dir,
                    "--rootfs",
                    rootfs,
                    "--release-id",
                    "1.0.0-nspawn-val",
                    "--allow-staged-install",
                ],
                env=env_nspawn,
                capture_output=True,
                text=True,
            )

            self.assertTrue(
                os.path.isfile(nspawn_cap),
                "systemd-nspawn production branch was not executed",
            )
            with open(nspawn_cap, "r", encoding="utf-8") as f:
                nspawn_script = f.read()

            colcon_line_nspawn = None
            for line in nspawn_script.splitlines():
                if "colcon build" in line:
                    colcon_line_nspawn = line
                    break
            self.assertIsNotNone(
                colcon_line_nspawn,
                "colcon build command missing in systemd-nspawn branch",
            )
            cmd_part_ns = colcon_line_nspawn.split("colcon build", 1)[1].strip()
            tokens_ns = shlex.split(cmd_part_ns)
            parsed_ns = colcon_parser.parse_args(tokens_ns)
            self.assertFalse(parsed_ns.symlink_install)
            self.assertTrue(parsed_ns.merge_install)
            self.assertEqual(
                parsed_ns.install_base,
                "/opt/ubuntu_tank/releases/1.0.0-nspawn-val/install",
            )

            # Branch 2: chroot execution capture (hide systemd-nspawn)
            shim_dir_chroot = os.path.join(td, "shims_chroot")
            os.makedirs(shim_dir_chroot, exist_ok=True)
            chroot_cap = os.path.join(td, "chroot_cmd.txt")
            with open(os.path.join(shim_dir_chroot, "chroot"), "w") as f:
                f.write(f"""#!/bin/bash
for ((i=1;i<=$#;i++)); do
  if [ "${{!i}}" = "-c" ]; then
    next=$((i+1))
    echo "${{!next}}" > "{chroot_cap}"
  fi
done
exit 0
""")
            os.chmod(os.path.join(shim_dir_chroot, "chroot"), 0o755)
            for u in ["sudo", "mount", "umount"]:
                with open(os.path.join(shim_dir_chroot, u), "w") as f:
                    if u == "sudo":
                        f.write("""#!/bin/bash
while [[ "$1" == -* ]]; do shift; done
"$@"
""")
                    else:
                        f.write("#!/bin/bash\nexit 0\n")
                os.chmod(os.path.join(shim_dir_chroot, u), 0o755)

            clean_bin = os.path.join(td, "clean_bin")
            os.makedirs(clean_bin, exist_ok=True)
            for p in ["/bin", "/usr/bin"]:
                if os.path.isdir(p):
                    for name in os.listdir(p):
                        if name != "systemd-nspawn" and not os.path.exists(
                            os.path.join(clean_bin, name)
                        ):
                            try:
                                os.symlink(
                                    os.path.join(p, name),
                                    os.path.join(clean_bin, name),
                                )
                            except OSError:
                                pass

            env_chroot = dict(os.environ)
            env_chroot["PATH"] = f"{shim_dir_chroot}:{clean_bin}"
            env_chroot.pop("_UBUNTU_TANK_TEST_BUILD_CMD", None)

            subprocess.run(
                [
                    builder_script,
                    "--workspace",
                    self.workspace_dir,
                    "--rootfs",
                    rootfs,
                    "--release-id",
                    "1.0.0-chroot-val",
                    "--allow-staged-install",
                ],
                env=env_chroot,
                capture_output=True,
                text=True,
            )

            self.assertTrue(
                os.path.isfile(chroot_cap),
                "chroot production branch was not executed",
            )
            with open(chroot_cap, "r", encoding="utf-8") as f:
                chroot_script = f.read()

            colcon_line_chroot = None
            for line in chroot_script.splitlines():
                if "colcon build" in line:
                    colcon_line_chroot = line
                    break
            self.assertIsNotNone(
                colcon_line_chroot,
                "colcon build command missing in chroot branch",
            )
            cmd_part_ch = colcon_line_chroot.split("colcon build", 1)[1].strip()
            tokens_ch = shlex.split(cmd_part_ch)
            parsed_ch = colcon_parser.parse_args(tokens_ch)
            self.assertFalse(parsed_ch.symlink_install)
            self.assertTrue(parsed_ch.merge_install)
            self.assertEqual(
                parsed_ch.install_base,
                "/opt/ubuntu_tank/releases/1.0.0-chroot-val/install",
            )

            # Mutation regression: injecting --no-symlink-install into any branch fails parser
            for branch_tokens in [tokens_dry, tokens_ns, tokens_ch]:
                with self.assertRaises(SystemExit):
                    colcon_parser.parse_args(branch_tokens + ["--no-symlink-install"])


class TestReviewFindingsRound6(BaseDeploymentTestCase):
    """
    Regression tests covering Review Round 6 findings:
    - Finding 1: Mutual exclusion prevents startup across deploy.sh, mentorpi-tank-run,
      and recovery runner when Docker inventory is unreadable, conflicting containers exist,
      conflicting systemd units are active, or serial device is held open.
    - Finding 2: Fresh installation provisions signed SROS2 keystore with enclaves,
      verifying against tank.launch.py preflight and OpenSSL validation.
    - Finding 3: Configured bridge stop deadline (freshness_timeout_sec) is forwarded into
      launch arguments, validated against write_timeout_sec, and trips watchdog on command gap.
    - Finding 4: ROS environment sourcing tolerates unset AMENT_TRACE_SETUP_FILES
      by temporarily disabling nounset.
    """

    def test_finding3_bridge_freshness_timeout_forwarding_and_watchdog_trip(self):
        """Finding 3: Configured bridge stop deadline is forwarded, validated, and trips watchdog."""
        bin_dir = os.path.join(self.workspace_dir, "bin")
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        # Import build_launch_arguments from mentorpi-tank-run
        import importlib.util
        from importlib.machinery import SourceFileLoader

        run_path = os.path.join(bin_dir, "mentorpi-tank-run")
        loader = SourceFileLoader("mentorpi_tank_run_mod", run_path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        run_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(run_mod)

        # 3a. Config validation rejects write_timeout_sec >= freshness_timeout_sec
        bad_cfg = copy.deepcopy(DEFAULTS_V1_0)
        bad_cfg["serial_bridge"]["write_timeout_sec"] = 0.250
        bad_cfg["serial_bridge"]["freshness_timeout_sec"] = 0.200
        ok, errs = validate_config(bad_cfg)
        self.assertFalse(ok)
        self.assertTrue(any("strictly less than" in e for e in errs))

        # build_launch_arguments also raises ValueError if write_timeout_sec >= freshness_timeout_sec
        with self.assertRaises(ValueError) as ctx:
            run_mod.build_launch_arguments(bad_cfg)
        self.assertIn("strictly less than", str(ctx.exception))

        # 3b. build_launch_arguments forwards freshness_timeout_sec when valid
        valid_cfg = copy.deepcopy(DEFAULTS_V1_0)
        valid_cfg["serial_bridge"]["freshness_timeout_sec"] = 0.150
        valid_cfg["serial_bridge"]["write_timeout_sec"] = 0.050
        args = run_mod.build_launch_arguments(valid_cfg)
        self.assertIn("freshness_timeout_sec:=0.15", args)
        self.assertIn("write_timeout_sec:=0.05", args)

        # 3c. tank.launch.py declares freshness_timeout_sec and passes it to bridge node
        launch_file = os.path.join(
            self.workspace_dir, "src", "ubuntu_tank_bringup", "launch", "tank.launch.py"
        )
        with open(launch_file, "r") as f:
            launch_content = f.read()
        import re

        self.assertIsNotNone(
            re.search(
                r"DeclareLaunchArgument\(\s*['\"]freshness_timeout_sec['\"]",
                launch_content,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"['\"]freshness_timeout_sec['\"]\s*:\s*freshness_timeout_sec",
                launch_content,
            )
        )

        # 3d. Verify watchdog trips at configured deadline (0.150s) when command gap occurs
        sys.path.insert(
            0, os.path.join(self.workspace_dir, "src", "ros_robot_controller")
        )
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge_freshness")
        node.running = True
        node.freshness_timeout_sec = 0.150  # configured 150 ms deadline
        node.write_timeout_sec = 0.050

        # Command gap of 140 ms (below 150 ms deadline): watchdog does NOT trip
        node._last_motor_cmd_time = time.monotonic() - 0.140
        node._motor_watchdog_step()
        self.assertFalse(node._fatal_fault)

        # Command gap of 160 ms (exceeds 150 ms deadline): watchdog trips
        node._last_motor_cmd_time = time.monotonic() - 0.160
        node._motor_watchdog_step()
        self.assertTrue(node._fatal_fault)
        mock_board.zero_motors.assert_called_with(count=4)


class TestReviewFindingsRound7(BaseDeploymentTestCase):
    """Exercise production signing, policy transactions, deployment gates and role access."""

    def _provision(self, release=None):
        self.mgr._provision_sros2_keystore(
            release or self.workspace_dir, os.geteuid(), os.getegid()
        )
        return Path(self.etc_dir) / "security" / "keystore"

    def test_authentic_dds_accepts_generated_credentials(self):
        """Initialize real Fast DDS participants without creating any motion endpoints."""
        available = subprocess.run(
            [sys.executable, "-c", "import rclpy"], capture_output=True
        )
        if available.returncode:
            self.skipTest(
                "Native ROS 2 unavailable: run this test with Lyrical sourced on the target"
            )
        store = self._provision()
        code = """
import rclpy
from rclpy.parameter import Parameter
rclpy.init()
node = rclpy.create_node('credential_probe', enable_rosout=False,
    start_parameter_services=False,
    parameter_overrides=[Parameter('start_type_description_service', value=False)])
node.destroy_node()
rclpy.shutdown()
"""
        env = dict(
            os.environ,
            ROS_SECURITY_ENABLE="true",
            ROS_SECURITY_STRATEGY="Enforce",
            ROS_SECURITY_KEYSTORE=str(store),
            ROS_LOCALHOST_ONLY="1",
            ROS_DOMAIN_ID="0",
            RMW_IMPLEMENTATION="rmw_fastrtps_cpp",
        )
        for role in ("controller", "guard", "bridge", "operator", "status"):
            env["ROS_SECURITY_ENCLAVE_OVERRIDE"] = "/ubuntu_tank/" + role
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (store / "enclaves/ubuntu_tank/status/permissions.p7s").write_text("tampered")
        denied = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(
            denied.returncode, 0, "Middleware accepted tampered credentials"
        )

    def test_initial_identity_selection_reads_serial_or_stable_usb_port(self):
        """Bind actual inventory attributes and reject multiple initial candidates."""
        from unittest.mock import patch

        inventory = Path(self.test_root) / "usb"
        board = inventory / "1-2.3"
        board.mkdir(parents=True)
        (board / "idVendor").write_text("1a86")
        (board / "idProduct").write_text("55d4")
        (board / "serial").write_text("selected-serial")
        rule = (Path(self.workspace_dir) / "host/99-mentorpi-rrc.rules").read_text()
        real_scandir = os.scandir
        with patch(
            "os.scandir",
            side_effect=lambda path: real_scandir(
                inventory if path == "/sys/bus/usb/devices" else path
            ),
        ):
            selected = self.mgr._bind_udev_identity(rule)
            self.assertIn('ATTRS{serial}=="selected-serial"', selected)
            (board / "serial").unlink()
            selected = self.mgr._bind_udev_identity(rule)
            self.assertIn('KERNELS=="1-2.3"', selected)
            shutil.copytree(board, inventory / "1-2.4")
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                self.mgr._bind_udev_identity(rule)

    def test_persistent_identity_required_and_preserved(self):
        """Current uniqueness alone cannot authorize a broad rule; selection persists over enumeration."""
        rule = (Path(self.workspace_dir) / "host/99-mentorpi-rrc.rules").read_text()
        for inventory in ([], [{"idVendor": "1a86", "idProduct": "55d4"}]):
            self.assertTrue(ReleaseManager.is_ambiguous_udev_rule(rule, inventory)[0])
        bound = ReleaseManager.merge_udev_rule(rule, 'ATTRS{serial}=="chosen-board"')
        for inventory in (
            [{"serial": "other"}, {"serial": "chosen-board"}],
            [{"serial": "chosen-board"}, {"serial": "other"}],
        ):
            self.assertFalse(ReleaseManager.is_ambiguous_udev_rule(bound, inventory)[0])
            self.assertIn(
                'ATTRS{serial}=="chosen-board"',
                ReleaseManager.merge_udev_rule(rule, bound),
            )
        for invalid in ("*", "board?", "[ab]", "<verified-serial>"):
            self.assertTrue(
                ReleaseManager.is_ambiguous_udev_rule(
                    ReleaseManager.merge_udev_rule(
                        rule, 'ATTRS{serial}=="' + invalid + '"'
                    )
                )[0]
            )


class TestReviewFindingsRound8(BaseDeploymentTestCase):
    """Functional production packaging, retry identity and host teleop regressions."""

    def test_host_teleop_settings_reach_cli_and_lease(self):
        """Normal keyboard command uses host speed caps and the configured lease."""
        import copy

        import yaml

        from ubuntu_tank.scripts.config_migration import DEFAULTS_V1_0, teleop_arguments

        config = copy.deepcopy(DEFAULTS_V1_0)
        config["controller"]["max_linear_speed"] = 0.05
        config["controller"]["max_angular_speed"] = 0.1
        config["teleop"]["lease_duration_sec"] = 0.08
        file = Path(self.etc_dir) / "controller.yaml"
        file.write_text(yaml.safe_dump(config))
        args = teleop_arguments(str(file))
        self.assertIn("linear_vel:=0.05", args)
        self.assertIn("angular_vel:=0.1", args)
        self.assertIn("lease_duration_sec:=0.08", args)
        binary = Path(self.test_root) / "bin"
        binary.mkdir()
        ros2 = binary / "ros2"
        ros2.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        ros2.chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(binary) + os.pathsep + os.environ["PATH"],
            UBUNTU_TANK_CONFIG=str(file),
        )
        result = subprocess.run(
            ["bash", str(Path(self.workspace_dir) / "deploy.sh"), "teleop"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("linear_vel:=0.05", result.stdout)
        self.assertIn("lease_duration_sec:=0.08", result.stdout)
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lease_fixture",
            Path(self.workspace_dir)
            / "src/ubuntu_tank_teleop/ubuntu_tank_teleop/lease.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lease = module.TeleopLeaseManager(
            linear_vel=0.05, angular_vel=0.1, lease_duration_sec=0.08
        )
        self.assertEqual(lease.process_key("w", 10.0)[0], 0.05)
        self.assertEqual(lease.get_velocities(10.081), (0.0, 0.0))

    def test_wrong_prefix_and_changed_payload_are_rejected(self):
        """A relabelled or changed supplied build cannot masquerade as a new release."""
        from ubuntu_tank.scripts.deployment_manager import attest_build, verify_build

        tree = Path(self.test_root) / "install"
        tree.mkdir()
        source = str(Path(self.workspace_dir) / "src")
        (tree / "setup.bash").write_text(
            "export AMENT_PREFIX_PATH=/opt/ubuntu_tank/releases/OLD/install\n"
        )
        (tree / "payload.py").write_text("value=1\n")
        prefix = f"{self.opt_dir}/releases/NEW/install"
        attest_build(str(tree), prefix, source)
        with self.assertRaisesRegex(ValueError, "another production prefix"):
            verify_build(str(tree), prefix, source)
        (tree / "setup.bash").write_text(f"export AMENT_PREFIX_PATH={prefix}\n")
        attest_build(str(tree), prefix, source)
        (tree / "payload.py").write_text("value=2\n")
        with self.assertRaisesRegex(ValueError, "hashes"):
            verify_build(str(tree), prefix, source)

    def test_bootstrap_dry_run_and_destination_guards(self):
        """Root creation is a documented command and cannot overwrite broad host paths."""
        from unittest.mock import patch

        from ubuntu_tank.scripts.prepare_build_root import bootstrap

        workspace = Path(self.test_root) / "workspace"
        with self.assertRaises(ValueError):
            bootstrap(workspace, Path("/usr"), dry_run=True)
        with (
            patch("os.geteuid", return_value=0),
            patch(
                "ubuntu_tank.scripts.prepare_build_root.verify_host",
                side_effect=RuntimeError("wrong host"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong host"):
                bootstrap(workspace, workspace / ".work/rootfs")
        self.assertFalse((workspace / ".work").exists())
        result = subprocess.run(
            [
                "bash",
                str(Path(self.workspace_dir) / "scripts/build_disposable_root.sh"),
                "--workspace",
                str(workspace),
                "--release-id",
                "dry",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".work/native-rootfs", result.stdout)
        self.assertIn(".work/native-build", result.stdout)
        bootstrap(workspace, workspace / ".work/rootfs", dry_run=True)
        self.assertFalse((workspace / ".work").exists())


if __name__ == "__main__":
    unittest.main()
