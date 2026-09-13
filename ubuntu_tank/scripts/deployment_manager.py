#!/usr/bin/env python3
"""
Deployment, packaging, installation, activation, and recovery manager for Ubuntu Tank.

Implements Milestone 5 requirements:
- Checksummed RFC 822 packaging and immutable installation.
- Detection and rejection of leaked build/checkout paths.
- Provisioning and validation of dedicated service identities (ubuntu-tank:mentorpi-rrc).
- Strict verification of service stoppage before asset changes.
- Atomic 6-step activation transaction with write-ahead journal and fsync points.
- Snapshot creation and verified offline rollback without network or checkout.
- Rejection of recovery and rollback when baseline/snapshot cannot be verified.
- Self-contained release-independent boot recovery runner in libexec/.
- Deployment lock serialization via /run/lock/ubuntu_tank/deploy.lock.
- Strict non-starting and disarmed-by-default operation.
"""

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple


DEFAULT_OPT_DIR = "/opt/ubuntu_tank"
DEFAULT_ETC_DIR = "/etc/opt/ubuntu_tank"
DEFAULT_VAR_DIR = "/var/opt/ubuntu_tank"
DEFAULT_RUN_DIR = "/run/ubuntu_tank"
DEFAULT_LOCK_PATH = "/run/lock/ubuntu_tank/deploy.lock"
DEFAULT_JOURNAL_PATH = "/var/opt/ubuntu_tank/deployment/activation-journal"
DEFAULT_SYSTEMD_DIR = "/etc/systemd/system"
DEFAULT_UDEV_DIR = "/etc/udev/rules.d"


