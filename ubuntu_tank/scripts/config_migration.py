#!/usr/bin/env python3
"""
Configuration schema validation, forward migration, and downgrade restoration for Ubuntu Tank.

Validates and migrates /etc/opt/ubuntu_tank/controller.yaml across release schemas
while strictly preserving user calibrations and safety limits.
"""

import argparse
import copy
import math
import os
import sys
import yaml
from typing import Any, Dict, List, Tuple


SCHEMA_VERSIONS = ["1.0", "1.1"]

DEFAULTS_V1_0 = {
    "controller": {
        "machine_type": "MentorPi_Tank",
        "wheelbase": 0.1368,
        "track_width": 0.1446,
        "wheel_diameter": 0.075,
        "max_linear_speed": 0.5,
        "max_angular_speed": 2.0,
        "correction_factor": {"left": 1.0, "right": 1.0},
    },
    "serial_bridge": {
        "serial_device": "/dev/rrc",
        "baud_rate": 1000000,
        "controller_only": True,
        "freshness_timeout_sec": 0.250,
    },
    "motor_guard": {
        "max_rps": 2.0,
        "timeout_sec": 0.250,
        "check_rate_hz": 50.0,
        "heartbeat_interval_sec": 0.200,
    },
    "supervisor": {
        "guard_deadline_sec": 1.0,
        "bridge_deadline_sec": 1.0,
        "check_interval_sec": 0.200,
    },
    "teleop": {"linear_speed": 0.2, "angular_speed": 0.8, "lease_duration_sec": 0.150},
}

# Schema v1.1 adds optional watchdog parameter in supervisor and telemetry rate in bridge
DEFAULTS_V1_1 = copy.deepcopy(DEFAULTS_V1_0)
DEFAULTS_V1_1["supervisor"]["watchdog_ping_sec"] = 0.500
DEFAULTS_V1_1["serial_bridge"]["telemetry_rate_hz"] = 50.0


def validate_config(
    cfg: Dict[str, Any], version: str = "1.0"
) -> Tuple[bool, List[str]]:
    """Validate configuration dictionary against schema constraints."""
    errors = []
    if not isinstance(cfg, dict):
        return False, ["Root configuration must be a mapping/dict"]

    required_sections = [
        "controller",
        "serial_bridge",
        "motor_guard",
        "supervisor",
        "teleop",
    ]
    for s in required_sections:
        if s not in cfg or not isinstance(cfg[s], dict):
            errors.append(f"Missing or invalid section '{s}'")

    if errors:
        return False, errors

    # Controller validation
    c = cfg["controller"]
    for k in [
        "wheelbase",
        "track_width",
        "wheel_diameter",
        "max_linear_speed",
        "max_angular_speed",
    ]:
        if (
            k not in c
            or not isinstance(c[k], (int, float))
            or not math.isfinite(c[k])
            or c[k] <= 0
        ):
            errors.append(f"controller.{k} must be a positive number")

    corr = c.get("correction_factor", {})
    if not isinstance(corr, dict) or "left" not in corr or "right" not in corr:
        errors.append(
            "controller.correction_factor must contain positive numbers for left and right"
        )
    else:
        for side in ["left", "right"]:
            if not isinstance(corr[side], (int, float)) or corr[side] <= 0:
                errors.append(
                    f"controller.correction_factor.{side} must be a positive number"
                )

    # Serial bridge validation
    sb = cfg["serial_bridge"]
    dev = sb.get("serial_device")
    if not isinstance(dev, str) or not dev.startswith("/dev/"):
        errors.append(
            "serial_bridge.serial_device must be an absolute path starting with /dev/"
        )
    baud = sb.get("baud_rate")
    if baud not in [9600, 19200, 38400, 57600, 115200, 921600, 1000000]:
        errors.append(f"serial_bridge.baud_rate {baud} is not a supported baud rate")
    if sb.get("controller_only") is not True:
        errors.append(
            "serial_bridge.controller_only must be true in controller-only mode"
        )
    freshness = sb.get("freshness_timeout_sec")
    if not isinstance(freshness, (int, float)) or freshness <= 0:
        errors.append("serial_bridge.freshness_timeout_sec must be positive")
    write_timeout = sb.get("write_timeout_sec")
    if write_timeout is not None:
        if not isinstance(write_timeout, (int, float)) or write_timeout <= 0:
            errors.append("serial_bridge.write_timeout_sec must be positive")
        elif (
            freshness is not None
            and isinstance(freshness, (int, float))
            and write_timeout >= freshness
        ):
            errors.append(
                f"serial_bridge.write_timeout_sec ({write_timeout}) must be strictly less than "
                f"serial_bridge.freshness_timeout_sec ({freshness})"
            )

    # Motor guard validation
    mg = cfg["motor_guard"]
    if not isinstance(mg.get("max_rps"), (int, float)) or mg["max_rps"] <= 0:
        errors.append("motor_guard.max_rps must be positive")
    timeout = mg.get("timeout_sec")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        errors.append("motor_guard.timeout_sec must be positive")

    # Supervisor validation
    sv = cfg["supervisor"]
    for k in ["guard_deadline_sec", "bridge_deadline_sec"]:
        if not isinstance(sv.get(k), (int, float)) or sv[k] <= 0:
            errors.append(f"supervisor.{k} must be positive")

    # Teleop validation
    tp = cfg["teleop"]
    for k in ["linear_speed", "angular_speed"]:
        if not isinstance(tp.get(k), (int, float)) or tp[k] <= 0:
            errors.append(f"teleop.{k} must be positive")
    lease = tp.get("lease_duration_sec")
    if not isinstance(lease, (int, float)) or lease <= 0:
        errors.append("teleop.lease_duration_sec must be positive")
    elif timeout and lease >= timeout:
        errors.append(
            f"teleop.lease_duration_sec ({lease}) must be strictly less than motor_guard.timeout_sec ({timeout})"
        )

    return len(errors) == 0, errors


