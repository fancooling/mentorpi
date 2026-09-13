#!/usr/bin/env python3
"""Bootstrap an offline disposable build root from a prepared native Ubuntu Pi.

Copy installed system tools and ROS into workspace/.work without copying home
folders, robot releases, credentials or devices. Validate the target platform
and locked package versions first. No apt or hardware operation is performed.
The resulting root is consumed by build_disposable_root.sh via nspawn/chroot.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile

import yaml


def verify_host(lock_path: Path) -> dict:
    """Return locked package versions after checking this native build host.

    Raise RuntimeError for unsupported hosts, missing ROS or package drift.
    """
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    if (
        release.get("ID", "").strip('"') != "ubuntu"
        or release.get("VERSION_ID", "").strip('"') != "26.04"
        or platform.machine() not in ("aarch64", "arm64")
    ):
        raise RuntimeError(
            "Bootstrap requires prepared Ubuntu 26.04 ARM64 with ROS 2 Lyrical"
        )
    if not Path("/opt/ros/lyrical/setup.bash").is_file():
        raise RuntimeError("Run install-ros and install-deps on the native host first")
    lock = yaml.safe_load(lock_path.read_text())
    expected = {package["name"]: package["version"] for package in lock["packages"]}
    result = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n"],
        text=True,
    )
    actual = {
        parts[0]: parts[1]
        for line in result.splitlines()
        if len(parts := line.split("\t")) == 3 and parts[2] == "installed"
    }
    mismatch = [
        name for name, version in expected.items() if actual.get(name) != version
    ]
    if mismatch:
        raise RuntimeError(
            "Build host differs from versions.lock: " + ", ".join(mismatch)
        )
    return expected


def bootstrap(workspace: Path, destination: Path, dry_run: bool = False) -> None:
    """Publish a new root atomically below the workspace's disposable directory.

    Existing roots are never overwritten. Requires root for faithful ownership
    and mode preservation. Dry-run prints prerequisites without accessing host
    packages or mutating directories.
    """
    workspace, destination = workspace.resolve(), destination.resolve()
    work = workspace / ".work"
    if not destination.is_relative_to(work) or destination == work:
        raise ValueError(
            "Bootstrap destination must be strictly inside workspace/.work"
        )
    if dry_run:
        print(
            f"Bootstrap {destination} from prepared Ubuntu 26.04 arm64 host; verify versions.lock, copy system tools and /opt/ros, then build at the production prefix"
        )
        return
    if os.geteuid() != 0:
        raise PermissionError(
            "Bootstrap requires root; run the production builder with sudo"
        )
    if destination.exists():
        raise FileExistsError(
            f"Root already exists: {destination}; select a new --rootfs directory"
        )
    packages = verify_host(workspace / "versions.lock")
    work.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".bootstrap-", dir=work))
    try:
        # Archive only system/runtime trees, preserving merged-/usr symlinks.
        paths = [
            name
            for name in ("usr", "bin", "sbin", "lib", "lib64", "opt/ros")
            if Path("/" + name).exists()
        ]
        producer = subprocess.Popen(
            ["tar", "-C", "/", "--exclude=usr/local", "-cf", "-", *paths],
            stdout=subprocess.PIPE,
        )
        try:
            subprocess.run(
                ["tar", "-C", str(temporary), "-xf", "-"],
                stdin=producer.stdout,
                check=True,
            )
        finally:
            producer.stdout.close()
            status = producer.wait()
        if status:
            raise RuntimeError("System runtime copy failed")
        for name in (
            "etc",
            "dev",
            "proc",
            "sys",
            "run",
            "tmp",
            "var/tmp",
            "var/lib/dpkg",
            "root",
            "usr/local",
        ):
            (temporary / name).mkdir(parents=True, exist_ok=True)
        for name, minor in (("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)):
            os.mknod(
                temporary / "dev" / name, stat.S_IFCHR | 0o666, os.makedev(1, minor)
            )
        (temporary / "dev/fd").symlink_to("/proc/self/fd")
        (temporary / "dev/shm").mkdir(mode=0o1777)
        for name in (
            "os-release",
            "alternatives",
            "ld.so.cache",
            "ld.so.conf",
            "ld.so.conf.d",
            "nsswitch.conf",
            "localtime",
            "timezone",
            "locale.alias",
            "default/locale",
            "python3",
            "python3.14",
        ):
            source, target = Path("/etc") / name, temporary / "etc" / name
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                else:
                    shutil.copy2(source, target)
        (temporary / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
        (temporary / "etc/group").write_text("root:x:0:\n")
        (temporary / "etc/machine-id").touch()
        shutil.copy2("/var/lib/dpkg/status", temporary / "var/lib/dpkg/status")
        (temporary / "var/lib/dpkg/arch").write_text("arm64\n")
        os.chmod(temporary / "tmp", 0o1777)
        os.chmod(temporary / "var/tmp", 0o1777)
        os.chmod(temporary, 0o755)
        (temporary / ".ubuntu-tank-build-root.json").write_text(
            json.dumps(
                {
                    "lock_sha256": hashlib.sha256(
                        (workspace / "versions.lock").read_bytes()
                    ).hexdigest(),
                    "packages": packages,
                },
                sort_keys=True,
            )
        )
        if verify_host(workspace / "versions.lock") != packages:
            raise RuntimeError("Host package state changed during bootstrap")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> None:
    """Parse the workspace/root destination and bootstrap without changing host packages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--rootfs", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    bootstrap(
        args.workspace,
        args.rootfs or args.workspace / ".work/native-rootfs",
        args.dry_run,
    )


if __name__ == "__main__":
    main()
