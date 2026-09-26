#!/usr/bin/env python3
"""Verify built ARM64 images over actual read-only shared IPC and HTTPS mounts.

Creates uniquely named, hardware-free containers and one disposable volume; removes
only those resources afterward. No ports are published and no device is mapped.
Use --emulator with a trusted buildkit-qemu-aarch64 binary on an x86 computer that
has no binfmt handler. Emulation is limited to these containers, never the host.
The runtime fixture serves real installed IPC code with a stopped-controller
process double. This is packaging evidence, not ROS/DDS or physical acceptance.
"""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from image_identity import resolve_image

ROOT = Path(__file__).resolve().parents[2]

INIT = r"""
import json, os, pathlib
from ubuntu_tank_web.tls import generate_self_signed_cert
root = pathlib.Path('/smoke')
(root / 'ipc').mkdir(mode=0o700)
(root / 'certs').mkdir(mode=0o700)
(root / 'deployment').mkdir(mode=0o755)
selected = dict(token=os.environ['UBUNTU_TANK_DEPLOYMENT_TOKEN'], release_id=os.environ['UBUNTU_TANK_RELEASE_ID'])
(root / 'deployment/selected.json').write_text(json.dumps(selected))
(root / 'deployment/ready.json').write_text(json.dumps(dict(selected, boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip())))
cert, key = generate_self_signed_cert('/smoke/certs/server.crt', '/smoke/certs/server.key')
(root / 'web.yaml').write_text(json.dumps(dict(tls_cert_path=cert, tls_key_path=key,
    operator_socket_path='/smoke/ipc/operator.sock', lifecycle_socket_path='/smoke/ipc/lifecycle.sock')))
for path in [root, *root.rglob('*')]: os.chown(path, 10001, 10001)
"""
PROBE = r"""
import importlib.util, json, os, ssl, stat, urllib.request
assert importlib.util.find_spec('rclpy') is None
assert importlib.util.find_spec('ubuntu_tank_operator') is None
from ubuntu_tank_protocol.ipc_client import OperatorIpcClient
from ubuntu_tank_protocol.deployment import admitted
assert admitted()
from ubuntu_tank_web.lifecycle_client import LifecycleClient
for name in ('operator', 'lifecycle'):
    assert stat.S_IMODE(os.stat('/smoke/ipc/' + name + '.sock').st_mode) == 0o600
with OperatorIpcClient('/smoke/ipc/operator.sock') as client:
    assert client.get_version()['protocol_version'] == '1.0.0'
    assert client.get_status()['active_owner'] is None
assert LifecycleClient('/smoke/ipc/lifecycle.sock').get_status()[:2] == (True, 'inactive')
context = ssl.create_default_context(cafile='/smoke/certs/server.crt')
for path in ('/', '/api/v1/version', '/api/v1/status'):
    with urllib.request.urlopen('https://127.0.0.1:8443' + path, context=context, timeout=5) as response:
        assert response.status == 200
        data = response.read()
        if path == '/': assert b'<html' in data.lower()
        if path == '/api/v1/status': assert json.loads(data)['service_state'] == 'inactive'
"""


def docker(*args: str) -> str:
    """Execute Docker and preserve diagnostics for any failed smoke operation."""
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=90
    ).strip()


def main() -> None:
    """Use exact image IDs from a completed build and verify socket recreation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path, help="build.py release.json")
    parser.add_argument(
        "--emulator", type=Path, help="Trusted buildkit-qemu-aarch64 executable"
    )
    args = parser.parse_args()
    release = json.loads(args.release.read_text())
    images = {key: resolve_image(value) for key, value in release["images"].items()}
    name = "mentorpi-c4-smoke-" + uuid.uuid4().hex[:12]
    runtime, web = name + "-runtime", name + "-web"
    mounts = []
    prefix = []
    if args.emulator:
        mounts = [
            "--mount",
            f"type=bind,src={args.emulator.resolve()},dst=/emulator,readonly",
        ]
        prefix = ["/emulator"]
    admission_env = []
    for key, value in {
        "UBUNTU_TANK_DEPLOYMENT_DIR": "/smoke/deployment",
        "UBUNTU_TANK_DEPLOYMENT_TOKEN": name,
        "UBUNTU_TANK_RELEASE_ID": release["release_id"],
        "UBUNTU_TANK_CONTEXT_SHA256": release["context_sha256"],
    }.items():
        admission_env += ["--env", f"{key}={value}"]
    common = [
        *admission_env,
        "--platform",
        "linux/arm64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:uid=10001,gid=10001,mode=1777",
        *mounts,
    ]

    def command(image, argv, options):
        executable = prefix[0] if prefix else argv[0]
        tail = argv if prefix else argv[1:]
        return docker("run", *options, "--entrypoint", executable, image, *tail)

    def start_runtime():
        command(
            images["runtime"],
            [
                "/bin/bash",
                "/usr/local/bin/runtime-entrypoint",
                "python3",
                "/fixture.py",
            ],
            [
                *common,
                "-d",
                "--name",
                runtime,
                "-v",
                f"{name}:/smoke",
                "--mount",
                f"type=bind,src={ROOT / 'ubuntu_tank/tests/fixtures/container_ipc.py'},dst=/fixture.py,readonly",
                "--env",
                "UBUNTU_TANK_PRIVATE_DIR=/tmp/private",
            ],
        )

    try:
        docker("volume", "create", name)
        # Only this disposable volume is initialized as root; production starts non-root.
        command(
            images["web"],
            ["/usr/bin/python3", "-c", INIT],
            [
                "--rm",
                "--platform",
                "linux/arm64",
                "--network",
                "none",
                "--user",
                "0:0",
                *admission_env,
                *mounts,
                "-v",
                f"{name}:/smoke",
            ],
        )
        # Production web must reject an old generation even with valid TLS.
        try:
            command(
                images["web"],
                [
                    "/bin/bash",
                    "/usr/local/bin/web-entrypoint",
                    "--config",
                    "/smoke/web.yaml",
                ],
                [
                    *common,
                    "--rm",
                    "-v",
                    f"{name}:/smoke:ro",
                    "--env",
                    "UBUNTU_TANK_DEPLOYMENT_TOKEN=obsolete",
                ],
            )
        except subprocess.CalledProcessError as exc:
            if "obsolete deployment" not in exc.output:
                raise
        else:
            raise RuntimeError("Web accepted an obsolete deployment generation")
        start_runtime()
        command(
            images["web"],
            [
                "/bin/bash",
                "/usr/local/bin/web-entrypoint",
                "--config",
                "/smoke/web.yaml",
            ],
            [*common, "-d", "--name", web, "-v", f"{name}:/smoke:ro"],
        )
        for attempt in range(2):
            deadline = time.monotonic() + 60
            while True:
                try:
                    docker("exec", web, *prefix, "/usr/bin/python3", "-c", PROBE)
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            if attempt == 0:
                docker("rm", "-f", runtime)
                start_runtime()
        print(
            "PASS: ARM64 installed web, HTTPS/static assets, ROS-free imports, same-UID IPC over read-only mounts, server recreation, obsolete pair rejection"
        )
    except subprocess.CalledProcessError as exc:
        print(exc.output)
        for container in (runtime, web):
            subprocess.run(["docker", "logs", container], check=False)
        raise
    finally:
        for container in (runtime, web):
            subprocess.run(
                ["docker", "rm", "-f", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["docker", "volume", "rm", name], check=False, stdout=subprocess.DEVNULL
        )


if __name__ == "__main__":
    main()
