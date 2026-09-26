#!/usr/bin/env python3
"""Validate runtime mounts and hold the hardware-owner lock across Supervisor exec.

Host provisioning and checks for legacy device holders belong to C4. This local
lock prevents cooperating runtimes from starting together, including stopped
controllers. It never opens the serial device or starts/arms the controller.
"""

import fcntl
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, "/opt/ubuntu_tank/current/scripts")


def main() -> None:
    """Fail closed on invalid mounts/configuration, then exec stopped supervision."""
    import yaml
    from config_migration import validate_config
    from fastdds_setup import apply_loopback_env

    if os.getuid() != 10001:
        raise RuntimeError("Runtime must use application UID 10001")
    for directory in ("/run/ubuntu_tank", "/run/ubuntu_tank-private"):
        info = os.stat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError(f"{directory} must be application-owned mode 0700")
    with open(os.environ["UBUNTU_TANK_CONFIG"]) as stream:
        valid, errors = validate_config(yaml.safe_load(stream))
    if not valid:
        raise ValueError("; ".join(errors))
    apply_loopback_env()
    keystore = Path(os.environ["ROS_SECURITY_KEYSTORE"])
    for enclave in ("operator", "controller", "guard", "bridge"):
        if not (keystore / "enclaves" / "ubuntu_tank" / enclave).is_dir():
            raise RuntimeError(f"Missing SROS2 enclave: {enclave}")
    if not stat.S_ISCHR(os.stat("/dev/rrc").st_mode):
        raise RuntimeError("/dev/rrc must be the mapped serial device")
    fd = os.open("/run/ubuntu_tank-owner/owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    os.umask(0o077)
    os.execv(
        "/usr/bin/supervisord",
        ["supervisord", "-c", "/opt/ubuntu_tank/container/supervisord.conf"],
    )


if __name__ == "__main__":
    main()