def migrate_config(current_cfg: Dict[str, Any], target_version: str) -> Dict[str, Any]:
    """
    Forward migrate configuration to target_version while strictly preserving user calibrations.
    """
    ok, errs = validate_config(current_cfg)
    if not ok:
        raise ValueError(
            f"Cannot migrate invalid source configuration: {'; '.join(errs)}"
        )

    if target_version not in SCHEMA_VERSIONS:
        raise ValueError(
            f"Unknown target schema version: '{target_version}'. Supported: {SCHEMA_VERSIONS}"
        )

    migrated = copy.deepcopy(current_cfg)

    if target_version == "1.1":
        # Add v1.1 fields with defaults if not present
        if "watchdog_ping_sec" not in migrated.get("supervisor", {}):
            migrated.setdefault("supervisor", {})["watchdog_ping_sec"] = 0.500
        if "telemetry_rate_hz" not in migrated.get("serial_bridge", {}):
            migrated.setdefault("serial_bridge", {})["telemetry_rate_hz"] = 50.0

    ok_migrated, errs_migrated = validate_config(migrated, version=target_version)
    if not ok_migrated:
        raise RuntimeError(
            f"Migrated configuration failed validation: {'; '.join(errs_migrated)}"
        )

    return migrated


def downgrade_config(
    current_cfg: Dict[str, Any], target_version: str
) -> Dict[str, Any]:
    """
    Downgrade configuration to target_version while preserving user calibrations.
    """
    if target_version not in SCHEMA_VERSIONS:
        raise ValueError(
            f"Unknown target schema version: '{target_version}'. Supported: {SCHEMA_VERSIONS}"
        )

    downgraded = copy.deepcopy(current_cfg)

    if target_version == "1.0":
        # Remove fields introduced in v1.1+
        if (
            "supervisor" in downgraded
            and "watchdog_ping_sec" in downgraded["supervisor"]
        ):
            del downgraded["supervisor"]["watchdog_ping_sec"]
        if (
            "serial_bridge" in downgraded
            and "telemetry_rate_hz" in downgraded["serial_bridge"]
        ):
            del downgraded["serial_bridge"]["telemetry_rate_hz"]

    ok, errs = validate_config(downgraded, version=target_version)
    if not ok:
        raise RuntimeError(
            f"Downgraded configuration failed validation: {'; '.join(errs)}"
        )

    return downgraded


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(path: str, data: Dict[str, Any]):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    pdir = os.path.dirname(os.path.abspath(path))
    dfd = os.open(pdir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def main():
    parser = argparse.ArgumentParser(description="Ubuntu Tank Config Migration Tool")
    sub = parser.add_subparsers(dest="command")

    teleop_p = sub.add_parser(
        "teleop-args",
        help="Print validated host keyboard settings as ROS parameter arguments",
    )
    teleop_p.add_argument("file", help="Host controller.yaml path")

    val_p = sub.add_parser("validate", help="Validate a controller.yaml file")
    val_p.add_argument("file", help="Path to YAML configuration")
    val_p.add_argument(
        "--version", default="1.0", help="Schema version to check against"
    )

    mig_p = sub.add_parser("migrate", help="Forward-migrate a controller.yaml file")
    mig_p.add_argument("file", help="Path to YAML configuration")
    mig_p.add_argument("--target", default="1.1", help="Target schema version")
    mig_p.add_argument(
        "--inplace", action="store_true", help="Overwrite file in-place with fsync"
    )

    down_p = sub.add_parser("downgrade", help="Downgrade a controller.yaml file")
    down_p.add_argument("file", help="Path to YAML configuration")
    down_p.add_argument("--target", default="1.0", help="Target schema version")
    down_p.add_argument(
        "--inplace", action="store_true", help="Overwrite file in-place with fsync"
    )

    args = parser.parse_args()

    if args.command == "teleop-args":
        print("\n".join(teleop_arguments(args.file)))
        return

    if args.command == "validate":
        cfg = load_yaml(args.file)
        ok, errs = validate_config(cfg, version=args.version)
        if ok:
            print(f"PASS: '{args.file}' is valid for schema version {args.version}.")
            sys.exit(0)
        else:
            print(f"FAIL: '{args.file}' has validation errors:", file=sys.stderr)
            for e in errs:
                print(f"  - {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "migrate":
        cfg = load_yaml(args.file)
        res = migrate_config(cfg, target_version=args.target)
        if args.inplace:
            save_yaml(args.file, res)
            print(f"Migrated '{args.file}' in-place to schema {args.target}.")
        else:
            print(yaml.dump(res, sort_keys=False))
        sys.exit(0)

    elif args.command == "downgrade":
        cfg = load_yaml(args.file)
        res = downgrade_config(cfg, target_version=args.target)
        if args.inplace:
            save_yaml(args.file, res)
            print(f"Downgraded '{args.file}' in-place to schema {args.target}.")
        else:
            print(yaml.dump(res, sort_keys=False))
        sys.exit(0)

    else:
        parser.print_help()
        sys.exit(1)


def teleop_arguments(config_path: str) -> List[str]:
    """Return ROS parameter arguments from validated host configuration.

    Clamp requested teleop speeds to controller caps. Missing/invalid config
    raises rather than silently reverting to faster keyboard defaults.
    """
    with open(config_path, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    valid, errors = validate_config(config)
    if not valid:
        raise ValueError("Invalid teleop configuration: " + "; ".join(errors))
    teleop, controller = config["teleop"], config["controller"]
    values = {
        "linear_vel": min(teleop["linear_speed"], controller["max_linear_speed"]),
        "angular_vel": min(teleop["angular_speed"], controller["max_angular_speed"]),
        "lease_duration_sec": teleop["lease_duration_sec"],
    }
    if any(not math.isfinite(value) or value <= 0 for value in values.values()):
        raise ValueError("Teleop settings must be finite positive values")
    if values["lease_duration_sec"] >= 0.250:
        raise ValueError("Teleop lease must be shorter than 250 ms")
    return [
        argument
        for name, value in values.items()
        for argument in ("-p", f"{name}:={value}")
    ]


if __name__ == "__main__":
    main()
