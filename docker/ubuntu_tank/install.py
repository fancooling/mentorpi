#!/usr/bin/env python3
"""Rootful Docker host operations for the single-owner Ubuntu 26.04 Pi 5.

Stage never starts applications. Deploy blocks control before stopping the old
pair and admits only a verified, stopped replacement. Failure leaves admission
closed; recovery is an explicit redeploy. Persistent configuration is retained.
The installed copy uses only Python's standard library; development uses .venv.
Run this CLI on the Pi with prebuilt images already loaded. Building, smoke
testing and SSH transfer belong to the development-host deploy.sh workflow.
Configuration, TLS and health probes run in those images on the Pi; image
identity verification may export a temporary archive but never builds images.
"""

import argparse
import contextlib
import fcntl
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import camera_devices
from image_identity import resolve_image

HERE = Path(__file__).resolve().parent
STATE = Path("/var/lib/ubuntu_tank-container")
CONTROL = STATE / "control"
RUN = Path("/run/ubuntu_tank-container")
INSTALL = Path("/opt/ubuntu_tank-container")
LOCK = Path("/run/lock/ubuntu_tank/deploy.lock")
HOST_FILES = (
    "install.py",
    "image_identity.py",
    "tls_setup.py",
    "camera_devices.py",
    "compose.yaml",
    "ubuntu-tank-container.service",
)
NATIVE = (
    "mentorpi-tank-stack.target",
    "mentorpi-tank.service",
    "mentorpi-tank-operator.service",
    "mentorpi-tank-lifecycle.service",
    "mentorpi-tank-web.service",
    "mentorpi-tank-recover.service",
)
CONFIG = (
    Path("/etc/opt/ubuntu_tank/controller.yaml"),
    Path("/etc/opt/ubuntu_tank/web/web.yaml"),
    Path("/etc/opt/ubuntu_tank/security/keystore"),
    Path("/var/opt/ubuntu_tank/web/certs"),
)


