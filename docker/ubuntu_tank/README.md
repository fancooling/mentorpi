# Ubuntu Tank container images (C3)

Builds the controller runtime and ROS-free web service for Ubuntu 26.04 ARM64
with ROS 2 Lyrical. This is separate from the factory `MentorPi` container and
`docker/customization`. C4 host preparation/cutover and C5 robot testing are still
required. Do not start this Compose project beside the factory or native stack.

## Build

Run from the repository root with its `.venv`. Initial supported baseline:
rootful Docker Engine 29.8 and Compose 5.5, without user-namespace remapping.
Both images use UID/GID **10001:10001**. The build requires an ARM64-capable
Buildx builder; emulation can validate packaging but cannot validate stop timing.

```bash
.venv/bin/python docker/ubuntu_tank/build.py \
  --output ubuntu_tank/.work/container-build
# Optional: --builder NAME for an existing native/remote/emulated builder.
# Optional: --context-only to stage inputs without calling Docker.
```

The output directory must be new. `build.py` copies an allowlist of authored
inputs, preserving vendor license notices. It excludes developer installations,
caches, factory sources, disk images, credentials and hardware configuration.
Untracked application files are excluded; new C3 build definitions are included
before commit. Inspect `context/build-identity.json` for exact input hashes.

The multistage Dockerfile builds two final targets:

- `runtime`: installed colcon workspace at `/opt/ubuntu_tank/current/install`,
  ROS, operator, guarded controller and Supervisor 4.3.0-1. Controller boot is
  stopped and disarmed; required processes do not automatically restart.
- `web`: installed protocol/web wheels, APT Python dependencies and compiled
  frontend from `npm ci`. It has no ROS or operator implementation. Uvicorn runs
  one worker on `0.0.0.0:8443`.