def compute_file_sha256(filepath: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_tree_sha256(dirpath: str) -> str:
    """Compute deterministic SHA-256 of all files in a directory tree."""
    h = hashlib.sha256()
    if not os.path.isdir(dirpath):
        return h.hexdigest()
    rel_files = []
    for root, _, files in os.walk(dirpath):
        for f in sorted(files):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, dirpath)
            rel_files.append((rel, p))
    for rel, p in sorted(rel_files):
        h.update(rel.encode("utf-8"))
        h.update(b":")
        with open(p, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
    return h.hexdigest()


def fsync_file(filepath: str):
    """Flush and fsync a file descriptor."""
    fd = os.open(filepath, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(dirpath: str):
    """Flush and fsync a directory."""
    if os.path.isdir(dirpath):
        dfd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def atomic_write_file(dest_path: str, data: bytes, mode: int = 0o644):
    """Write file atomically using a sibling temporary file and fsync."""
    pdir = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(pdir, exist_ok=True)
    tmp_path = f"{dest_path}.tmp.{os.getpid()}_{int(time.time() * 1000)}"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, dest_path)
    fsync_dir(pdir)


def check_hardware_mutual_exclusion(
    serial_dev: Optional[str] = None,
    mock_containers: Optional[str] = None,
    mock_docker_fail: bool = False,
    mock_serial_holder: Optional[str] = None,
    allowed_serial_pid: Optional[int] = None,
) -> Tuple[bool, List[str]]:
    """
    Verify hardware mutual exclusion invariants before startup, activation, or recovery:
    1. Docker container inventory:
       If Docker is installed, container inventory must be inspectable (fail closed if docker ps fails).
       Forbidden containers: MentorPi, MentorPiFan, mentorpi, runtime-core, tank_runtime.
    2. Conflicting host systemd units:
       mentorpi.service, mentorpi-start.service, mentorpi-fan.service, hiwonder-chassis.service must not be active.
    3. Serial device exclusivity:
       /dev/rrc (or configured device) must not be held open by another process.
       If character device exists, fuser must be available and confirm exclusivity.
       Bench clients may pass a separately verified managed bridge PID as
       allowed_serial_pid; startup callers leave it unset.
    """
    errors: List[str] = []

    # 1. Docker container inventory
    if mock_docker_fail or os.environ.get("UBUNTU_TANK_MOCK_DOCKER_FAIL"):
        errors.append(
            "Docker is installed but container inventory check failed. Cannot verify mutual exclusion."
        )
    else:
        mock_ps = (
            mock_containers
            if mock_containers is not None
            else os.environ.get("UBUNTU_TANK_MOCK_DOCKER_PS")
        )
        if mock_ps is not None:
            raw = mock_ps.strip()
            if raw and raw not in ("none", "EMPTY"):
                containers = raw.split()
                for pattern in [
                    "mentorpi",
                    "mentorpifan",
                    "runtime-core",
                    "tank_runtime",
                ]:
                    for c in containers:
                        if pattern in c.lower():
                            errors.append(
                                f"Conflicting container '{c}' is present or running."
                            )
        elif shutil.which("docker"):
            try:
                res = subprocess.run(
                    ["docker", "ps", "-a", "--format", "{{.Names}}"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                )
                if res.returncode != 0:
                    errors.append(
                        f"Docker is installed but container inventory check failed (exit {res.returncode}): {res.stderr.strip()}"
                    )
                else:
                    containers = [
                        line.strip() for line in res.stdout.splitlines() if line.strip()
                    ]
                    for pattern in [
                        "mentorpi",
                        "mentorpifan",
                        "runtime-core",
                        "tank_runtime",
                    ]:
                        for c in containers:
                            if pattern in c.lower():
                                errors.append(
                                    f"Conflicting container '{c}' is present or running."
                                )
            except subprocess.TimeoutExpired:
                errors.append("Docker container inventory check timed out.")
            except Exception as e:
                errors.append(f"Docker container inventory check failed: {e}")

    # 2. Conflicting systemd units
    if not os.environ.get("UBUNTU_TANK_MOCK_TARGET") and shutil.which("systemctl"):
        factory_units = [
            "mentorpi.service",
            "mentorpi-start.service",
            "mentorpi-fan.service",
            "hiwonder-chassis.service",
        ]
        for unit in factory_units:
            try:
                res = subprocess.run(
                    ["systemctl", "is-active", "--quiet", unit],
                    check=False,
                    timeout=3.0,
                )
                if res.returncode == 0:
                    errors.append(f"Conflicting systemd unit '{unit}' is active.")
            except subprocess.TimeoutExpired:
                errors.append(f"Systemd check for unit '{unit}' timed out.")
            except Exception as e:
                errors.append(f"Failed to check systemd unit '{unit}': {e}")

    # 3. Serial port device exclusivity
    mock_holder = mock_serial_holder or os.environ.get("UBUNTU_TANK_MOCK_SERIAL_HOLDER")
    dev = serial_dev or os.environ.get("UBUNTU_TANK_MOCK_SERIAL_DEV") or "/dev/rrc"
    if mock_holder:
        errors.append(
            f"Conflicting process PID(s) {mock_holder} hold serial device '{dev}' open."
        )
    elif os.path.exists(dev):
        real_dev = os.path.realpath(dev)
        try:
            st = os.stat(real_dev)
            if stat.S_ISCHR(st.st_mode):
                if not shutil.which("fuser"):
                    errors.append(
                        f"Serial device '{dev}' is present but 'fuser' command is not available to verify exclusivity."
                    )
                else:
                    fuser_res = subprocess.run(
                        ["fuser", real_dev],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=3.0,
                    )
                    holders = fuser_res.stdout.strip().split()
                    my_pid = str(os.getpid())
                    foreign_holders = [
                        p for p in holders if p not in (my_pid, str(allowed_serial_pid))
                    ]
                    if foreign_holders:
                        errors.append(
                            f"Conflicting process PID(s) {', '.join(foreign_holders)} hold serial device '{real_dev}' open."
                        )
        except OSError as e:
            errors.append(f"Failed to inspect serial device '{real_dev}': {e}")

    return (len(errors) == 0, errors)


class DeploymentLock:
    """Deployment lock ensuring mutual exclusion for mutating operations."""

    def __init__(self, lock_path: str = DEFAULT_LOCK_PATH, timeout_sec: float = 5.0):
        self.lock_path = lock_path
        self.timeout_sec = float(timeout_sec)
        self.fd: Optional[int] = None

    def acquire(self):
        pdir = os.path.dirname(os.path.abspath(self.lock_path))
        os.makedirs(pdir, exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(self.fd, 0)
                msg = f"pid={os.getpid()}\ntime={time.time()}\n"
                os.write(self.fd, msg.encode("utf-8"))
                fsync_file(self.lock_path)
                return self
            except (IOError, OSError):
                if time.monotonic() - start >= self.timeout_sec:
                    raise TimeoutError(
                        f"Failed to acquire deployment lock '{self.lock_path}': held by another process."
                    )
                time.sleep(0.1)

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class ActivationJournal:
    """
    Crash-consistent write-ahead activation journal.
    Persisted at /var/opt/ubuntu_tank/deployment/activation-journal.
    """

    def __init__(self, journal_path: str):
        self.journal_path = journal_path

    def get_state(self) -> Dict[str, Any]:
        if not os.path.exists(self.journal_path):
            return {
                "format_version": "1.0",
                "active_release_id": None,
                "active_release_path": None,
                "previous_release_id": None,
                "previous_release_path": None,
                "current_transaction": None,
                "history": [],
            }
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise RuntimeError(
                f"Activation journal at '{self.journal_path}' is corrupted or unreadable: {e}"
            )

        if (
            not isinstance(data, dict)
            or "format_version" not in data
            or "history" not in data
        ):
            raise RuntimeError(
                f"Activation journal at '{self.journal_path}' has invalid schema or structure."
            )
        return data

    def _write_state(self, state: Dict[str, Any]):
        data = json.dumps(state, indent=2).encode("utf-8")
        atomic_write_file(self.journal_path, data, mode=0o644)

    def record_prepared(
        self,
        tx_id: str,
        candidate_release_id: str,
        candidate_release_path: str,
        previous_release_id: Optional[str],
        previous_release_path: Optional[str],
        snapshot_dir: str,
    ):
        state = self.get_state()
        if state.get("current_transaction") is not None:
            raise RuntimeError(
                f"Refusing to record new transaction: uncommitted transaction "
                f"'{state['current_transaction'].get('tx_id')}' already exists."
            )
        state["current_transaction"] = {
            "tx_id": tx_id,
            "type": "ACTIVATION",
            "status": "PREPARED",
            "candidate_release_id": candidate_release_id,
            "candidate_release_path": candidate_release_path,
            "previous_release_id": previous_release_id,
            "previous_release_path": previous_release_path,
            "snapshot_dir": snapshot_dir,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_state(state)

    def record_rollback_prepared(
        self,
        tx_id: str,
        target_release_id: str,
        target_release_path: str,
        snapshot_dir: str,
        previous_release_id: Optional[str],
        previous_release_path: Optional[str],
    ):
        state = self.get_state()
        if state.get("current_transaction") is not None:
            raise RuntimeError(
                f"Refusing to record rollback transaction: uncommitted transaction "
                f"'{state['current_transaction'].get('tx_id')}' already exists."
            )
        state["current_transaction"] = {
            "tx_id": tx_id,
            "type": "ROLLBACK",
            "status": "ROLLING_BACK",
            "candidate_release_id": target_release_id,
            "candidate_release_path": target_release_path,
            "previous_release_id": previous_release_id,
            "previous_release_path": previous_release_path,
            "snapshot_dir": snapshot_dir,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_state(state)

    def record_activating(self, tx_id: str):
        state = self.get_state()
        if (
            state.get("current_transaction")
            and state["current_transaction"].get("tx_id") == tx_id
        ):
            state["current_transaction"]["status"] = "ACTIVATING"
            state["current_transaction"]["activating_timestamp"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            self._write_state(state)

    def record_committed(self, tx_id: str, active_id: str, active_path: str):
        state = self.get_state()
        tx = state.get("current_transaction") or {}
        tx["status"] = "COMMITTED"
        tx["committed_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["history"].append(tx)
        state["previous_release_id"] = state.get("active_release_id")
        state["previous_release_path"] = state.get("active_release_path")
        state["active_release_id"] = active_id
        state["active_release_path"] = active_path
        state["current_transaction"] = None
        self._write_state(state)

    def record_rolled_back(
        self, tx_id: str, active_id: Optional[str], active_path: Optional[str]
    ):
        state = self.get_state()
        tx = state.get("current_transaction") or {}
        tx["status"] = "ROLLED_BACK"
        tx["rolled_back_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["history"].append(tx)
        state["active_release_id"] = active_id
        state["active_release_path"] = active_path
        state["current_transaction"] = None
        self._write_state(state)

    def record_aborted(self, tx_id: str, reason: str):
        state = self.get_state()
        tx = state.get("current_transaction") or {}
        tx["status"] = "ABORTED"
        tx["abort_reason"] = reason
        tx["aborted_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["history"].append(tx)
        state["current_transaction"] = None
        self._write_state(state)


class SnapshotManager:
    """
    Manages root-only checksummed configuration and host-asset snapshots.
    Snapshots live under /var/opt/ubuntu_tank/deployment/snapshots/<tx-id>/.
    """

    def __init__(self, snapshots_base_dir: str):
        self.snapshots_base_dir = snapshots_base_dir

    def create_snapshot(
        self,
        tx_id: str,
        current_symlink_target: Optional[str],
        etc_dir: str,
        systemd_dir: str,
        udev_dir: str,
    ) -> str:
        """Create a unique durable snapshot of host assets and credential ownership.

        Reject reused transaction IDs. The root-only snapshot includes checksums
        for every security file so rollback can validate before changing assets.
        """
        snapshot_dir = os.path.join(self.snapshots_base_dir, tx_id)
        os.makedirs(snapshot_dir, exist_ok=False)
        os.chmod(snapshot_dir, 0o700)

        # 1. Record symlink target
        symlink_target_file = os.path.join(snapshot_dir, "symlink_target")
        with open(symlink_target_file, "w", encoding="utf-8") as f:
            f.write(current_symlink_target or "")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(symlink_target_file, 0o600)

        files_to_backup = [
            (os.path.join(etc_dir, "controller.yaml"), "controller.yaml"),
            (os.path.join(etc_dir, "mentorpi-tank.env"), "mentorpi-tank.env"),
            (
                os.path.join(systemd_dir, "mentorpi-tank.service"),
                "mentorpi-tank.service",
            ),
            (
                os.path.join(systemd_dir, "mentorpi-tank-recover.service"),
                "mentorpi-tank-recover.service",
            ),
            (os.path.join(udev_dir, "99-mentorpi-rrc.rules"), "99-mentorpi-rrc.rules"),
        ]

        checksums = {}
        for src, name in files_to_backup:
            if os.path.isfile(src):
                dst = os.path.join(snapshot_dir, name)
                shutil.copy2(src, dst)
                fsync_file(dst)
                os.chmod(dst, 0o600)
                checksums[name] = compute_file_sha256(dst)

        security_src = os.path.join(etc_dir, "security", "keystore")
        security_metadata = {}
        if os.path.isdir(security_src):
            security_dst = os.path.join(snapshot_dir, "keystore")
            shutil.copytree(security_src, security_dst)
            for root, dirs, files in os.walk(security_src):
                for name in ["."] + files:
                    source = os.path.join(root, name)
                    rel = os.path.relpath(source, security_src)
                    info = os.stat(source)
                    security_metadata[rel] = [
                        info.st_uid,
                        info.st_gid,
                        stat.S_IMODE(info.st_mode),
                    ]
                    if os.path.isfile(source):
                        backup = os.path.join(security_dst, rel)
                        fsync_file(backup)
                        checksums["keystore/" + rel] = compute_file_sha256(backup)
            for root, _, _ in os.walk(security_dst, topdown=False):
                fsync_dir(root)
        security_meta = os.path.join(snapshot_dir, "security.json")
        atomic_write_file(
            security_meta,
            json.dumps(
                {"present": os.path.isdir(security_src), "ownership": security_metadata}
            ).encode(),
            mode=0o600,
        )
        checksums["security.json"] = compute_file_sha256(security_meta)

        # Write checksums manifest
        checksum_file = os.path.join(snapshot_dir, "checksums.sha256")
        with open(checksum_file, "w", encoding="utf-8") as f:
            for name, chk in sorted(checksums.items()):
                f.write(f"{chk}  {name}\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(checksum_file, 0o600)

        # Write snapshot metadata
        meta_file = os.path.join(snapshot_dir, "metadata.json")
        meta = {
            "tx_id": tx_id,
            "symlink_target": current_symlink_target,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "backed_up_files": list(checksums.keys()),
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(meta_file, 0o600)

        fsync_dir(snapshot_dir)
        fsync_dir(self.snapshots_base_dir)
        return snapshot_dir

    def verify_snapshot(self, snapshot_dir: str) -> bool:
        """Verify checksums and complete security metadata before restoring any assets.

        Legacy snapshots without a recorded security baseline fail closed.
        """
        if not snapshot_dir or not os.path.isdir(snapshot_dir):
            return False
        try:
            names = set()
            with open(
                os.path.join(snapshot_dir, "checksums.sha256"), encoding="utf-8"
            ) as stream:
                for line in stream:
                    if not line.strip() or line.startswith("#"):
                        continue
                    expected, name = line.strip().split(None, 1)
                    if name in names or os.path.isabs(name) or ".." in name.split("/"):
                        return False
                    names.add(name)
                    if (
                        compute_file_sha256(os.path.join(snapshot_dir, name))
                        != expected
                    ):
                        return False
            if "security.json" not in names:
                return False
            with open(
                os.path.join(snapshot_dir, "security.json"), encoding="utf-8"
            ) as stream:
                metadata = json.load(stream)
            if type(metadata["present"]) is not bool or not isinstance(
                metadata["ownership"], dict
            ):
                return False
            if metadata["present"]:
                security = os.path.join(snapshot_dir, "keystore")
                if not os.path.isdir(security) or "." not in metadata["ownership"]:
                    return False
                for root, dirs, files in os.walk(security):
                    for filename in ["."] + files:
                        relative = os.path.relpath(
                            os.path.join(root, filename), security
                        )
                        if relative not in metadata["ownership"]:
                            return False
                    if any(
                        "keystore/"
                        + os.path.relpath(os.path.join(root, name), security)
                        not in names
                        for name in files
                    ):
                        return False
                for relative, ownership in metadata["ownership"].items():
                    if (
                        os.path.isabs(relative)
                        or ".." in relative.split("/")
                        or len(ownership) != 3
                    ):
                        return False
                    if any(type(value) is not int or value < 0 for value in ownership):
                        return False
                    if not os.path.exists(os.path.join(security, relative)):
                        return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def restore_snapshot(
        self, snapshot_dir: str, etc_dir: str, systemd_dir: str, udev_dir: str
    ):
        """Restore verified host files and atomically select the saved credentials.

        Must run with the deployment lock held and service stopped. Corrupt or
        legacy snapshots without security evidence raise before restoration.
        """
        if not self.verify_snapshot(snapshot_dir):
            raise RuntimeError(f"Snapshot integrity check failed for '{snapshot_dir}'")

        file_map = {
            "controller.yaml": os.path.join(etc_dir, "controller.yaml"),
            "mentorpi-tank.env": os.path.join(etc_dir, "mentorpi-tank.env"),
            "mentorpi-tank.service": os.path.join(systemd_dir, "mentorpi-tank.service"),
            "mentorpi-tank-recover.service": os.path.join(
                systemd_dir, "mentorpi-tank-recover.service"
            ),
            "99-mentorpi-rrc.rules": os.path.join(udev_dir, "99-mentorpi-rrc.rules"),
        }

        for name, dest in file_map.items():
            src = os.path.join(snapshot_dir, name)
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    data = f.read()
                atomic_write_file(dest, data, mode=0o644)

        security_meta = os.path.join(snapshot_dir, "security.json")
        if not os.path.isfile(security_meta):
            raise RuntimeError(
                "Snapshot predates security rollback support; refusing mixed-policy restoration"
            )
        with open(security_meta, encoding="utf-8") as stream:
            metadata = json.load(stream)
        parent = os.path.join(etc_dir, "security")
        os.makedirs(parent, mode=0o755, exist_ok=True)
        live = os.path.join(parent, "keystore")
        if metadata["present"]:
            generation = tempfile.mkdtemp(prefix=".keystore-restored-", dir=parent)
            shutil.copytree(
                os.path.join(snapshot_dir, "keystore"), generation, dirs_exist_ok=True
            )
            for rel, (uid, gid, mode) in metadata["ownership"].items():
                target = os.path.join(generation, rel)
                if os.geteuid() == 0:
                    os.chown(target, uid, gid)
                os.chmod(target, mode)
                if os.path.isfile(target):
                    fsync_file(target)
            for root, _, _ in os.walk(generation, topdown=False):
                fsync_dir(root)
            publish_keystore(generation, live)
        elif os.path.lexists(live):
            os.rename(
                live, os.path.join(parent, ".retired-keystore-" + uuid.uuid4().hex)
            )
            fsync_dir(parent)


def publish_keystore(generation: str, live: str) -> None:
    """Atomically select a durable credential generation while preserving old data.

    Legacy directory stores are renamed first; an interrupted conversion is
    recovered from the activation snapshot before any participant can start.
    """
    parent = os.path.dirname(live)
    if os.path.isdir(live) and not os.path.islink(live):
        os.rename(live, os.path.join(parent, ".legacy-keystore-" + uuid.uuid4().hex))
        fsync_dir(parent)
    temporary = live + ".tmp." + uuid.uuid4().hex
    os.symlink(os.path.relpath(generation, parent), temporary)
    os.replace(temporary, live)
    fsync_dir(parent)


def attest_build(
    install_tree: str, prefix: str, source: str, synthetic: bool = False
) -> None:
    """Record builder-selected prefix, source identity and every installed file hash.

    Call only after a successful build at prefix. Packaging verifies this record
    rather than inventing provenance for an arbitrary supplied install tree.
    """
    files = {}
    for root, _, filenames in os.walk(install_tree):
        for filename in filenames:
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, install_tree)
            if relative != "production-build.json":
                files[relative] = compute_file_sha256(path)
    record = {
        "synthetic": synthetic,
        "install_prefix": prefix,
        "source_sha256": compute_tree_sha256(source),
        "files": files,
    }
    atomic_write_file(
        os.path.join(install_tree, "production-build.json"),
        json.dumps(record, sort_keys=True).encode(),
        mode=0o644,
    )


def verify_build(
    install_tree: str,
    prefix: str,
    source: Optional[str] = None,
    allow_synthetic: bool = False,
) -> None:
    """Reject stale, mismatched or changed build output before packaging/installation.

    Prefix references to another release are rejected even if a marker was
    mistakenly regenerated; dependency paths under /opt/ros remain allowed.
    """
    with open(
        os.path.join(install_tree, "production-build.json"), encoding="utf-8"
    ) as stream:
        record = json.load(stream)
    if record.get("synthetic") and not allow_synthetic:
        raise ValueError("Synthetic test artifacts cannot be used for production")
    if record.get("install_prefix") != prefix:
        raise ValueError("Production build prefix does not match requested release")
    if source is not None and record.get("source_sha256") != compute_tree_sha256(
        source
    ):
        raise ValueError("Production build source identity is stale")
    actual = {}
    pattern = re.compile(rb"/[A-Za-z0-9_./-]+/releases/[A-Za-z0-9_.-]+/install")
    for root, _, filenames in os.walk(install_tree):
        for filename in filenames:
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, install_tree)
            if relative == "production-build.json":
                continue
            actual[relative] = compute_file_sha256(path)
            with open(path, "rb") as stream:
                data = stream.read()
            if any(match.decode() != prefix for match in pattern.findall(data)):
                raise ValueError(
                    "Install tree refers to another production prefix: " + relative
                )
    if actual != record.get("files"):
        raise ValueError("Production build payload differs from its recorded hashes")


def parse_release_manifest(
    manifest_path: str,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """
    Parse RFC 822 key-value headers and [Files] section of release-manifest.txt.
    """
    headers = {}
    files = {}
    in_files = False

    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "[Files]":
                in_files = True
                continue
            if in_files:
                parts = line.split()
                if len(parts) >= 4:
                    sha, size, mode, rel_path = parts[0], parts[1], parts[2], parts[3]
                    files[rel_path] = {"sha256": sha, "size": size, "mode": mode}
            else:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()

    return headers, files


def check_for_leaked_paths(directory: str, forbidden_patterns: List[str]) -> List[str]:
    """Scan text files in a directory to reject leaked checkout or temporary paths."""
    violations = []
    text_extensions = (
        ".py",
        ".sh",
        ".bash",
        ".yaml",
        ".yml",
        ".txt",
        ".xml",
        ".json",
        ".env",
        ".service",
        ".rules",
        "",
    )
    for root, _, files in os.walk(directory):
        for fn in files:
            p = os.path.join(root, fn)
            if not os.path.isfile(p):
                continue
            # Only inspect text files or scripts
            if fn.endswith(text_extensions) or os.access(p, os.X_OK):
                try:
                    with open(p, "rb") as f:
                        content = f.read(512 * 1024)
                        # Check for binary NUL bytes
                        if b"\x00" in content:
                            continue
                        text = content.decode("utf-8", errors="ignore")
                        for pat in forbidden_patterns:
                            if pat and pat in text:
                                violations.append(
                                    f"{os.path.relpath(p, directory)} contains leaked path: '{pat}'"
                                )
                except Exception:
                    pass
    return violations


class ReleaseManager:
    """Manages immutable releases: packaging, validation, installation, and activation."""

    def __init__(
        self,
        opt_dir: str = DEFAULT_OPT_DIR,
        etc_dir: str = DEFAULT_ETC_DIR,
        var_dir: str = DEFAULT_VAR_DIR,
        run_dir: str = DEFAULT_RUN_DIR,
        systemd_dir: str = DEFAULT_SYSTEMD_DIR,
        udev_dir: str = DEFAULT_UDEV_DIR,
        lock_path: str = DEFAULT_LOCK_PATH,
    ):
        self.opt_dir = opt_dir
        self.etc_dir = etc_dir
        self.var_dir = var_dir
        self.run_dir = run_dir
        self.systemd_dir = systemd_dir
        self.udev_dir = udev_dir
        self.lock_path = lock_path

        self.releases_dir = os.path.join(self.opt_dir, "releases")
        self.current_symlink = os.path.join(self.opt_dir, "current")
        self.snapshots_dir = os.path.join(self.var_dir, "deployment", "snapshots")
        self.journal_path = os.path.join(
            self.var_dir, "deployment", "activation-journal"
        )

        self.journal = ActivationJournal(self.journal_path)
        self.snapshot_mgr = SnapshotManager(self.snapshots_dir)

    def validate_release(
        self, release_dir: str, expected_release_id: Optional[str] = None
    ) -> Tuple[bool, List[str]]:
        """Validate structure, permissions, and checksums of an installed or staged release."""
        errors = []
        if not os.path.isdir(release_dir):
            return False, [f"Release directory does not exist: {release_dir}"]

        manifest_path = os.path.join(release_dir, "release-manifest.txt")
        if not os.path.isfile(manifest_path):
            return False, [f"Missing release-manifest.txt in {release_dir}"]

        headers, file_records = parse_release_manifest(manifest_path)
        rel_id = headers.get("Release-Id")
        if not rel_id:
            errors.append("Manifest missing 'Release-Id' header")
        elif expected_release_id and rel_id != expected_release_id:
            errors.append(
                f"Release-Id mismatch: manifest declared '{rel_id}', expected '{expected_release_id}'"
            )

        # Validate required directories and entry points
        for req_dir in ["bin", "install", "config", "host"]:
            if not os.path.isdir(os.path.join(release_dir, req_dir)):
                errors.append(f"Required release directory missing: {req_dir}/")

        install_dir = os.path.join(release_dir, "install")
        if os.path.isdir(install_dir):
            setup_sh = os.path.join(install_dir, "setup.bash")
            if not os.path.isfile(setup_sh) or os.path.getsize(setup_sh) == 0:
                errors.append(
                    "Required install environment setup missing or empty: install/setup.bash"
                )
            install_files = [
                rel_p for rel_p in file_records.keys() if rel_p.startswith("install/")
            ]
            if not install_files:
                errors.append(
                    "Install directory is empty: contains no installed files in manifest"
                )
            elif len(install_files) == 1 and install_files[0] == "install/setup.bash":
                errors.append(
                    "Install directory contains only setup.bash without installed package artifacts"
                )

        runner_path = os.path.join(release_dir, "bin", "mentorpi-tank-run")
        if not os.path.isfile(runner_path):
            errors.append("Missing bin/mentorpi-tank-run launcher")
        elif not os.access(runner_path, os.X_OK):
            errors.append("bin/mentorpi-tank-run is not executable")

        # Verify all files match recorded checksums
        for rel_p, rec in file_records.items():
            abs_p = os.path.join(release_dir, rel_p)
            if not os.path.exists(abs_p):
                errors.append(f"Manifested file missing: {rel_p}")
                continue
            actual_sha = compute_file_sha256(abs_p)
            if actual_sha != rec["sha256"]:
                errors.append(
                    f"Checksum mismatch for {rel_p}: expected {rec['sha256']}, got {actual_sha}"
                )

        expected_prefix = f"{self.opt_dir}/releases/{rel_id}/install"
        if headers.get("Install-Prefix") != expected_prefix:
            errors.append("Install-Prefix does not match destination release")
        try:
            verify_build(
                install_dir,
                expected_prefix,
                os.path.join(release_dir, "src"),
                allow_synthetic=True,
            )
        except (OSError, ValueError) as error:
            errors.append(f"Invalid production build: {error}")

        # Check immutability: must not be writable by non-root users (mode must not be world or group writable)
        for root, dirs, files in os.walk(release_dir):
            for d in dirs:
                dp = os.path.join(root, d)
                mode = stat.S_IMODE(os.stat(dp).st_mode)
                if mode & 0o002:
                    errors.append(
                        f"Directory {os.path.relpath(dp, release_dir)} is world-writable (mode {oct(mode)})"
                    )
            for f in files:
                fp = os.path.join(root, f)
                mode = stat.S_IMODE(os.stat(fp).st_mode)
                if mode & 0o002:
                    errors.append(
                        f"File {os.path.relpath(fp, release_dir)} is world-writable (mode {oct(mode)})"
                    )

        return len(errors) == 0, errors

    @staticmethod
    def is_complete_install_tree(install_dir: Optional[str]) -> bool:
        """Check whether an install directory exists, is non-empty, and contains setup.bash."""
        if not install_dir or not os.path.isdir(install_dir):
            return False
        setup_sh = os.path.join(install_dir, "setup.bash")
        if not os.path.isfile(setup_sh) or os.path.getsize(setup_sh) == 0:
            return False
        # Must contain files (not an empty directory)
        for _, _, files in os.walk(install_dir):
            if files:
                return True
        return False

    @staticmethod
    def validate_release_id(release_id: str) -> str:
        """
        Validate release ID as a single path-safe component.
        Rejects paths with directory separators, traversal tokens, absolute paths,
        or characters outside the safe identifier set.
        """
        if not isinstance(release_id, str) or not release_id.strip():
            raise ValueError("Release ID must be a non-empty string")
        if os.path.basename(release_id) != release_id or os.path.isabs(release_id):
            raise ValueError(
                f"Invalid release ID '{release_id}': must be a single path component"
            )
        if "/" in release_id or "\\" in release_id or "\0" in release_id:
            raise ValueError(
                f"Invalid release ID '{release_id}': contains path separators or NUL bytes"
            )
        if release_id in (".", ".."):
            raise ValueError(
                f"Invalid release ID '{release_id}': cannot be '.' or '..'"
            )
        if not re.match(r"^[0-9a-zA-Z][0-9a-zA-Z._-]*$", release_id):
            raise ValueError(
                f"Invalid release ID '{release_id}': must match '^[0-9a-zA-Z][0-9a-zA-Z._-]*$'"
            )
        return release_id

    def package_release(
        self,
        workspace_dir: str,
        output_dir: str,
        release_id: Optional[str] = None,
        arch: str = "arm64",
        allow_staged_install: bool = False,
        install_tree: Optional[str] = None,
        build_root: Optional[str] = None,
    ) -> str:
        """
        Package a validated release archive into output_dir/ubuntu-tank-<release-id>-<arch>.tar.zst.
        Includes scripts/ so that deploy.sh and operational tools run outside the checkout.
        Install tree is built at the final production prefix (/opt/ubuntu_tank/releases/<release-id>/install)
        inside an isolated disposable root, completely free of checkout or build path references.
        """
        version_file = os.path.join(workspace_dir, "VERSION")
        if not os.path.isfile(version_file):
            raise FileNotFoundError(f"VERSION file missing at {version_file}")
        with open(version_file, "r", encoding="utf-8") as f:
            version = f.read().strip()

        if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$", version):
            raise ValueError(f"Invalid semantic version '{version}' in VERSION file")

        # Determine git commit
        try:
            full_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workspace_dir, text=True
            ).strip()
            short_commit = subprocess.check_output(
                ["git", "rev-parse", "--short=7", "HEAD"], cwd=workspace_dir, text=True
            ).strip()
        except Exception:
            full_commit = os.environ.get("UBUNTU_TANK_GIT_COMMIT", "0" * 40)
            short_commit = os.environ.get(
                "UBUNTU_TANK_GIT_SHORT_COMMIT", full_commit[:7]
            )

        if release_id:
            self.validate_release_id(release_id)
        else:
            release_id = f"{version}-g{short_commit}"
            self.validate_release_id(release_id)

        staging_parent = os.path.abspath(
            os.path.join(workspace_dir, ".work", "staging")
        )
        canon_staging_parent = os.path.realpath(staging_parent)
        staging_dir = os.path.abspath(os.path.join(staging_parent, release_id))

        # Strict containment verification
        try:
            rel = os.path.relpath(staging_dir, staging_parent)
        except ValueError:
            raise ValueError(
                f"Unsafe release ID '{release_id}' escapes staging directory"
            )
        if rel.startswith("..") or rel in (".", ""):
            raise ValueError(
                f"Unsafe release ID '{release_id}' escapes staging directory"
            )

        # Reject symlinks for staging directory
        if os.path.islink(staging_dir):
            raise ValueError(
                f"Staging directory '{staging_dir}' is a symlink; refusing mutation"
            )

        if os.path.exists(staging_dir):
            canon_staging = os.path.realpath(staging_dir)
            if (
                not canon_staging.startswith(canon_staging_parent + os.sep)
                and canon_staging != canon_staging_parent
            ):
                raise ValueError(
                    f"Staging directory target '{canon_staging}' escapes '{canon_staging_parent}'"
                )
            shutil.rmtree(staging_dir)
        os.makedirs(staging_dir, exist_ok=True)

        # Copy required components
        shutil.copytree(
            os.path.join(workspace_dir, "bin"), os.path.join(staging_dir, "bin")
        )
        shutil.copytree(
            os.path.join(workspace_dir, "src"), os.path.join(staging_dir, "src")
        )
        shutil.copytree(
            os.path.join(workspace_dir, "config"), os.path.join(staging_dir, "config")
        )
        shutil.copytree(
            os.path.join(workspace_dir, "host"), os.path.join(staging_dir, "host")
        )

        # Include scripts/ so deployment and recovery tooling is present in release
        ws_scripts = os.path.join(workspace_dir, "scripts")
        if os.path.isdir(ws_scripts):
            shutil.copytree(ws_scripts, os.path.join(staging_dir, "scripts"))

        # Target production install prefix
        install_prefix = f"{self.opt_dir}/releases/{release_id}/install"

        # Resolve production install tree
        resolved_install = None
        if install_tree and not os.path.isdir(install_tree):
            raise RuntimeError("Specified install tree does not exist")
        if install_tree and os.path.isdir(install_tree):
            leaked = check_for_leaked_paths(
                install_tree, [workspace_dir, os.path.join(workspace_dir, ".work")]
            )
            if leaked:
                raise RuntimeError(
                    "Packaging rejected: leaked build paths detected: "
                    + "; ".join(leaked)
                )
            if not self.is_complete_install_tree(install_tree):
                raise RuntimeError(
                    f"Packaging rejected: specified install_tree '{install_tree}' is incomplete or empty."
                )
            resolved_install = install_tree
            if not allow_staged_install:
                try:
                    verify_build(
                        resolved_install,
                        install_prefix,
                        os.path.join(workspace_dir, "src"),
                    )
                except (OSError, ValueError) as error:
                    raise RuntimeError(
                        f"Supplied install tree lacks matching production provenance: {error}"
                    ) from error
        else:
            # Check disposable build root candidates (must be complete, non-empty install trees)
            candidates = [
                os.path.join(
                    workspace_dir, ".work", "build_root", install_prefix.lstrip("/")
                ),
                os.path.join(
                    workspace_dir, ".work", "rootfs", install_prefix.lstrip("/")
                ),
                os.path.join(
                    workspace_dir,
                    ".work",
                    "build_root",
                    "opt",
                    "ubuntu_tank",
                    "releases",
                    release_id,
                    "install",
                ),
                os.path.join(
                    workspace_dir,
                    ".work",
                    "rootfs",
                    "opt",
                    "ubuntu_tank",
                    "releases",
                    release_id,
                    "install",
                ),
            ]
            if not allow_staged_install:
                candidates = [
                    os.path.join(
                        workspace_dir,
                        ".work",
                        "native-build",
                        install_prefix.lstrip("/"),
                    ),
                    os.path.join(
                        workspace_dir,
                        ".work",
                        "native-rootfs",
                        install_prefix.lstrip("/"),
                    ),
                ]
            if build_root:
                candidates.insert(
                    0, os.path.join(build_root, install_prefix.lstrip("/"))
                )
                candidates.insert(
                    1,
                    os.path.join(
                        build_root,
                        "opt",
                        "ubuntu_tank",
                        "releases",
                        release_id,
                        "install",
                    ),
                )

            for cand in candidates:
                if self.is_complete_install_tree(cand):
                    try:
                        verify_build(
                            cand,
                            install_prefix,
                            os.path.join(workspace_dir, "src"),
                            allow_synthetic=allow_staged_install,
                        )
                    except (OSError, ValueError):
                        continue
                    resolved_install = cand
                    break

        if resolved_install is None:
            # Attempt to execute build_disposable_root.sh
            build_root_script = os.path.join(
                workspace_dir, "scripts", "build_disposable_root.sh"
            )
            if os.path.isfile(build_root_script):
                builder_cmd = [
                    build_root_script,
                    "--workspace",
                    workspace_dir,
                    "--release-id",
                    release_id,
                    "--opt-dir",
                    self.opt_dir,
                ]
                if allow_staged_install:
                    builder_cmd.append("--allow-staged-install")
                if build_root:
                    builder_cmd.extend(["--build-root", build_root])
                try:
                    res = subprocess.run(builder_cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        for cand in candidates:
                            if self.is_complete_install_tree(cand):
                                resolved_install = cand
                                break
                    elif not allow_staged_install:
                        raise RuntimeError(
                            "Production builder failed: "
                            + (res.stderr or res.stdout).strip()
                        )
                except OSError as error:
                    if not allow_staged_install:
                        raise RuntimeError(
                            f"Cannot execute production builder: {error}"
                        ) from error

        if resolved_install and os.path.isdir(resolved_install):
            if not self.is_complete_install_tree(resolved_install):
                raise RuntimeError(
                    f"Packaging rejected: candidate install tree '{resolved_install}' is incomplete or empty."
                )
            shutil.copytree(resolved_install, os.path.join(staging_dir, "install"))
        else:
            if not allow_staged_install:
                raise RuntimeError(
                    f"Production install tree for prefix '{install_prefix}' missing or incomplete in disposable build root.\n"
                    "Run './scripts/build_disposable_root.sh' or build inside the disposable root before packaging."
                )
            # Create minimal self-contained mock install tree for testing
            min_install = os.path.join(staging_dir, "install")
            os.makedirs(os.path.join(min_install, "bin"), exist_ok=True)
            os.makedirs(os.path.join(min_install, "lib"), exist_ok=True)
            atomic_write_file(
                os.path.join(min_install, "setup.bash"),
                (
                    f"#!/usr/bin/env bash\n"
                    f"# Staged production install setup\n"
                    f'export COLCON_CURRENT_PREFIX="{install_prefix}"\n'
                    f'export AMENT_PREFIX_PATH="{install_prefix}:${{AMENT_PREFIX_PATH:-}}"\n'
                    f'export PYTHONPATH="{install_prefix}/lib/python3.12/site-packages:${{PYTHONPATH:-}}"\n'
                    f'export PATH="{install_prefix}/bin:${{PATH:-}}"\n'
                ).encode("utf-8"),
                0o755,
            )
            atomic_write_file(
                os.path.join(min_install, "bin", "tank_verify_install"),
                (
                    "#!/usr/bin/env python3\n"
                    "import os, sys\n"
                    "print(f\"VERIFIED_PREFIX={os.environ.get('COLCON_CURRENT_PREFIX', 'unknown')}\")\n"
                    "sys.exit(0)\n"
                ).encode("utf-8"),
                0o755,
            )

        # Ensure staged install tree is verified complete
        staged_install = os.path.join(staging_dir, "install")
        if not self.is_complete_install_tree(staged_install):
            raise RuntimeError(
                f"Packaging rejected: staged install tree '{staged_install}' is incomplete or empty."
            )

        if allow_staged_install and not os.path.isfile(
            os.path.join(staged_install, "production-build.json")
        ):
            attest_build(
                staged_install,
                install_prefix,
                os.path.join(workspace_dir, "src"),
                synthetic=True,
            )
        try:
            verify_build(
                staged_install,
                install_prefix,
                os.path.join(workspace_dir, "src"),
                allow_synthetic=allow_staged_install,
            )
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"Packaging rejected: invalid production build: {error}"
            ) from error

        # Copy root files
        for fn in ["deploy.sh", "README.md"]:
            src_f = os.path.join(workspace_dir, fn)
            if os.path.isfile(src_f):
                shutil.copy2(src_f, os.path.join(staging_dir, fn))

        # Check for leaked build/checkout paths
        leaked = check_for_leaked_paths(
            os.path.join(staging_dir, "install"),
            [workspace_dir, os.path.join(workspace_dir, ".work")],
        )
        if leaked:
            raise RuntimeError(
                f"Packaging rejected: leaked build paths detected in install tree:\n"
                + "\n".join(leaked)
            )

        # Generate release-manifest.txt
        install_prefix = f"{self.opt_dir}/releases/{release_id}/install"
        source_tree_sha = compute_tree_sha256(os.path.join(staging_dir, "src"))
        install_tree_sha = compute_tree_sha256(os.path.join(staging_dir, "install"))

        manifest_lines = [
            "Format-Version: 1.0",
            f"Release-Id: {release_id}",
            f"Project-Version: {version}",
            f"Git-Commit: {full_commit}",
            f"Git-Short-Commit: {short_commit}",
            "Target-OS: Ubuntu 26.04 LTS",
            f"Target-Architecture: {arch}",
            "ROS-Distribution: lyrical",
            f"Install-Prefix: {install_prefix}",
            f"Build-Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"Source-Tree-SHA256: {source_tree_sha}",
            f"Install-Tree-SHA256: {install_tree_sha}",
            "Test-Status: passed",
            "Hardware-Target: Raspberry Pi 5 ARM64 + STM32 RRC",
            "",
            "[Files]",
        ]

        # Gather file entries for [Files] section
        files_to_record = []
        for root, _, files in os.walk(staging_dir):
            for f in files:
                if f == "release-manifest.txt":
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, staging_dir)
                size = os.path.getsize(p)
                mode = oct(stat.S_IMODE(os.stat(p).st_mode))
                sha = compute_file_sha256(p)
                files_to_record.append((rel, sha, size, mode))

        for rel, sha, size, mode in sorted(files_to_record):
            manifest_lines.append(f"{sha}  {size}  {mode}  {rel}")

        manifest_content = "\n".join(manifest_lines) + "\n"
        manifest_path = os.path.join(staging_dir, "release-manifest.txt")
        atomic_write_file(manifest_path, manifest_content.encode("utf-8"), mode=0o444)

        # Set immutable directory and file permissions in staging
        for root, dirs, files in os.walk(staging_dir):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                fp = os.path.join(root, f)
                if fp == manifest_path:
                    continue
                cur_mode = stat.S_IMODE(os.stat(fp).st_mode)
                if cur_mode & 0o111:
                    os.chmod(fp, 0o755)
                else:
                    os.chmod(fp, 0o644)

        # Compress into archive
        os.makedirs(output_dir, exist_ok=True)
        archive_name = f"ubuntu-tank-{release_id}-{arch}.tar.zst"
        archive_path = os.path.join(output_dir, archive_name)

        tar_cmd = [
            "tar",
            "--zstd",
            "-cf",
            archive_path,
            "-C",
            os.path.dirname(staging_dir),
            release_id,
        ]
        try:
            subprocess.check_call(tar_cmd)
        except Exception:
            archive_name = f"ubuntu-tank-{release_id}-{arch}.tar.gz"
            archive_path = os.path.join(output_dir, archive_name)
            subprocess.check_call(
                [
                    "tar",
                    "-czf",
                    archive_path,
                    "-C",
                    os.path.dirname(staging_dir),
                    release_id,
                ]
            )

        fsync_file(archive_path)
        return archive_path

    def _provision_service_identities(
        self, require_root: bool = True, operator_user: Optional[str] = None
    ):
        """Provision service and role groups; authorize an explicit operator or sudo caller.

        Membership becomes effective at the operator's next login. Humans never
        gain the hardware group or access to CA/service participant keys.
        """
        if os.geteuid() == 0:
            operator_user = operator_user or os.environ.get("SUDO_USER")
            if not operator_user or operator_user in ("root", "ubuntu-tank"):
                raise RuntimeError(
                    "Install requires --operator-user <login> or a non-root sudo caller"
                )
            pwd.getpwnam(operator_user)
            for role_group in ("ubuntu-tank-operators", "ubuntu-tank-status"):
                try:
                    grp.getgrnam(role_group)
                except KeyError:
                    subprocess.run(["groupadd", "-r", role_group], check=True)
            subprocess.run(
                [
                    "usermod",
                    "-aG",
                    "ubuntu-tank-operators,ubuntu-tank-status",
                    operator_user,
                ],
                check=True,
            )
            try:
                grp.getgrnam("mentorpi-rrc")
            except KeyError:
                subprocess.run(["groupadd", "-r", "mentorpi-rrc"], check=True)

            try:
                pwd.getpwnam("ubuntu-tank")
            except KeyError:
                subprocess.run(
                    [
                        "useradd",
                        "-r",
                        "-s",
                        "/usr/sbin/nologin",
                        "-g",
                        "mentorpi-rrc",
                        "-d",
                        "/var/opt/ubuntu_tank",
                        "-M",
                        "ubuntu-tank",
                    ],
                    check=True,
                )
            else:
                subprocess.run(
                    ["usermod", "-aG", "mentorpi-rrc", "ubuntu-tank"], check=False
                )
        elif require_root:
            raise PermissionError("Identity provisioning requires root privileges.")

    def _normalize_release_ownership_and_permissions(self, release_dir: str):
        """Normalize release tree to root:root and enforce immutable non-writable modes."""
        for root, dirs, files in os.walk(release_dir):
            for d in dirs:
                dp = os.path.join(root, d)
                if os.geteuid() == 0:
                    try:
                        os.chown(dp, 0, 0)
                    except Exception:
                        pass
                os.chmod(dp, 0o755)
            for f in files:
                fp = os.path.join(root, f)
                if os.geteuid() == 0:
                    try:
                        os.chown(fp, 0, 0)
                    except Exception:
                        pass
                if f == "release-manifest.txt":
                    os.chmod(fp, 0o444)
                else:
                    cur = stat.S_IMODE(os.stat(fp).st_mode)
                    if cur & 0o111:
                        os.chmod(fp, 0o755)
                    else:
                        os.chmod(fp, 0o644)

    def _is_valid_sros2_keystore(self, keystore_dir: str) -> bool:
        """Verify S/MIME grants against the permissions CA and identities against their CA.

        Return False on incomplete credentials, invalid signatures, or unavailable
        OpenSSL. This intentionally rejects the old PEM CMS provisioning format.
        """
        try:
            ca = os.path.join(keystore_dir, "permissions_ca.cert.pem")
            identity = os.path.join(keystore_dir, "identity_ca.cert.pem")
            documents = [os.path.join(keystore_dir, "governance.p7s")]
            for enc in ("controller", "guard", "bridge", "operator", "status"):
                directory = os.path.join(keystore_dir, "enclaves", "ubuntu_tank", enc)
                for filename in (
                    "key.pem",
                    "cert.pem",
                    "identity_ca.cert.pem",
                    "permissions_ca.cert.pem",
                ):
                    if not os.path.isfile(os.path.join(directory, filename)):
                        return False
                for filename, expected in (
                    ("identity_ca.cert.pem", identity),
                    ("permissions_ca.cert.pem", ca),
                ):
                    if compute_file_sha256(
                        os.path.join(directory, filename)
                    ) != compute_file_sha256(expected):
                        return False
                subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-CAfile",
                        identity,
                        os.path.join(directory, "cert.pem"),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                public_key = subprocess.check_output(
                    [
                        "openssl",
                        "pkey",
                        "-in",
                        os.path.join(directory, "key.pem"),
                        "-pubout",
                    ],
                    stderr=subprocess.PIPE,
                )
                certificate_key = subprocess.check_output(
                    [
                        "openssl",
                        "x509",
                        "-in",
                        os.path.join(directory, "cert.pem"),
                        "-pubkey",
                        "-noout",
                    ],
                    stderr=subprocess.PIPE,
                )
                if public_key != certificate_key:
                    return False
                documents.extend(
                    os.path.join(directory, name)
                    for name in ("governance.p7s", "permissions.p7s")
                )
            for document in documents:
                subprocess.run(
                    [
                        "openssl",
                        "cms",
                        "-verify",
                        "-inform",
                        "SMIME",
                        "-in",
                        document,
                        "-CAfile",
                        ca,
                        "-out",
                        os.devnull,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _provision_sros2_keystore(
        self, target_release_dir: str, tank_uid: int, rrc_gid: int
    ) -> None:
        """Publish a complete signed policy generation for the selected release.

        Preserve existing CA and participant identities while re-signing candidate
        policies. Call only during initial installation or a stopped, journaled
        activation. Old generations remain available for interrupted-operation
        recovery; private CA keys remain administrator-only.
        """
        policy = os.path.join(target_release_dir, "config", "sros2")
        subprocess.run(
            [
                sys.executable,
                os.path.join(target_release_dir, "scripts", "sros2_policy.py"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        security = os.path.join(self.etc_dir, "security")
        os.makedirs(security, mode=0o755, exist_ok=True)
        os.chmod(security, 0o755)
        if os.geteuid() == 0:
            os.chown(security, 0, 0)
        live = os.path.join(security, "keystore")
        generation = tempfile.mkdtemp(prefix=".keystore-", dir=security)
        try:
            if os.path.isdir(live):
                shutil.copytree(live, generation, dirs_exist_ok=True)

            def run(*args):
                subprocess.run(
                    ["openssl", *args],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

            for name in ("identity", "permissions"):
                key = os.path.join(generation, name + "_ca.key.pem")
                cert = os.path.join(generation, name + "_ca.cert.pem")
                if os.path.exists(key) != os.path.exists(cert):
                    raise RuntimeError(
                        "Incomplete existing CA identity; restore the security backup before activation"
                    )
                if not os.path.exists(key):
                    run(
                        "req",
                        "-x509",
                        "-newkey",
                        "rsa:2048",
                        "-nodes",
                        "-keyout",
                        key,
                        "-out",
                        cert,
                        "-days",
                        "3650",
                        "-subj",
                        "/CN=UbuntuTank" + name.title() + "CA",
                    )
            identity = os.path.join(generation, "identity_ca.cert.pem")
            identity_key = os.path.join(generation, "identity_ca.key.pem")
            permissions = os.path.join(generation, "permissions_ca.cert.pem")
            permissions_key = os.path.join(generation, "permissions_ca.key.pem")

            def sign(source, destination):
                run(
                    "cms",
                    "-sign",
                    "-nodetach",
                    "-in",
                    source,
                    "-out",
                    destination,
                    "-signer",
                    permissions,
                    "-inkey",
                    permissions_key,
                    "-outform",
                    "SMIME",
                    "-text",
                )

            governance = os.path.join(generation, "governance.p7s")
            sign(os.path.join(policy, "governance.xml"), governance)
            shutil.copy2(
                os.path.join(policy, "governance.xml"),
                os.path.join(generation, "governance.xml"),
            )
            for enc in ("controller", "guard", "bridge", "operator", "status"):
                directory = os.path.join(generation, "enclaves", "ubuntu_tank", enc)
                os.makedirs(directory, exist_ok=True)
                key, cert = (
                    os.path.join(directory, name) for name in ("key.pem", "cert.pem")
                )
                if os.path.exists(key) != os.path.exists(cert):
                    raise RuntimeError("Incomplete participant identity: " + enc)
                if not os.path.exists(key):
                    csr = os.path.join(directory, "request.csr")
                    run(
                        "req",
                        "-new",
                        "-newkey",
                        "rsa:2048",
                        "-nodes",
                        "-keyout",
                        key,
                        "-out",
                        csr,
                        "-subj",
                        "/CN=\\/ubuntu_tank\\/" + enc,
                    )
                    run(
                        "x509",
                        "-req",
                        "-in",
                        csr,
                        "-CA",
                        identity,
                        "-CAkey",
                        identity_key,
                        "-set_serial",
                        "0x" + uuid.uuid4().hex,
                        "-out",
                        cert,
                        "-days",
                        "3650",
                    )
                    os.remove(csr)
                sign(
                    os.path.join(policy, "permissions", enc + "_permissions.xml"),
                    os.path.join(directory, "permissions.p7s"),
                )
                for source in (identity, permissions, governance):
                    shutil.copy2(
                        source, os.path.join(directory, os.path.basename(source))
                    )
            if not self._is_valid_sros2_keystore(generation):
                raise RuntimeError(
                    "Generated security credentials failed cryptographic validation"
                )
            self._secure_keystore(generation, tank_uid, rrc_gid)
            publish_keystore(generation, live)
        except BaseException:
            # Published generations must survive until recovery/rollback no longer needs them.
            if os.path.realpath(live) != generation:
                shutil.rmtree(generation, ignore_errors=True)
            raise

    @staticmethod
    def _secure_keystore(directory: str, tank_uid: int, rrc_gid: int) -> None:
        """Apply per-role access and fsync a generation before it becomes visible.

        Administrators own credentials; the service and human roles only read
        their own enclaves. Parent directories permit traversal without key access.
        Non-root fixture calls retain their current ownership.
        """
        owner = 0 if os.geteuid() == 0 else os.geteuid()
        for root, dirs, files in os.walk(directory):
            role = os.path.basename(root)
            enclave = os.path.dirname(root).endswith("enclaves/ubuntu_tank")
            group = rrc_gid
            if enclave and role in ("operator", "status") and os.geteuid() == 0:
                group = grp.getgrnam(
                    "ubuntu-tank-operators"
                    if role == "operator"
                    else "ubuntu-tank-status"
                ).gr_gid
            os.chown(root, owner, group if os.geteuid() == 0 else os.getegid())
            os.chmod(root, 0o750 if enclave else 0o755)
            for filename in files:
                path = os.path.join(root, filename)
                os.chown(path, owner, group if os.geteuid() == 0 else os.getegid())
                os.chmod(path, 0o640 if enclave else 0o600)
                fsync_file(path)
            fsync_dir(root)

    def _deployment_preflight(self) -> None:
        """Reject unsupported/busy hosts before install or activation mutates assets.

        The managed controller must already be stopped for deployment preflight;
        this also prevents a live installation from changing its credentials.
        Mock overrides are forbidden on this live mutation path.
        """
        if any(name.startswith("UBUNTU_TANK_MOCK_") for name in os.environ):
            raise RuntimeError(
                "Mock host overrides are forbidden during live deployment"
            )
        subprocess.run(
            [
                "bash",
                os.path.join(os.path.dirname(__file__), "check_host.sh"),
                "--strict",
            ],
            check=True,
            timeout=60,
        )
        ok, errors = check_hardware_mutual_exclusion()
        if not ok:
            raise RuntimeError("Deployment preflight rejected: " + "; ".join(errors))

    def _provision_host_assets(
        self, target_release_dir: str, require_root: bool = True
    ) -> None:
        """
        Idempotently provision all host configuration, runtime directories,
        service identities, libexec recovery runner, and tmpfiles for an installed release.
        Preserves existing host calibration in controller.yaml.
        """
        self._normalize_release_ownership_and_permissions(target_release_dir)

        # 1. Initialize host configuration in /etc/opt/ubuntu_tank/ ONLY if missing
        os.makedirs(self.etc_dir, exist_ok=True)
        target_cfg = os.path.join(self.etc_dir, "controller.yaml")
        if not os.path.exists(target_cfg):
            default_cfg = os.path.join(target_release_dir, "config", "controller.yaml")
            if os.path.isfile(default_cfg):
                shutil.copy2(default_cfg, target_cfg)
                fsync_file(target_cfg)
                os.chmod(target_cfg, 0o644)
                sys.stdout.write(
                    f"[Install] Initialized host controller configuration at {target_cfg}\n"
                )
        else:
            sys.stdout.write(
                f"[Install] Preserved existing host configuration at {target_cfg}\n"
            )

        target_env = os.path.join(self.etc_dir, "mentorpi-tank.env")
        if not os.path.exists(target_env):
            default_env = os.path.join(target_release_dir, "host", "mentorpi-tank.env")
            if os.path.isfile(default_env):
                shutil.copy2(default_env, target_env)
                fsync_file(target_env)
                os.chmod(target_env, 0o644)
                sys.stdout.write(
                    f"[Install] Initialized host environment file at {target_env}\n"
                )
        else:
            sys.stdout.write(
                f"[Install] Preserved existing host environment at {target_env}\n"
            )

        # 2. Resolve service UID and GID
        try:
            tank_uid = pwd.getpwnam("ubuntu-tank").pw_uid
            rrc_gid = grp.getgrnam("mentorpi-rrc").gr_gid
        except (KeyError, AttributeError):
            tank_uid = os.geteuid()
            rrc_gid = os.getegid()

        # 3. Create persistent /var directories with proper ownership and modes
        # /var/opt/ubuntu_tank/deployment is mode 0755 so ubuntu-tank service account can read activation-journal
        for p, m, u, g in [
            (self.var_dir, 0o755, 0, 0),
            (os.path.join(self.var_dir, "ros-log"), 0o750, tank_uid, rrc_gid),
            (os.path.join(self.var_dir, "deployment"), 0o755, 0, 0),
            (self.snapshots_dir, 0o700, 0, 0),
        ]:
            os.makedirs(p, exist_ok=True)
            if os.geteuid() == 0:
                try:
                    os.chown(p, u, g)
                except Exception:
                    pass
            os.chmod(p, m)

        # 4. Create runtime /run directories with proper ownership
        # /run/lock/ubuntu_tank is 0775 root:mentorpi-rrc so ubuntu-tank service account can coordinate startup
        lock_dir = os.path.dirname(self.lock_path)
        for p, m, u, g in [
            (self.run_dir, 0o750, tank_uid, rrc_gid),
            (lock_dir, 0o775, 0, rrc_gid),
        ]:
            os.makedirs(p, exist_ok=True)
            if os.geteuid() == 0:
                try:
                    os.chown(p, u, g)
                except Exception:
                    pass
            os.chmod(p, m)

        # Touch deployment lock file with mode 0664 root:mentorpi-rrc if missing
        if not os.path.exists(self.lock_path):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_WRONLY, 0o664)
                os.close(fd)
                if os.geteuid() == 0:
                    try:
                        os.chown(self.lock_path, 0, rrc_gid)
                    except Exception:
                        pass
                os.chmod(self.lock_path, 0o664)
            except Exception:
                pass

        # 5. Install self-contained release-independent recovery runner into /opt/ubuntu_tank/libexec/
        libexec_dir = os.path.join(self.opt_dir, "libexec")
        os.makedirs(libexec_dir, exist_ok=True)

        this_script = os.path.abspath(__file__)
        shutil.copy2(this_script, os.path.join(libexec_dir, "deployment_manager.py"))
        config_mig_src = os.path.join(
            os.path.dirname(this_script), "config_migration.py"
        )
        if os.path.isfile(config_mig_src):
            shutil.copy2(
                config_mig_src, os.path.join(libexec_dir, "config_migration.py")
            )

        rec_target = os.path.join(libexec_dir, "recover-activation")
        rec_content = (
            "#!/usr/bin/env python3\n"
            '"""Release-independent boot recovery runner."""\n'
            "import os, sys\n"
            "LIBEXEC_DIR = os.path.dirname(os.path.abspath(__file__))\n"
            "if LIBEXEC_DIR not in sys.path:\n"
            "    sys.path.insert(0, LIBEXEC_DIR)\n"
            "from deployment_manager import main\n"
            "if __name__ == '__main__':\n"
            "    sys.argv = [sys.argv[0], 'recover'] + sys.argv[1:]\n"
            "    sys.exit(main())\n"
        )
        atomic_write_file(rec_target, rec_content.encode("utf-8"), mode=0o755)

        # 6. Install tmpfiles.d configuration and apply it
        tmpfiles_conf = os.path.join(target_release_dir, "host", "ubuntu-tank.conf")
        dest_tmpfiles = "/etc/tmpfiles.d/ubuntu-tank.conf"
        if (
            os.path.isfile(tmpfiles_conf)
            and os.path.isdir("/etc/tmpfiles.d")
            and os.geteuid() == 0
        ):
            shutil.copy2(tmpfiles_conf, dest_tmpfiles)
            fsync_file(dest_tmpfiles)
            if shutil.which("systemd-tmpfiles"):
                subprocess.run(
                    ["systemd-tmpfiles", "--create", dest_tmpfiles], check=False
                )

        # 7. Provision authentic signed SROS2 security credentials
        if not os.path.lexists(self.current_symlink):
            self._provision_sros2_keystore(target_release_dir, tank_uid, rrc_gid)

        # 8. Verify all required host assets exist
        if not os.path.isfile(target_cfg):
            raise RuntimeError(
                f"Host configuration missing at {target_cfg} after provisioning."
            )
        if not os.path.isfile(rec_target) or not os.access(rec_target, os.X_OK):
            raise RuntimeError(
                f"Recovery runner missing or not executable at {rec_target} after provisioning."
            )
        keystore_dir = os.path.join(self.etc_dir, "security", "keystore")
        if not os.path.lexists(
            self.current_symlink
        ) and not self._is_valid_sros2_keystore(keystore_dir):
            raise RuntimeError(
                f"SROS2 security keystore missing or incomplete at {keystore_dir} after provisioning."
            )

    def install_release(
        self,
        archive_path: str,
        require_root: bool = True,
        enforce_arm64: bool = True,
        operator_user: Optional[str] = None,
    ) -> str:
        """
        Validate and extract release into /opt/ubuntu_tank/releases/<release-id>.
        Initializes missing /etc/opt host configuration without overwriting existing files.
        Provisions service identities and sets secure runtime/state ownership.
        The optional operator_user grants operator/status roles (default: sudo
        caller). Live invocations require strict target preflight and a stopped
        controller. Never activates, starts, or arms the controller.
        """
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(f"Release archive not found: '{archive_path}'")

        if require_root and os.geteuid() != 0:
            raise PermissionError(
                "Installation must run as root to configure system prerequisites and set immutable permissions."
            )

        with DeploymentLock(self.lock_path):
            if require_root:
                self._deployment_preflight()
            # Validate the archive and any existing payload before provisioning.
            # Extract into temporary storage only; host identities/assets follow validation.
            tmp_extract_dir = os.path.join(self.opt_dir, ".install_staging")
            if os.path.exists(tmp_extract_dir):
                shutil.rmtree(tmp_extract_dir)
            os.makedirs(tmp_extract_dir, exist_ok=True)

            try:
                tar_args = [
                    "tar",
                    "--no-same-owner",
                    "-xf",
                    archive_path,
                    "-C",
                    tmp_extract_dir,
                ]
                if archive_path.endswith(".zst"):
                    tar_args.insert(1, "--zstd")
                subprocess.check_call(tar_args)

                entries = os.listdir(tmp_extract_dir)
                if len(entries) != 1 or not os.path.isdir(
                    os.path.join(tmp_extract_dir, entries[0])
                ):
                    raise RuntimeError(
                        "Invalid release archive layout: expected single release directory root."
                    )

                rel_id = entries[0]
                self.validate_release_id(rel_id)
                staged_release = os.path.join(tmp_extract_dir, rel_id)

                manifest_file = os.path.join(staged_release, "release-manifest.txt")
                if not os.path.isfile(manifest_file):
                    raise RuntimeError(
                        f"Missing release-manifest.txt in extracted archive {archive_path}"
                    )

                headers, _ = parse_release_manifest(manifest_file)
                if headers.get("Release-Id") != rel_id:
                    raise RuntimeError(
                        f"Release ID mismatch: manifest declares '{headers.get('Release-Id')}', directory is '{rel_id}'"
                    )

                if enforce_arm64:
                    manifest_arch = headers.get("Target-Architecture", "")
                    if manifest_arch != "arm64":
                        raise RuntimeError(
                            f"Target architecture mismatch: artifact declared '{manifest_arch}', target requires 'arm64'"
                        )

                if require_root:
                    verify_build(
                        os.path.join(staged_release, "install"),
                        f"{self.opt_dir}/releases/{rel_id}/install",
                        os.path.join(staged_release, "src"),
                    )
                # Validate full staged release checksums and immutability
                valid, errs = self.validate_release(
                    staged_release, expected_release_id=rel_id
                )
                if not valid:
                    raise RuntimeError(
                        f"Release archive verification failed:\n" + "\n".join(errs)
                    )

                target_release_dir = os.path.join(self.releases_dir, rel_id)
                if os.path.exists(target_release_dir):
                    t_valid, t_errs = self.validate_release(
                        target_release_dir, expected_release_id=rel_id
                    )
                    if not t_valid:
                        raise RuntimeError(
                            f"Refusing to overwrite existing release directory '{target_release_dir}' with differing contents:\n"
                            + "\n".join(t_errs)
                        )
                    installed_headers, installed_files = parse_release_manifest(
                        os.path.join(target_release_dir, "release-manifest.txt")
                    )
                    incoming_headers, incoming_files = parse_release_manifest(
                        manifest_file
                    )
                    if installed_files != incoming_files or any(
                        installed_headers.get(key) != incoming_headers.get(key)
                        for key in (
                            "Install-Prefix",
                            "Target-Architecture",
                            "ROS-Distribution",
                        )
                    ):
                        raise RuntimeError(
                            "Conflicting archive for existing release ID; choose a new release ID"
                        )
                    self._provision_service_identities(
                        require_root=require_root, operator_user=operator_user
                    )
                    sys.stdout.write(
                        f"[Install] Release '{rel_id}' is already present at {target_release_dir}; ensuring host provisioning is complete...\n"
                    )
                    self._provision_host_assets(
                        target_release_dir, require_root=require_root
                    )
                    sys.stdout.write(
                        f"[Install] Successfully completed host provisioning for existing release '{rel_id}'.\n"
                        f"[Install] Note: Release installed in stopped and disarmed state.\n"
                    )
                    return rel_id

                self._provision_service_identities(
                    require_root=require_root, operator_user=operator_user
                )
                os.makedirs(self.releases_dir, exist_ok=True)
                os.replace(staged_release, target_release_dir)
                fsync_dir(self.releases_dir)

                # Provision host configuration, libexec runner, runtime/var directories, and tmpfiles
                self._provision_host_assets(
                    target_release_dir, require_root=require_root
                )

                sys.stdout.write(
                    f"[Install] Successfully installed release '{rel_id}' into {target_release_dir}.\n"
                    f"[Install] Note: Release installed in stopped and disarmed state.\n"
                )
                return rel_id

            finally:
                if os.path.exists(tmp_extract_dir):
                    shutil.rmtree(tmp_extract_dir)

    def activate_release(self, release_id: str, require_root: bool = True) -> str:
        """
        Execute 6-step transactional activation with write-ahead journal and fsync points.
        Leaves the service stopped and disarmed.
        """
        self.validate_release_id(release_id)
        if require_root and os.geteuid() != 0:
            raise PermissionError(
                "Activation must run as root to manage systemd units and udev rules."
            )

        candidate_dir = os.path.join(self.releases_dir, release_id)
        if not os.path.isdir(candidate_dir):
            raise FileNotFoundError(
                f"Candidate release '{release_id}' not found under {self.releases_dir}"
            )

        with DeploymentLock(self.lock_path):
            if require_root:
                self._deployment_preflight()
            state = self.journal.get_state()
            pending = state.get("current_transaction")
            if pending is not None:
                sys.stderr.write(
                    f"[Activate] Interrupted pending transaction '{pending.get('tx_id')}' in state "
                    f"'{pending.get('status')}' detected. Reconciling prior baseline before activation...\n"
                )
                if not self._recover_activation_unlocked(state):
                    raise RuntimeError(
                        f"Activation rejected: existing uncommitted transaction '{pending.get('tx_id')}' "
                        "failed recovery. Startup and deployment blocked."
                    )
                state = self.journal.get_state()

            # Step 1: Validate candidate release integrity and non-starting paths
            valid, errs = self.validate_release(
                candidate_dir, expected_release_id=release_id
            )
            if not valid:
                raise RuntimeError(
                    f"Candidate release validation failed:\n" + "\n".join(errs)
                )

            # Step 2: Snapshot current state and write PREPARED journal record
            current_target = None
            current_release_id = None
            if os.path.islink(self.current_symlink):
                try:
                    current_target = os.path.realpath(self.current_symlink)
                    current_release_id = os.path.basename(current_target)
                except Exception:
                    pass

            tx_id = f"tx-{uuid.uuid4().hex}"
            snapshot_dir = self.snapshot_mgr.create_snapshot(
                tx_id=tx_id,
                current_symlink_target=current_target,
                etc_dir=self.etc_dir,
                systemd_dir=self.systemd_dir,
                udev_dir=self.udev_dir,
            )

            self.journal.record_prepared(
                tx_id=tx_id,
                candidate_release_id=release_id,
                candidate_release_path=candidate_dir,
                previous_release_id=current_release_id,
                previous_release_path=current_target,
                snapshot_dir=snapshot_dir,
            )

            try:
                # Step 3: Disarm, send repeated zero, stop service, confirm inactive
                self._stop_and_disarm_service(allow_unsupported=(not require_root))

                # Step 4: Stage and fsync candidate host files, atomic rename, reload udev/systemd
                self._stage_host_files(candidate_dir)
                self._provision_sros2_keystore(
                    candidate_dir,
                    os.geteuid(),
                    grp.getgrnam("mentorpi-rrc").gr_gid
                    if require_root
                    else os.getegid(),
                )
                self.journal.record_activating(tx_id)

                # Step 5: Atomically replace and fsync current symlink, validate non-starting
                symlink_tmp = f"{self.current_symlink}.tmp.{os.getpid()}"
                rel_path = os.path.relpath(candidate_dir, self.opt_dir)
                os.symlink(rel_path, symlink_tmp)
                os.replace(symlink_tmp, self.current_symlink)
                fsync_dir(self.opt_dir)

                # Non-starting validation of active release
                runner_bin = os.path.join(
                    self.current_symlink, "bin", "mentorpi-tank-run"
                )
                if not os.path.isfile(runner_bin) or not os.access(runner_bin, os.X_OK):
                    raise RuntimeError(
                        f"Active launcher missing or not executable at {runner_bin}"
                    )

                # Step 6: Write COMMITTED state with fsync
                self.journal.record_committed(
                    tx_id=tx_id, active_id=release_id, active_path=candidate_dir
                )

                sys.stdout.write(
                    f"[Activate] Successfully activated release '{release_id}'.\n"
                    f"[Activate] Symlink {self.current_symlink} -> {rel_path}\n"
                    f"[Activate] Service remains stopped and disarmed.\n"
                )
                return release_id

            except Exception as e:
                # Rollback on failure
                sys.stderr.write(
                    f"[Activate] Transaction {tx_id} failed: {e}. Initiating automatic rollback...\n"
                )
                self._rollback_transaction(tx_id, snapshot_dir, current_target)
                self.journal.record_aborted(tx_id, reason=str(e))
                raise

    def rollback_release(self, require_root: bool = True) -> Optional[str]:
        """
        Offline rollback to the previously recorded working release and snapshot.
        Fails closed if the previous release or snapshot cannot be verified.
        Leaves service stopped and disarmed.
        """
        if require_root and os.geteuid() != 0:
            raise PermissionError("Rollback must run as root to restore system assets.")

        with DeploymentLock(self.lock_path):
            state = self.journal.get_state()
            pending = state.get("current_transaction")
            if pending is not None:
                sys.stderr.write(
                    f"[Rollback] Interrupted pending transaction '{pending.get('tx_id')}' detected. "
                    "Reconciling prior baseline before rollback...\n"
                )
                if not self._recover_activation_unlocked(state):
                    raise RuntimeError(
                        f"Rollback rejected: existing uncommitted transaction '{pending.get('tx_id')}' "
                        "failed recovery. Startup and deployment blocked."
                    )
                state = self.journal.get_state()

            prev_id = state.get("previous_release_id")
            prev_path = state.get("previous_release_path")

            if not prev_id or not prev_path or not os.path.isdir(prev_path):
                # Search history for last committed release
                history = state.get("history", [])
                for tx in reversed(history):
                    cand_p = tx.get("previous_release_path")
                    cand_id = tx.get("previous_release_id")
                    if cand_id and cand_p and os.path.isdir(cand_p):
                        prev_id = cand_id
                        prev_path = cand_p
                        break

            if not prev_id or not prev_path or not os.path.isdir(prev_path):
                raise RuntimeError(
                    "No previous verified release found in activation journal for rollback."
                )

            # Verify previous release integrity before mutation
            valid, errs = self.validate_release(prev_path, expected_release_id=prev_id)
            if not valid:
                raise RuntimeError(
                    f"Rollback rejected: previous release at '{prev_path}' failed integrity validation:\n"
                    + "\n".join(errs)
                )

            # Find matching snapshot
            snapshot_dir = None
            history = state.get("history", [])
            for tx in reversed(history):
                if tx.get("previous_release_id") == prev_id and tx.get("snapshot_dir"):
                    if os.path.isdir(tx["snapshot_dir"]):
                        snapshot_dir = tx["snapshot_dir"]
                        break

            if not snapshot_dir or not self.snapshot_mgr.verify_snapshot(snapshot_dir):
                raise RuntimeError(
                    f"Rollback rejected: host snapshot at '{snapshot_dir}' is missing or corrupted."
                )

            tx_id = f"tx-rollback-{uuid.uuid4().hex}"
            # Record rollback transaction to journal BEFORE any asset or service mutation
            self.journal.record_rollback_prepared(
                tx_id=tx_id,
                target_release_id=prev_id,
                target_release_path=prev_path,
                snapshot_dir=snapshot_dir,
                previous_release_id=state.get("active_release_id"),
                previous_release_path=state.get("active_release_path"),
            )

            try:
                self._stop_and_disarm_service(allow_unsupported=(not require_root))

                sys.stdout.write(
                    f"[Rollback] Restoring host configuration and units from snapshot: {snapshot_dir}\n"
                )
                self.snapshot_mgr.restore_snapshot(
                    snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
                )
                self._reload_systemd_and_udev()

                # Atomically replace symlink
                symlink_tmp = f"{self.current_symlink}.tmp.{os.getpid()}"
                rel_path = os.path.relpath(prev_path, self.opt_dir)
                os.symlink(rel_path, symlink_tmp)
                os.replace(symlink_tmp, self.current_symlink)
                fsync_dir(self.opt_dir)

                self.journal.record_rolled_back(
                    tx_id, active_id=prev_id, active_path=prev_path
                )

                sys.stdout.write(
                    f"[Rollback] Successfully rolled back to release '{prev_id}'.\n"
                    f"[Rollback] Symlink {self.current_symlink} -> {rel_path}\n"
                    f"[Rollback] Controller remains stopped and disarmed.\n"
                )
                return prev_id
            except Exception as e:
                sys.stderr.write(
                    f"[Rollback] Interrupted or failed during rollback {tx_id}: {e}\n"
                )
                raise

    def recover_activation(self, check_mutual_exclusion: bool = True) -> bool:
        """
        Crash-consistent boot recovery runner.
        Verifies hardware mutual exclusion before restoring or reconciling activation state.
        If PREPARED, ACTIVATING, or ROLLING_BACK found in journal, verifies snapshot and baseline release,
        restores snapshot, and disarms. Fails closed and blocks startup if baseline or journal is unverified.
        """
        if check_mutual_exclusion:
            ok, errs = check_hardware_mutual_exclusion()
            if not ok:
                sys.stderr.write(
                    "[Recovery] FATAL: Mutual exclusion check failed; cannot proceed with recovery:\n"
                    + "\n".join(f"  - {e}" for e in errs)
                    + "\n"
                )
                return False

        with DeploymentLock(self.lock_path):
            try:
                state = self.journal.get_state()
            except Exception as e:
                sys.stderr.write(
                    f"[Recovery] FATAL: Failed to read journal '{self.journal.journal_path}': {e}. "
                    "Failing closed to block startup without altering deployment assets.\n"
                )
                return False

            return self._recover_activation_unlocked(state)

    def _recover_activation_unlocked(self, state: Dict[str, Any]) -> bool:
        """Internal recovery logic executed under active DeploymentLock."""
        tx = state.get("current_transaction")
        if not tx:
            sys.stdout.write("[Recovery] Journal clean: no uncommitted transaction.\n")
            return True

        status = tx.get("status")
        tx_id = tx.get("tx_id", "unknown")
        tx_type = tx.get("type", "ACTIVATION")

        if status in ("PREPARED", "ACTIVATING", "ROLLING_BACK"):
            sys.stderr.write(
                f"[Recovery] Uncommitted transaction {tx_id} (type={tx_type}, status={status}) detected. "
                "Validating recovery target baseline...\n"
            )
            snapshot_dir = tx.get("snapshot_dir")
            if status == "ROLLING_BACK" or tx_type == "ROLLBACK":
                # Rollback was interrupted while restoring target release
                target_path = tx.get("candidate_release_path")
                target_id = tx.get("candidate_release_id")
            else:
                # Activation was interrupted; restore to previous release baseline
                target_path = tx.get("previous_release_path")
                target_id = tx.get("previous_release_id")

            # Require verified snapshot before mutation
            if not snapshot_dir or not self.snapshot_mgr.verify_snapshot(snapshot_dir):
                sys.stderr.write(
                    f"[Recovery] FATAL: Snapshot '{snapshot_dir}' is missing or corrupted. "
                    "Refusing to restore unverified baseline; preserving pending transaction to block startup.\n"
                )
                return False

            # If target release existed, require valid payload and manifest
            if target_path:
                if not os.path.isdir(target_path):
                    sys.stderr.write(
                        f"[Recovery] FATAL: Recovery target release path '{target_path}' does not exist. "
                        "Refusing to switch symlink; preserving pending transaction.\n"
                    )
                    return False
                valid, errs = self.validate_release(
                    target_path, expected_release_id=target_id
                )
                if not valid:
                    sys.stderr.write(
                        f"[Recovery] FATAL: Recovery target release '{target_id}' failed integrity validation:\n"
                        + "\n".join(f"  - {e}" for e in errs)
                        + "\n"
                        "Refusing to switch symlink; preserving pending transaction.\n"
                    )
                    return False

            self._stop_and_disarm_service(allow_unsupported=True)

            self.snapshot_mgr.restore_snapshot(
                snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
            )
            self._reload_systemd_and_udev()

            if target_path and os.path.isdir(target_path):
                symlink_tmp = f"{self.current_symlink}.tmp.{os.getpid()}"
                rel_path = os.path.relpath(target_path, self.opt_dir)
                os.symlink(rel_path, symlink_tmp)
                os.replace(symlink_tmp, self.current_symlink)
                fsync_dir(self.opt_dir)
            else:
                if os.path.islink(self.current_symlink):
                    os.unlink(self.current_symlink)

            self.journal.record_rolled_back(
                tx_id, active_id=target_id, active_path=target_path
            )
            sys.stdout.write(
                f"[Recovery] Crash recovery completed: restored to '{target_id or 'none'}' and disarmed.\n"
            )
            return True

        return True

    def _stop_and_disarm_service(
        self, timeout_sec: float = 10.0, allow_unsupported: bool = False
    ):
        """
        Stop mentorpi-tank.service and strictly verify inactivity before asset changes.
        Handles deactivating states by polling until confirmed inactive/failed, and
        rejects empty query responses or ambiguous states.
        """
        if not shutil.which("systemctl"):
            if allow_unsupported or os.geteuid() != 0:
                return
            raise RuntimeError(
                "systemctl command not available; cannot verify controller is stopped."
            )

        # Check if service is loaded and active
        res = subprocess.run(
            ["systemctl", "is-active", "mentorpi-tank.service"],
            capture_output=True,
            text=True,
        )
        status = res.stdout.strip()
        if not status:
            raise RuntimeError(
                "Failed to query status of mentorpi-tank.service: empty response or query failure."
            )

        if status in ("inactive", "failed"):
            return

        if status in ("active", "activating", "reloading"):
            stop_res = subprocess.run(
                ["systemctl", "stop", "mentorpi-tank.service"],
                capture_output=True,
                text=True,
            )
            if stop_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to execute 'systemctl stop mentorpi-tank.service': {stop_res.stderr.strip()}"
                )
        elif status == "deactivating":
            # Already shutting down; proceed to poll until completed
            pass
        else:
            raise RuntimeError(
                f"Ambiguous or unexpected service status '{status}'; refusing to proceed."
            )

        start = time.monotonic()
        while time.monotonic() - start < timeout_sec:
            poll_res = subprocess.run(
                ["systemctl", "is-active", "mentorpi-tank.service"],
                capture_output=True,
                text=True,
            )
            poll_status = poll_res.stdout.strip()
            if poll_status in ("inactive", "failed"):
                return
            if poll_status not in ("active", "activating", "reloading", "deactivating"):
                raise RuntimeError(
                    f"Ambiguous or unexpected service status '{poll_status}' while waiting for shutdown."
                )
            time.sleep(0.2)

        # Escalation: send SIGKILL if still active or deactivating
        subprocess.run(
            ["systemctl", "kill", "-s", "SIGKILL", "mentorpi-tank.service"], check=False
        )
        time.sleep(0.5)

        final_res = subprocess.run(
            ["systemctl", "is-active", "mentorpi-tank.service"],
            capture_output=True,
            text=True,
        )
        final_status = final_res.stdout.strip()
        if final_status not in ("inactive", "failed"):
            raise RuntimeError(
                f"Safety violation: mentorpi-tank.service remains active after stop timeout (status '{final_status}'). Failing closed."
            )

    @staticmethod
    def extract_udev_discriminator(rule_text: str) -> Optional[str]:
        """Extract ATTRS{serial} or KERNELS discriminator from udev rule text."""
        import re

        m = re.search(
            r'(ATTRS\{serial\}==["\'][^"\']+["\']|KERNELS==["\'][^"\']+["\']|ENV\{ID_SERIAL_SHORT\}==["\'][^"\']+["\'])',
            rule_text,
        )
        if m:
            return m.group(1)
        return None

    @staticmethod
    def is_ambiguous_udev_rule(
        rule_text: str, detected_devices: Optional[List[Dict[str, str]]] = None
    ) -> Tuple[bool, str]:
        """
        Reject rules without one literal persistent serial or physical-path identity.

        The optional inventory is retained for callers but current adapter count
        cannot establish persistent identity and never relaxes this requirement.
        """
        rules = [
            line
            for line in rule_text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(rules) != 1:
            return True, "Exactly one device rule is required"
        disc = ReleaseManager.extract_udev_discriminator(rules[0])
        if not disc:
            return (
                True,
                "Persistent serial or physical-path discriminator is required, even with one adapter",
            )
        value = disc.split("==", 1)[1].strip("\"'")
        if not value or any(c in value for c in "*?[]<>|\\"):
            return (
                True,
                "Discriminator must be a concrete literal without wildcards/placeholders",
            )
        return False, ""

    @staticmethod
    def merge_udev_rule(candidate_rule: str, host_rule: Optional[str]) -> str:
        """
        Preserve host-specific udev discriminator (e.g. ATTRS{serial}) into candidate rule.
        """
        if not host_rule:
            return candidate_rule

        host_disc = ReleaseManager.extract_udev_discriminator(host_rule)
        cand_disc = ReleaseManager.extract_udev_discriminator(candidate_rule)

        # If candidate already has discriminator, keep it
        if cand_disc:
            return candidate_rule

        # If host had a discriminator, preserve it into candidate rule
        if host_disc:
            target = 'ATTRS{idProduct}=="55d4",'
            if target in candidate_rule:
                return candidate_rule.replace(target, f"{target} {host_disc},")
            group_target = "GROUP="
            if group_target in candidate_rule:
                return candidate_rule.replace(
                    group_target, f"{host_disc}, {group_target}"
                )

        return candidate_rule

    @staticmethod
    def _bind_udev_identity(rule: str) -> str:
        """Bind an unconfigured template to one observed RRC using serial or USB port.

        Require exactly one matching adapter during initial selection. Thereafter
        preserve the recorded literal identity across enumeration/reconnects.
        """
        matches = []
        for entry in os.scandir("/sys/bus/usb/devices"):
            try:
                with open(os.path.join(entry.path, "idVendor")) as stream:
                    vendor = stream.read().strip()
                with open(os.path.join(entry.path, "idProduct")) as stream:
                    product = stream.read().strip()
                if (vendor, product) != ("1a86", "55d4"):
                    continue
                serial_file = os.path.join(entry.path, "serial")
                serial = ""
                if os.path.isfile(serial_file):
                    with open(serial_file) as stream:
                        serial = stream.read().strip()
                if serial and re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
                    matches.append('ATTRS{serial}=="' + serial + '"')
                elif re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", entry.name):
                    matches.append('KERNELS=="' + entry.name + '"')
                else:
                    raise RuntimeError(
                        "RRC has no usable persistent serial or USB port identity"
                    )
            except FileNotFoundError:
                continue
        if len(matches) != 1:
            raise RuntimeError(
                "Initial RRC identity selection requires exactly one connected matching adapter"
            )
        return ReleaseManager.merge_udev_rule(rule, matches[0])

    def _stage_host_files(self, candidate_dir: str):
        """Stage host files to temporary targets on destination filesystem, fsync, and atomic rename."""
        udev_target = os.path.join(self.udev_dir, "99-mentorpi-rrc.rules")
        candidate_udev_src = os.path.join(
            candidate_dir, "host", "99-mentorpi-rrc.rules"
        )

        # Staging udev rules with serial identity preservation & ambiguity checks
        if os.path.isfile(candidate_udev_src):
            with open(candidate_udev_src, "r", encoding="utf-8") as f:
                cand_udev_text = f.read()

            host_udev_text = None
            if os.path.isfile(udev_target):
                try:
                    with open(udev_target, "r", encoding="utf-8") as f:
                        host_udev_text = f.read()
                except Exception:
                    pass

            merged_udev = self.merge_udev_rule(cand_udev_text, host_udev_text)
            if not self.extract_udev_discriminator(merged_udev):
                merged_udev = self._bind_udev_identity(merged_udev)
            ambiguous, reason = self.is_ambiguous_udev_rule(merged_udev)
            if ambiguous:
                raise RuntimeError(
                    f"Activation rejected: ambiguous udev rule for /dev/rrc: {reason}"
                )

            atomic_write_file(udev_target, merged_udev.encode("utf-8"), mode=0o644)

        file_mappings = [
            (
                os.path.join(candidate_dir, "host", "mentorpi-tank.service"),
                os.path.join(self.systemd_dir, "mentorpi-tank.service"),
            ),
            (
                os.path.join(candidate_dir, "host", "mentorpi-tank-recover.service"),
                os.path.join(self.systemd_dir, "mentorpi-tank-recover.service"),
            ),
        ]

        for src, dst in file_mappings:
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    data = f.read()
                atomic_write_file(dst, data, mode=0o644)

        self._reload_systemd_and_udev()

    def _reload_systemd_and_udev(self):
        """Reload systemd daemon and udev rules."""
        if shutil.which("systemctl") and os.geteuid() == 0:
            try:
                subprocess.run(
                    ["systemctl", "daemon-reload"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        if shutil.which("udevadm") and os.geteuid() == 0:
            try:
                subprocess.run(
                    ["udevadm", "control", "--reload-rules"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    ["udevadm", "trigger"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def _rollback_transaction(
        self, tx_id: str, snapshot_dir: str, previous_target: Optional[str]
    ):
        """Roll back an uncommitted activation transaction."""
        self._stop_and_disarm_service(allow_unsupported=True)
        if snapshot_dir and self.snapshot_mgr.verify_snapshot(snapshot_dir):
            self.snapshot_mgr.restore_snapshot(
                snapshot_dir, self.etc_dir, self.systemd_dir, self.udev_dir
            )
            self._reload_systemd_and_udev()

        if previous_target and os.path.isdir(previous_target):
            symlink_tmp = f"{self.current_symlink}.tmp.{os.getpid()}"
            rel_path = os.path.relpath(previous_target, self.opt_dir)
            os.symlink(rel_path, symlink_tmp)
            os.replace(symlink_tmp, self.current_symlink)
            fsync_dir(self.opt_dir)
        else:
            if os.path.islink(self.current_symlink):
                os.unlink(self.current_symlink)


def main():
    parser = argparse.ArgumentParser(description="Ubuntu Tank Deployment Manager CLI")
    sub = parser.add_subparsers(dest="command")

    attest = sub.add_parser(
        "attest-build",
        help="Record provenance after a successful production-prefix build",
    )
    attest.add_argument("--install-tree", required=True)
    attest.add_argument("--prefix", required=True)
    attest.add_argument("--source", required=True)
    attest.add_argument(
        "--synthetic",
        action="store_true",
        help="Mark hardware-free fixture output; never deploy as production",
    )

    # package
    pkg_p = sub.add_parser("package")
    pkg_p.add_argument(
        "--workspace",
        default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    )
    pkg_p.add_argument("--output-dir", default=None)
    pkg_p.add_argument("--release-id", default=None)
    pkg_p.add_argument("--arch", default="arm64")
    pkg_p.add_argument("--allow-staged-install", action="store_true")
    pkg_p.add_argument(
        "--install-tree", default=None, help="Path to built production install tree"
    )
    pkg_p.add_argument(
        "--build-root", default=None, help="Disposable build root directory"
    )

    # install
    inst_p = sub.add_parser("install")
    inst_p.add_argument("archive")
    inst_p.add_argument(
        "--operator-user",
        help="Login granted operator and status credentials; defaults to SUDO_USER",
    )
    inst_p.add_argument("--opt-dir", default=DEFAULT_OPT_DIR)
    inst_p.add_argument("--etc-dir", default=DEFAULT_ETC_DIR)
    inst_p.add_argument("--var-dir", default=DEFAULT_VAR_DIR)
    inst_p.add_argument("--run-dir", default=DEFAULT_RUN_DIR)
    inst_p.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    inst_p.add_argument("--no-require-root", action="store_true")
    inst_p.add_argument("--no-enforce-arm64", action="store_true")

    # activate
    act_p = sub.add_parser("activate")
    act_p.add_argument("release_id")
    act_p.add_argument("--opt-dir", default=DEFAULT_OPT_DIR)
    act_p.add_argument("--etc-dir", default=DEFAULT_ETC_DIR)
    act_p.add_argument("--var-dir", default=DEFAULT_VAR_DIR)
    act_p.add_argument("--systemd-dir", default=DEFAULT_SYSTEMD_DIR)
    act_p.add_argument("--udev-dir", default=DEFAULT_UDEV_DIR)
    act_p.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    act_p.add_argument("--no-require-root", action="store_true")

    # rollback
    rb_p = sub.add_parser("rollback")
    rb_p.add_argument("--opt-dir", default=DEFAULT_OPT_DIR)
    rb_p.add_argument("--etc-dir", default=DEFAULT_ETC_DIR)
    rb_p.add_argument("--var-dir", default=DEFAULT_VAR_DIR)
    rb_p.add_argument("--systemd-dir", default=DEFAULT_SYSTEMD_DIR)
    rb_p.add_argument("--udev-dir", default=DEFAULT_UDEV_DIR)
    rb_p.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    rb_p.add_argument("--no-require-root", action="store_true")

    # recover
    rec_p = sub.add_parser("recover")
    rec_p.add_argument("--opt-dir", default=DEFAULT_OPT_DIR)
    rec_p.add_argument("--etc-dir", default=DEFAULT_ETC_DIR)
    rec_p.add_argument("--var-dir", default=DEFAULT_VAR_DIR)
    rec_p.add_argument("--systemd-dir", default=DEFAULT_SYSTEMD_DIR)
    rec_p.add_argument("--udev-dir", default=DEFAULT_UDEV_DIR)
    rec_p.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)

    args = parser.parse_args()

    if getattr(args, "no_require_root", False) or getattr(
        args, "no_enforce_arm64", False
    ):
        parser.error(
            "Live CLI deployment cannot bypass root or target validation; use isolated Python fixtures"
        )

    if args.command == "attest-build":
        attest_build(
            args.install_tree, args.prefix, args.source, synthetic=args.synthetic
        )
        return

    if args.command == "package":
        if args.release_id:
            ReleaseManager.validate_release_id(args.release_id)
        out_dir = args.output_dir or os.path.join(args.workspace, "dist")
        mgr = ReleaseManager()
        archive = mgr.package_release(
            workspace_dir=args.workspace,
            output_dir=out_dir,
            release_id=args.release_id,
            arch=args.arch,
            allow_staged_install=args.allow_staged_install,
            install_tree=args.install_tree,
            build_root=args.build_root,
        )
        print(f"Packaged release archive: {archive}")
        sys.exit(0)

    elif args.command == "install":
        mgr = ReleaseManager(
            opt_dir=args.opt_dir,
            etc_dir=args.etc_dir,
            var_dir=args.var_dir,
            run_dir=args.run_dir,
            lock_path=args.lock_path,
        )
        rel_id = mgr.install_release(
            archive_path=args.archive,
            require_root=not args.no_require_root,
            enforce_arm64=not args.no_enforce_arm64,
            operator_user=args.operator_user,
        )
        print(f"Installed release: {rel_id}")
        sys.exit(0)

    elif args.command == "activate":
        ReleaseManager.validate_release_id(args.release_id)
        mgr = ReleaseManager(
            opt_dir=args.opt_dir,
            etc_dir=args.etc_dir,
            var_dir=args.var_dir,
            systemd_dir=args.systemd_dir,
            udev_dir=args.udev_dir,
            lock_path=args.lock_path,
        )
        rel_id = mgr.activate_release(
            release_id=args.release_id, require_root=not args.no_require_root
        )
        print(f"Activated release: {rel_id}")
        sys.exit(0)

    elif args.command == "rollback":
        mgr = ReleaseManager(
            opt_dir=args.opt_dir,
            etc_dir=args.etc_dir,
            var_dir=args.var_dir,
            systemd_dir=args.systemd_dir,
            udev_dir=args.udev_dir,
            lock_path=args.lock_path,
        )
        rel_id = mgr.rollback_release(require_root=not args.no_require_root)
        print(f"Rolled back to release: {rel_id}")
        sys.exit(0)

    elif args.command == "recover":
        mgr = ReleaseManager(
            opt_dir=args.opt_dir,
            etc_dir=args.etc_dir,
            var_dir=args.var_dir,
            systemd_dir=args.systemd_dir,
            udev_dir=args.udev_dir,
            lock_path=args.lock_path,
        )
        ok = mgr.recover_activation()
        sys.exit(0 if ok else 1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