def run(*args, timeout=120, check=True, env=None):
    """Run a bounded local command; propagate diagnostics and nonzero exit status."""
    result = subprocess.run(
        args, text=True, capture_output=True, timeout=timeout, check=False, env=env
    )
    if check and result.returncode:
        raise RuntimeError(
            f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def write(path, value, mode=0o644):
    """Atomically replace a generated document; never use this for lock files."""
    data = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        os.chmod(temporary, mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def target():
    """Require the supported real host and local rootful Docker before mutation."""
    if os.geteuid() != 0 or platform.machine() != "aarch64":
        raise RuntimeError("Requires root on the real ARM64 Pi 5; no host simulation")
    if "Raspberry Pi 5" not in Path("/proc/device-tree/model").read_text():
        raise RuntimeError("Requires Raspberry Pi 5")
    os_release = Path("/etc/os-release").read_text()
    if "ID=ubuntu" not in os_release or 'VERSION_ID="26.04"' not in os_release:
        raise RuntimeError("Requires Ubuntu 26.04")
    context = json.loads(run("docker", "context", "inspect"))[0]
    if context["Endpoints"]["docker"][
        "Host"
    ] != "unix:///var/run/docker.sock" or os.environ.get("DOCKER_HOST"):
        raise RuntimeError(
            "Requires default local Docker socket with no DOCKER_HOST override"
        )
    info = json.loads(run("docker", "info", "--format", "{{json .}}"))
    if any("rootless" in x or "userns" in x for x in info["SecurityOptions"]):
        raise RuntimeError(
            "Rootless Docker and user namespace remapping are unsupported"
        )
    run("docker", "compose", "version")


@contextlib.contextmanager
def locked():
    """Serialize host transactions on the retained native deployment-lock inode."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def device(name="/dev/rrc"):
    """Verify the currently enumerated RRC identity without opening the device."""
    path = Path(name).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISCHR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o660:
        raise RuntimeError("RRC must be a mode-0660 character device")
    props = dict(
        line.split("=", 1)
        for line in run(
            "udevadm", "info", "--query=property", f"--name={path}"
        ).splitlines()
        if "=" in line
    )
    if props.get("ID_VENDOR_ID") != "1a86" or props.get("ID_MODEL_ID") != "55d4":
        raise RuntimeError("RRC USB identity is not 1a86:55d4")
    identity = props.get("ID_SERIAL_SHORT") or props.get("ID_PATH")
    if not identity or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", identity):
        raise RuntimeError("RRC requires a stable serial or physical USB path")
    saved = STATE / "device.json"
    result = {
        "identity": identity,
        "property": "ID_SERIAL_SHORT" if props.get("ID_SERIAL_SHORT") else "ID_PATH",
    }
    if saved.exists() and json.loads(saved.read_text()) != result:
        raise RuntimeError("Reappeared RRC does not match the prepared device")
    return info, result


def holders():
    """Find every host process holding the RRC, failing closed on unreadable FDs."""
    dev = Path("/dev/rrc").stat().st_rdev
    found = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev == dev:
                        found.add(int(proc.name))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return sorted(found)


def containers(all_states=False):
    """Inspect local containers; all_states also includes stopped potential owners."""
    ids = run("docker", "ps", "-aq" if all_states else "-q").split()
    return json.loads(run("docker", "inspect", *ids)) if ids else []


def exclusive(allow_pair=False):
    """Reject legacy services, factory stacks and containers with hardware access."""
    for unit in NATIVE:
        if run("systemctl", "is-active", unit, check=False) in (
            "active",
            "activating",
            "deactivating",
        ):
            raise RuntimeError(f"Native unit is still active: {unit}")
        if run("systemctl", "is-enabled", unit, check=False) != "masked":
            raise RuntimeError(f"Native unit is not masked: {unit}; run prepare-host")
    for item in containers(all_states=True):
        labels = item["Config"].get("Labels") or {}
        if allow_pair and labels.get("com.docker.compose.project") == "ubuntu-tank":
            continue
        host = item["HostConfig"]
        if (
            host["Privileged"]
            or host.get("Devices")
            or host.get("DeviceCgroupRules")
            or item["Name"].lstrip("/") in ("MentorPi", "MentorPiFan")
            or any(
                m.get("Source", "").startswith("/dev") or m.get("Source") == "/"
                for m in item.get("Mounts", [])
            )
        ):
            raise RuntimeError(f"Conflicting container: {item['Name']}")
    if not allow_pair and holders():
        raise RuntimeError(f"RRC still held by host PIDs {holders()}")


def runtime_paths():
    """Create ephemeral IPC and a stable owner lock without replacing held inodes."""
    for path, uid, mode in (
        (RUN, 0, 0o755),
        (RUN / "ipc", 10001, 0o700),
        (RUN / "owner", 0, 0o755),
    ):
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, uid)
        os.chmod(path, mode)
    fd = os.open(
        RUN / "owner/owner.lock", os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o644
    )
    os.fchmod(fd, 0o644)
    os.close(fd)


def hashes():
    """Bind retained configuration, TLS and SROS2 file bytes to deployment evidence."""
    result = {}
    for root in CONFIG:
        if not root.exists():
            raise RuntimeError(f"Provision retained configuration first: {root}")
        for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if path.is_symlink():
                raise RuntimeError(f"Configuration symlinks unsupported: {path}")
            if path.is_file():
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_release(path):
    """Validate the generated manifest identity and C4-compatible immutable pair."""
    release = json.loads(path.read_text())
    identity = release.pop("release_id")
    if (
        hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()
        != identity
    ):
        raise RuntimeError("Release manifest hash mismatch")
    release["release_id"] = identity
    if (
        release["format_version"] != 1
        or release["platform"] != "linux/arm64"
        or release["state_format"] != "ephemeral"
    ):
        raise RuntimeError("Unsupported release format/platform/state")
    if (
        "ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/deployment.py"
        not in release["inputs"]
    ):
        raise RuntimeError(
            "Release predates the C4 admission gate; rebuild both images"
        )
    if set(release["images"]) != {"runtime", "web"}:
        raise RuntimeError("Release requires exactly runtime and web")
    for image in release["images"].values():
        for key in ("digest", "image_id"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image[key]):
                raise RuntimeError(f"Invalid image {key}")
    return release


def stage(path):
    """Return the verified release and local IDs without changing running processes.

    Config digests bind portable content; local Docker IDs bind execution. Tags
    only discover candidates. Installed manifests and configuration must match.
    """
    release = load_release(path)
    images = {
        role: resolve_image(image, STATE / "image-identities")
        for role, image in release["images"].items()
    }
    for role, image in release["images"].items():
        cid = run(
            "docker",
            "create",
            "--network=none",
            "--entrypoint=/bin/true",
            images[role],
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                run("docker", "cp", f"{cid}:/opt/ubuntu_tank/manifests/.", directory)
                actual = {
                    p.name: json.loads(p.read_text())
                    for p in Path(directory).glob("*.json")
                }
                if (
                    actual != image["dependencies"]
                    or actual["build-identity.json"]["context_sha256"]
                    != release["context_sha256"]
                ):
                    raise RuntimeError(f"Installed manifests differ: {role}")
        finally:
            run("docker", "rm", cid)
    validate_configuration(release, images)
    return release, images


def validate_configuration(release, images):
    """Run installed parsers and TLS validation in isolated images, with no hardware.

    Mounted input is read-only. This checks actual container readability and schema
    compatibility before the current pair is interrupted. Cryptographic identities
    are retained, never generated by staging.
    """
    hashes()
    common = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--entrypoint=python3",
    ]
    for role in ("runtime", "web"):
        mounts = []
        roots = (CONFIG[0], CONFIG[2]) if role == "runtime" else (CONFIG[1], CONFIG[3])
        for root in roots:
            mounts += ["--mount", f"type=bind,src={root},dst={root},readonly"]
        if role == "runtime":
            code = (
                'import sys,pathlib,yaml; sys.path.insert(0,"/opt/ubuntu_tank/current/scripts"); '
                "from config_migration import validate_config,SCHEMA_VERSIONS; "
                'v,e=validate_config(yaml.safe_load(open("/etc/opt/ubuntu_tank/controller.yaml"))); '
                "assert v,e; "
                'root=pathlib.Path("/etc/opt/ubuntu_tank/security/keystore/enclaves/ubuntu_tank"); '
                '[(root / name / leaf).read_bytes() for name in ("operator","controller","guard","bridge") '
                'for leaf in ("cert.pem","key.pem","permissions.p7s","governance.p7s","identity_ca.cert.pem","permissions_ca.cert.pem")]; '
                "import json; print(json.dumps(SCHEMA_VERSIONS))"
            )
            versions = json.loads(run(*common, *mounts, images[role], "-c", code))
            if versions != release["configuration_versions"]:
                raise RuntimeError("Configuration versions differ from release")
        else:
            code = (
                "import json; from ubuntu_tank_web.entrypoint import load_config_from_yaml; "
                "from ubuntu_tank_web.tls import validate_tls_certificate; "
                "from ubuntu_tank_protocol.constants import SUPPORTED_PROTOCOL_VERSIONS,SCHEMA_VERSION; "
                'c=load_config_from_yaml("/etc/opt/ubuntu_tank/web/web.yaml"); '
                "validate_tls_certificate(cert_path=c.tls_cert_path,key_path=c.tls_key_path); "
                "print(json.dumps([SUPPORTED_PROTOCOL_VERSIONS,SCHEMA_VERSION]))"
            )
            versions = json.loads(run(*common, *mounts, images[role], "-c", code))
            if versions != [release["protocol_versions"], release["schema_version"]]:
                raise RuntimeError("Protocol versions differ from release")


def compose(env, *args):
    """Invoke the installed Compose file with a generated immutable environment."""
    with tempfile.NamedTemporaryFile(mode="w") as stream:
        stream.write("".join(f"{k}={v}\n" for k, v in env.items()))
        stream.flush()
        return run(
            "docker",
            "compose",
            "--project-name",
            "ubuntu-tank",
            "--env-file",
            stream.name,
            "-f",
            str(INSTALL / "compose.yaml"),
            *args,
            env={k: v for k, v in os.environ.items() if k not in env},
        )


def observed_pair():
    """Return running containers in this project indexed by their service role."""
    result = {}
    for item in containers():
        labels = item["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") == "ubuntu-tank":
            role = labels.get("com.docker.compose.service")
            if role in result or role not in ("runtime", "web"):
                raise RuntimeError("Duplicate or unexpected project container")
            result[role] = item
    return result


def block():
    """Revoke control durably before any host transaction interrupts processes."""
    (CONTROL / "ready.json").unlink(missing_ok=True)
    fd = os.open(CONTROL, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)


def stop_pair():
    """Request controller Stop, then remove the full pair even if IPC is hung."""
    block()
    items = observed_pair()
    if "runtime" in items and not items["runtime"]["State"]["Paused"]:
        try:
            run(
                "docker",
                "exec",
                items["runtime"]["Id"],
                "runtime-entrypoint",
                "python3",
                "-c",
                'import sys; sys.path.insert(0,"/opt/ubuntu_tank/container"); from healthcheck import request; request("/run/ubuntu_tank/lifecycle.sock", {"action":"stop"})',
                timeout=4,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            pass  # Container shutdown is the bounded fallback; admission stays closed.
    ids = run(
        "docker",
        "ps",
        "-aq",
        "--filter",
        "label=com.docker.compose.project=ubuntu-tank",
    ).split()
    if ids:
        # Disable restart before stopping so an interrupted stop cannot resume control.
        run("docker", "update", "--restart=no", *ids)
        for item in items.values():
            if item["State"]["Paused"]:
                # Do not resume queued control work just to request shutdown.
                run("docker", "kill", "--signal=KILL", item["Id"])
        run("docker", "stop", "--time=5", *ids, timeout=30)
        run("docker", "rm", *ids)


def verify(release, env, images):
    """Check the actual pair and stopped/disarmed IPC; never acquire or arm."""
    pair = observed_pair()
    if set(pair) != {"runtime", "web"}:
        raise RuntimeError("Both containers must be running")
    for role, item in pair.items():
        if (
            item["Image"] != images[role]
            or env.get(role.upper() + "_IMAGE") != images[role]
            or item["State"]["Paused"]
        ):
            raise RuntimeError(f"Unexpected image or paused service: {role}")
        actual_env = dict(x.split("=", 1) for x in item["Config"]["Env"] if "=" in x)
        for key, value in {
            "UBUNTU_TANK_RELEASE_ID": env["RELEASE_ID"],
            "UBUNTU_TANK_DEPLOYMENT_TOKEN": env["DEPLOYMENT_TOKEN"],
            "UBUNTU_TANK_CONTEXT_SHA256": env["CONTEXT_SHA256"],
        }.items():
            if actual_env.get(key) != value:
                raise RuntimeError(f"Mismatched pair identity: {role}")
        if (
            not item["HostConfig"]["ReadonlyRootfs"]
            or item["Config"]["User"] != "10001:10001"
        ):
            raise RuntimeError(f"Unexpected confinement: {role}")
        mounted = {entry["Destination"]: entry for entry in item["Mounts"]}
        if mounted["/run/ubuntu_tank-deployment"]["RW"] or mounted["/run/ubuntu_tank"][
            "RW"
        ] != (role == "runtime"):
            raise RuntimeError(f"Incorrect admission/IPC mount access: {role}")
        devices = item["HostConfig"].get("Devices") or []
        rules = item["HostConfig"].get("DeviceCgroupRules") or []
        if role == "web" and (devices or rules or "/dev/bus/usb" in mounted):
            raise RuntimeError("Web must not have any hardware mapping")
        if role == "runtime" and (
            rules != ["c 189:* rw"]
            or mounted.get("/dev/bus/usb", {}).get("Source") != str(camera_devices.ROOT)
            or mounted.get("/dev/bus/usb", {}).get("RW") is not False
            or not devices
            or not {d.get("PathInContainer") for d in devices}.issubset(
                {"/dev/rrc", "/dev/video0", "/dev/null"}
            )
            or not any(d.get("PathInContainer") == "/dev/rrc" for d in devices)
            or item["HostConfig"]["NetworkMode"] != "none"
        ):
            raise RuntimeError("Runtime device/network isolation differs")
        command = ["runtime-entrypoint"] if role == "runtime" else []
        run(
            "docker",
            "exec",
            item["Id"],
            *command,
            "python3",
            "/opt/ubuntu_tank/container/healthcheck.py",
            role,
            timeout=10,
        )
    # Probe from web across its read-only IPC mount, exercising the real consumer.
    evidence = json.loads(
        run(
            "docker",
            "exec",
            pair["web"]["Id"],
            "python3",
            "-c",
            'import sys,json; sys.path.insert(0,"/opt/ubuntu_tank/container"); from healthcheck import request; from ubuntu_tank_protocol.ipc_client import OperatorIpcClient; '
            'c=OperatorIpcClient(); s=c.get_status(); c.close(); print(json.dumps({"operator":s,"lifecycle":request("/run/ubuntu_tank/lifecycle.sock",{"action":"status"})}))',
        )
    )
    status = evidence["operator"]
    if (
        evidence["lifecycle"].get("state") != "inactive"
        or status.get("operator_state") != "NO_OWNER"
        or status.get("guard_armed") is True
        or status.get("active_owner") is not None
        or status.get("release_id") != release["release_id"]
    ):
        raise RuntimeError("Replacement is not stopped/disarmed without an owner")
    if holders():
        raise RuntimeError("Stopped deployment still has serial holders")
    # A stopped runtime must nevertheless own the lifetime hardware lock.
    with (RUN / "owner/owner.lock").open() as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError("Runtime hardware-owner lock is not held")
    return evidence


def deploy(path):
    """Stage then stop/replace atomically under host lock; admit only after checks."""
    release, images = stage(path)
    with locked():
        block()
        try:
            device_info, _ = device()
            exclusive(allow_pair=True)
            stop_pair()
            exclusive()
            runtime_paths()
            with (RUN / "owner/owner.lock").open() as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            camera_serial = camera_devices.refresh()
            config = hashes()
            # Device mappings retain host ownership. Grant the camera's numeric
            # group independently of the motor controller's serial group.
            camera = Path("/dev/video0")
            camera_info = camera.stat() if camera.exists() else None
            if camera_info is not None and not stat.S_ISCHR(camera_info.st_mode):
                raise RuntimeError("Camera must be a character device")
            env = {
                "RUNTIME_IMAGE": images["runtime"],
                "WEB_IMAGE": images["web"],
                "SERIAL_GID": str(device_info.st_gid),
                "RELEASE_ID": release["release_id"],
                "CONTEXT_SHA256": release["context_sha256"],
                "DEPLOYMENT_TOKEN": uuid.uuid4().hex,
                "CAMERA_BACKEND": "aurora" if camera_serial else "v4l2",
                "CAMERA_SERIAL": camera_serial,
                "CAMERA_GID": str(camera_info.st_gid if camera_info else 10001),
                "CAMERA_DEVICE": (
                    "/dev/video0:/dev/video0:rw"
                    if camera_info is not None
                    else "/dev/null:/dev/null:rw"
                ),
            }
            write(STATE / "release.json", release)
            write(STATE / "environment.json", env)
            write(
                CONTROL / "selected.json",
                {"token": env["DEPLOYMENT_TOKEN"], "release_id": release["release_id"]},
            )
            compose(
                env,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "--remove-orphans",
            )
            deadline = time.monotonic() + 45
            while True:
                try:
                    evidence = verify(release, env, images)
                    break
                except (RuntimeError, subprocess.TimeoutExpired):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            if hashes() != config or device()[0].st_rdev != device_info.st_rdev:
                raise RuntimeError("Configuration or device changed during deployment")
            exclusive(allow_pair=True)
            approval = {
                "token": env["DEPLOYMENT_TOKEN"],
                "release_id": release["release_id"],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }
            write(
                STATE / "deployment.json",
                dict(approval, configuration_hashes=config, evidence=evidence),
            )
            write(CONTROL / "ready.json", approval)
            print(f"Deployed {release['release_id']}: stopped, disarmed, no owner")
        except BaseException:
            block()
            try:
                stop_pair()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(
                    f"Cleanup failed; control remains blocked: {error}", file=sys.stderr
                )
            raise


def prepare(serial_device):
    """Provision identity, observed-device udev, cutover masks and shutdown unit.

    Requires existing controller/web configuration, TLS and signed SROS2 keys. Saves
    original unit files, enablement and ownership for manual Git fallback; never
    starts the new containers or generates/replaces cryptographic identities.
    """
    for name in HOST_FILES:
        if not (HERE / name).is_file():
            raise RuntimeError(
                f"Incomplete host tools: missing {name}; copy the full host-tool bundle"
            )
    with locked():
        hashes()
        if not shutil.which("logrotate"):
            raise RuntimeError("Install the host logrotate prerequisite first")
        info, identity = device(serial_device)
        STATE.mkdir(parents=True, exist_ok=True)
        CONTROL.mkdir(exist_ok=True)
        os.chown(CONTROL, 0, 0)
        os.chmod(CONTROL, 0o755)
        block()
        backup = STATE / "host-backup"
        backup.mkdir(exist_ok=True)
        record_path = backup / "changes.json"
        if record_path.exists():
            record = json.loads(record_path.read_text())
        else:
            record = {
                "units": {},
                "ownership": {},
                "device_rule_existed": Path(
                    "/etc/udev/rules.d/99-mentorpi-rrc.rules"
                ).exists(),
            }
            for unit in NATIVE:
                record["units"][unit] = {
                    "enabled": run("systemctl", "is-enabled", unit, check=False),
                    "active": run("systemctl", "is-active", unit, check=False),
                }
                src = Path("/etc/systemd/system") / unit
                if src.is_file() or src.is_symlink():
                    shutil.copy2(src, backup / unit, follow_symlinks=False)
            rule = Path("/etc/udev/rules.d/99-mentorpi-rrc.rules")
            if rule.exists():
                shutil.copy2(rule, backup / rule.name)
            for root in CONFIG:
                for path in [root, *root.rglob("*")] if root.is_dir() else [root]:
                    st = path.stat()
                    record["ownership"][str(path)] = [
                        st.st_uid,
                        st.st_gid,
                        stat.S_IMODE(st.st_mode),
                    ]
            write(record_path, record, 0o600)
        for unit in NATIVE:
            run("systemctl", "stop", unit, check=False)
            run("systemctl", "disable", unit, check=False)
            path = Path("/etc/systemd/system") / unit
            if path.exists() or path.is_symlink():
                path.unlink()
            path.symlink_to("/dev/null")
        run("systemctl", "daemon-reload")
        if serial_device != "/dev/rrc":
            # The initial device may not yet have its persistent alias.
            alias = Path("/dev/rrc")
            if alias.exists() and alias.resolve() != Path(serial_device).resolve():
                raise RuntimeError("Existing RRC alias points at a different device")
            if not alias.exists():
                alias.symlink_to(Path(serial_device).resolve())
        exclusive()
        try:
            account = pwd.getpwuid(10001)
            if (
                account.pw_name != "ubuntu-tank-container"
                or account.pw_gid != 10001
                or grp.getgrgid(10001).gr_name != "ubuntu-tank-container"
            ):
                raise RuntimeError("UID 10001 belongs to another account")
        except KeyError:
            try:
                group = grp.getgrgid(10001)
                if group.gr_name != "ubuntu-tank-container":
                    raise RuntimeError("GID 10001 belongs to another group")
            except KeyError:
                run("groupadd", "--gid", "10001", "ubuntu-tank-container")
            run(
                "useradd",
                "--uid",
                "10001",
                "--gid",
                "10001",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "ubuntu-tank-container",
            )
        for path in record["ownership"]:
            item = Path(path)
            os.chown(item, 10001, 10001)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        runtime_paths()
        logs = Path("/var/opt/ubuntu_tank/ros-log")
        logs.mkdir(parents=True, exist_ok=True)
        os.chown(logs, 10001, 10001)
        os.chmod(logs, 0o700)
        write(STATE / "device.json", identity)
        group = grp.getgrgid(info.st_gid).gr_name
        write(
            Path("/etc/udev/rules.d/99-mentorpi-rrc.rules"),
            f'SUBSYSTEM=="tty", ATTRS{{idVendor}}=="1a86", ATTRS{{idProduct}}=="55d4", ENV{{{identity["property"]}}}=="{identity["identity"]}", SYMLINK+="rrc", GROUP="{group}", MODE="0660"\n',
        )
        run("udevadm", "control", "--reload-rules")
        run(
            "udevadm",
            "trigger",
            "--action=change",
            "--subsystem-match=tty",
            f"--sysname-match={Path(serial_device).resolve().name}",
        )
        run("udevadm", "settle")
        device()
        INSTALL.mkdir(parents=True, exist_ok=True)
        for name in HOST_FILES:
            if (HERE / name).resolve() != (INSTALL / name).resolve():
                shutil.copy2(HERE / name, INSTALL / name)
        shutil.copy2(
            HERE / "ubuntu-tank-container.service",
            "/etc/systemd/system/ubuntu-tank-container.service",
        )
        write(
            Path("/etc/tmpfiles.d/ubuntu-tank-container.conf"),
            "d /var/opt/ubuntu_tank/ros-log 0700 10001 10001 14d\n",
        )
        write(
            Path("/etc/logrotate.d/ubuntu-tank-container"),
            "/var/opt/ubuntu_tank/ros-log/*/*.log {\n  daily\n  maxsize 10M\n  rotate 4\n  missingok\n  notifempty\n  copytruncate\n  su ubuntu-tank-container ubuntu-tank-container\n}\n",
        )
        run("systemctl", "daemon-reload")
        camera_devices.refresh()
        write(
            Path("/etc/udev/rules.d/99-ubuntu-tank-camera.rules"),
            'SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", RUN+="/usr/bin/python3 /opt/ubuntu_tank-container/camera_devices.py"\n',
        )
        run("udevadm", "control", "--reload-rules")
        run("systemctl", "enable", "ubuntu-tank-container.service")
        print(
            f"Host prepared; retained identity/configuration. Manual undo record: {record_path}"
        )


def setup_tls(release_path, hostnames, addresses):
    """Provision first-use TLS and exact browser origins while applications are off.

    Verify the loaded web image from release_path, then run its TLS/YAML consumers
    against disposable copies. Existing pairs are validated and preserved; missing
    pairs are created as UID 10001. Refuse live applications and partial/invalid
    identities. Never start applications or replace an existing key. An interrupted
    two-file installation fails closed and requires restoring a complete pair.
    """
    with locked():
        if observed_pair():
            raise RuntimeError("Stop the container pair before setup-tls")
        for unit in NATIVE:
            if run("systemctl", "is-active", unit, check=False) in (
                "active",
                "activating",
                "deactivating",
            ):
                raise RuntimeError(f"Stop native applications before setup-tls: {unit}")
        release = load_release(release_path)
        image = resolve_image(release["images"]["web"], STATE / "image-identities")
        config, cert_root = CONFIG[1], CONFIG[3]
        for root in (config, cert_root):
            if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
                raise RuntimeError("Configuration/certificate symlinks are unsupported")
        with tempfile.TemporaryDirectory(prefix="ubuntu-tank-tls-") as directory:
            scratch = Path(directory)
            shutil.copy2(config, scratch / "web.yaml")
            if cert_root.exists():
                shutil.copytree(cert_root, scratch / "certs")
            else:
                (scratch / "certs").mkdir()
            for path in (scratch, *scratch.rglob("*")):
                os.chown(path, 10001, 10001)
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            result = json.loads(
                run(
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--user=10001:10001",
                    "--entrypoint=python3",
                    "--mount",
                    f"type=bind,src={scratch},dst=/setup",
                    "--mount",
                    f"type=bind,src={HERE / 'tls_setup.py'},dst=/tls_setup.py,readonly",
                    image,
                    "/tls_setup.py",
                    json.dumps({"hostnames": hostnames, "addresses": addresses}),
                )
            )
            generated = []
            for name in result["created"]:
                relative = Path(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                ):
                    raise RuntimeError("Invalid generated certificate path")
                destination = cert_root / relative
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError("Refusing to overwrite an existing TLS file")
                generated.append((scratch / "certs" / relative, destination))
            updated_config = (scratch / "web.yaml").read_text()
            if generated or updated_config != config.read_text():
                if CONTROL.exists():
                    block()
                for source, destination in generated:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    for parent in destination.parents:
                        if parent == cert_root.parent:
                            break
                        os.chown(parent, 10001, 10001)
                        os.chmod(parent, 0o700)
                    fd = os.open(
                        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(source.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chown(destination, 10001, 10001)
                if updated_config != config.read_text():
                    write(config, updated_config, 0o600)
                    os.chown(config, 10001, 10001)
            print(
                "TLS identity created"
                if generated
                else "Existing TLS identity preserved"
            )
            print("Browser origins: " + ", ".join(result["origins"]))
            print(
                "Trust server.crt on browser devices separately; then run stage and deploy."
            )


def main():
    """Dispatch explicit host operations; no implicit build, deployment or motion."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("stage", "deploy"):
        sub.add_parser(command).add_argument("release", type=Path)
    sub.add_parser("prepare-host").add_argument(
        "--serial-device",
        default="/dev/rrc",
        help="Observed RRC tty for first provisioning, e.g. /dev/ttyACM0",
    )
    sub.add_parser("target-test").add_argument(
        "--redeploy",
        action="store_true",
        help="Explicitly stop and redeploy the installed pair before verifying it",
    )
    tls = sub.add_parser(
        "setup-tls",
        help="Create missing TLS identity and add browser origins; applications must be stopped",
    )
    tls.add_argument(
        "--release",
        type=Path,
        default=Path("release.json"),
        help="Verified build release manifest (default: ./release.json)",
    )
    tls.add_argument(
        "--hostname",
        action="append",
        default=[],
        help="Browser DNS name; repeat for additional names",
    )
    tls.add_argument(
        "--ip",
        action="append",
        default=[],
        help="Browser IPv4/IPv6 address; repeat for additional addresses",
    )
    for command in ("boot", "stop", "status", "logs"):
        sub.add_parser(command)
    args = parser.parse_args()
    if args.command == "setup-tls" and not (args.hostname or args.ip):
        parser.error("setup-tls requires at least one --hostname or --ip")
    if args.command == "stage":
        release, _ = stage(args.release)
        print(f"Verified {release['release_id']}; no processes changed")
        return
    if args.command in ("status", "logs"):
        for role, item in observed_pair().items():
            print(role, item["Id"], item["State"])
            if args.command == "logs":
                subprocess.run(
                    ["docker", "logs", "--tail=100", item["Id"]], check=True, timeout=30
                )
        return
    target()
    if args.command == "prepare-host":
        prepare(args.serial_device)
    elif args.command == "setup-tls":
        setup_tls(args.release, args.hostname, args.ip)
    elif args.command in ("deploy", "boot"):
        if args.command == "boot" and not (STATE / "release.json").exists():
            return
        deploy(args.release if args.command == "deploy" else STATE / "release.json")
        if args.command == "deploy":
            # Activate shutdown ordering for a manual first deployment as well.
            run("systemctl", "start", "ubuntu-tank-container.service", timeout=240)
    elif args.command == "stop":
        with locked():
            stop_pair()
            if holders():
                raise RuntimeError("Serial ownership was not released")
    elif args.command == "target-test":
        prior_pair = None
        prior_hashes = None
        if args.redeploy:
            prior_pair = {role: item["Id"] for role, item in observed_pair().items()}
            prior_hashes = hashes()
            deploy(STATE / "release.json")
        with locked():
            release = load_release(STATE / "release.json")
            env = json.loads((STATE / "environment.json").read_text())
            images = {
                role: resolve_image(image, STATE / "image-identities")
                for role, image in release["images"].items()
            }
            exclusive(allow_pair=True)
            device()
            evidence = verify(release, env, images)
            if prior_pair is not None:
                current_pair = {
                    role: item["Id"] for role, item in observed_pair().items()
                }
                if (
                    set(prior_pair) != {"runtime", "web"}
                    or any(
                        current_pair[role] == prior_pair[role] for role in prior_pair
                    )
                    or hashes() != prior_hashes
                ):
                    raise RuntimeError(
                        "Redeployment did not replace both containers while retaining configuration"
                    )
                evidence["redeployed_from"] = prior_pair
                evidence["redeployed_to"] = current_pair
            previous = json.loads((STATE / "deployment.json").read_text())
            approval = json.loads((CONTROL / "ready.json").read_text())
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if approval != {
                "token": env["DEPLOYMENT_TOKEN"],
                "release_id": release["release_id"],
                "boot_id": boot_id,
            }:
                raise RuntimeError("Deployment admission is closed or stale")
            if hashes() != previous["configuration_hashes"]:
                raise RuntimeError("Installed configuration changed since deployment")
            report = dict(
                previous,
                evidence=evidence,
                images=release["images"],
                local_image_ids=images,
                run_id=uuid.uuid4().hex,
                status="PASS_STOPPED_INTEGRATION",
                physical_acceptance="PENDING",
            )
            write(STATE / "target-test.json", report)
            print(STATE / "target-test.json")


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