Base images are digest-pinned. `dependencies.json` splits builder, runtime and
web roots; `install_dependencies.py` applies `ubuntu_tank/versions.lock` and
checks downloaded package hashes before installation. Ubuntu dependencies resolve
against the [dated archive](https://snapshot.ubuntu.com/) at
`20260909T000000Z`. The dated ROS snapshot
2026-08-19 contains all 199 locked ROS packages. Its dedicated public signing key
is `ros-snapshot.asc`, fingerprint
`4B63CF8FDE49746E98FA01DDAD19BAB3CBF125EA`, retrieved from
[Ubuntu's keyserver](https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x4B63CF8FDE49746E98FA01DDAD19BAB3CBF125EA).
The [ROS snapshot repositories](https://discourse.ros.org/t/announcing-ros-snapshot-repositories/7705)
use a different signing key from the live ROS repository. APT signature checking
remains enabled. Unavailable or mismatched locked packages fail the build.

Each target retains dependency manifests under `/opt/ubuntu_tank/manifests`:
installed package versions, downloaded archive hashes, build identity and, for
web, the frontend integrity lock. Builder manifests remain distinguishable from
runtime dependencies. Container-only dependencies come from signed APT metadata;
no unpinned pip packages replace the native dependency closure. Only the two
application wheels are installed with pip, using `--no-index --no-deps`.

`release.json` is written only after both builds succeed. It binds both image
manifest digests and local image IDs to the source revision, exact context hash,
ARM64 platform, dependency manifests and existing protocol/configuration versions.
A dirty checkout is identified explicitly. Build tags are local transport handles,
not release identities. C4 must bind host configuration hashes and verify the
matching pair before enabling Start.

## Motor-free image verification

Build layers import installed ARM64 runtime modules and run installed CLI help.
The web layer checks that runtime imports are absent and constructs the real API.
After building, run:

```bash
.venv/bin/python docker/ubuntu_tank/smoke.py \
  ubuntu_tank/.work/container-build/release.json
```

This creates and removes uniquely named test containers and a disposable volume.
It publishes no ports and maps no hardware. The runtime fixture serves the real
installed IPC servers with a process double that reports a stopped controller
and rejects mutations. The production web entrypoint serves HTTPS and compiled
assets, connects through a read-only shared mount using UID 10001, and reconnects
after socket-server replacement. This verifies packaging and cross-container IPC;
it does not verify ROS/DDS, controller Start or physical motion.

On an x86 computer without host binfmt, `--emulator /absolute/path/to/buildkit-qemu-aarch64`
can use a trusted BuildKit emulator in only these containers. This does not install
or change host emulation. Build success alone does not imply Docker can execute
ARM64 containers outside its builder.

## Mounts and startup

`compose.yaml` defines only `runtime` and `web`, with read-only roots, dropped
capabilities, init/signal forwarding, five-second shutdown grace, bounded Docker
logs and private tmpfs directories. Runtime has no network interface beyond
loopback and receives only `/dev/rrc`; web publishes only 8443. Compose health
checks are informational and never acquire control, Start or Arm. A deliberately
stopped controller is healthy; an unavailable runtime does not prevent web startup.

C4 must prepare these paths before any cutover; Compose refuses to create missing
bind sources. Application containers never start as root to fix ownership.

| Host path | Required access |
|---|---|
| `/run/ubuntu_tank-container/ipc` | UID 10001, mode 0700; runtime writable, web read-only |
| `/run/ubuntu_tank-container/owner/owner.lock` | Stable readable regular file; runtime holds exclusive flock until exit; do not replace inode |
| `/etc/opt/ubuntu_tank/controller.yaml` | Runtime readable, immutable mount; existing controller format |
| `/etc/opt/ubuntu_tank/security/keystore` | Runtime readable; existing controller/guard/bridge/operator enclaves, no web access |
| `/etc/opt/ubuntu_tank/web/web.yaml` | Web readable; configure exact browser origins and retained certificate paths |
| `/var/opt/ubuntu_tank/web/certs` | Web readable certificate/key, immutable mount |
| `/var/opt/ubuntu_tank/ros-log` | UID 10001 writable; host retention/rotation required in C4 |

Private heartbeats and Supervisor RPC live in `/run/ubuntu_tank-private`, never
shared with web. Runtime validates configuration, mount ownership, device type
and its cooperating-owner lock before starting Supervisor. The lock cannot
exclude legacy processes: C4 must also verify actual serial holders and prevent
concurrent native/container startup. The five-second cleanup grace is not a
physical motor-stop bound.

TLS provisioning is a separate owner operation. Startup validates certificate
validity and key agreement without writing either file. Missing/invalid TLS or
web configuration fails startup. Preserve the existing identity across updates;
configure certificate paths below the mounted `/var/opt/ubuntu_tank/web/certs`.
`--no-tls` remains an explicit development-only CLI option.

Compose requires `RUNTIME_IMAGE` and `WEB_IMAGE` set to registry digest references
and `SERIAL_GID` set to the host's verified device group. It has no automatic
build or deployment step. C4 will implement pair verification and host deployment.
For terminal access after authorized deployment, source the installed environment:

```bash
docker compose -f docker/ubuntu_tank/compose.yaml exec runtime \
  runtime-entrypoint ros2 run ubuntu_tank_bringup operator_client --disarm
docker compose -f docker/ubuntu_tank/compose.yaml exec runtime \
  runtime-entrypoint ros2 run ubuntu_tank_teleop teleop_key
```

The normal clients use the existing operator IPC authority. Do not select the
`--direct-ros` diagnostic path for normal operation.

## Validation boundary

Development checks:

```bash
./ubuntu_tank/deploy.sh test
.venv/bin/python ubuntu_tank/tests/test_container_web.py -v
```

`test_container_web.py` uses real certificates and an HTTPS server to verify
mounted identity preservation and startup rejection of invalid configuration,
missing/corrupt/expired/mismatched TLS. Existing C1/C2 tests cover ROS-free installed
wheels and real child-process supervision. These results do not certify Pi 5 DDS,
serial ownership, container shutdown, pause/restart, browser-to-motor timing, or
any physical motion. Those remain separate C4/C5 gates.

For offline transport, save both local tags listed in `release.json` together
with that manifest, then load the archive on the ARM64 host:

```bash
docker image save -o images.tar RUNTIME_LOCAL_TAG WEB_LOCAL_TAG
docker image load -i images.tar
```

For registry transport, tag and push both images under the intended repository,
then record/verify their registry manifest digests against the build manifest.
Do not use `latest` as an identity or put registry credentials in build inputs.
Loading or pushing images does not authorize starting the robot; C4 supplies the
separate deployment operation and ownership checks.
