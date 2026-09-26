# Ubuntu Tank containers (C3/C4)

Builds the controller runtime and ROS-free web service for Ubuntu 26.04 ARM64
with ROS 2 Lyrical. This is separate from the factory `MentorPi` container and
`docker/customization`. C4 Pi deployment, redeployment, reboot and power-on checks
passed; the owner waived other C4 fault tests. C5 robot testing remains required. Do not start this Compose project beside the factory or native stack.

## Build

Run from the repository root with its `.venv`. You may activate it once with
`source .venv/bin/activate` and use `python` for the development commands below.
Pi host operations use system Python as shown later. Initial supported baseline:
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
manifest digests and portable image configuration digests to the source revision, exact context hash,
ARM64 platform, dependency manifests and existing protocol/configuration versions.
A dirty checkout is identified explicitly. Build tags are local transport handles,
not release identities. C4 binds host configuration hashes and verifies the
matching pair before enabling Start. Rebuild C3 images for the C4 admission gate.

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
after socket-server replacement. It also rejects a web image started with an
obsolete deployment generation. This verifies packaging and cross-container IPC;
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

`prepare-host` prepares these paths before cutover; Compose refuses to create missing
bind sources. Application containers never start as root to fix ownership.

| Host path | Required access |
|---|---|
| `/run/ubuntu_tank-container/ipc` | UID 10001, mode 0700; runtime writable, web read-only |
| `/run/ubuntu_tank-container/owner/owner.lock` | Stable readable regular file; runtime holds exclusive flock until exit; do not replace inode |
| `/etc/opt/ubuntu_tank/controller.yaml` | Runtime readable, immutable mount; existing controller format |
| `/etc/opt/ubuntu_tank/security/keystore` | Runtime readable; existing controller/guard/bridge/operator enclaves, no web access |
| `/etc/opt/ubuntu_tank/web/web.yaml` | Web readable; configure exact browser origins and retained certificate paths |
| `/var/opt/ubuntu_tank/web/certs` | Web readable certificate/key, immutable mount |
| `/var/opt/ubuntu_tank/ros-log` | UID 10001 writable; daily logrotate and 14-day tmpfiles retention |

Private heartbeats and Supervisor RPC live in `/run/ubuntu_tank-private`, never
shared with web. Runtime validates configuration, mount ownership, device type
and its cooperating-owner lock before starting Supervisor. The lock cannot
exclude legacy processes: deployment checks host serial holders, rejects
conflicting containers and requires all native application services to be masked. The five-second cleanup grace is not a
physical motor-stop bound.

TLS provisioning is an explicit `setup-tls` host operation. Startup validates certificate
validity and key agreement without writing either file. Missing/invalid TLS or
web configuration fails startup. Preserve the existing identity across updates;
configure certificate paths below the mounted `/var/opt/ubuntu_tank/web/certs`.
`--no-tls` remains an explicit development-only CLI option.

The host CLI supplies verified immutable local Docker image IDs, the serial group,
release identity and deployment generation to Compose. Do not invoke Compose
`up` directly. Registry manifest digests remain bound in the release manifest.
For terminal access after authorized deployment, source the installed environment:

```bash
docker exec -it "$(docker ps -q --filter label=com.docker.compose.project=ubuntu-tank --filter label=com.docker.compose.service=runtime)" \
  runtime-entrypoint ros2 run ubuntu_tank_bringup operator_client --disarm
docker exec -it "$(docker ps -q --filter label=com.docker.compose.project=ubuntu-tank --filter label=com.docker.compose.service=runtime)" \
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
Loading or pushing images does not start the robot. Deployment is a separate
operation with ownership checks.

## Host preparation and deployment

Use a real Ubuntu 26.04 ARM64 Pi 5 with local rootful Docker, Compose, udev,
logrotate and system Python 3. The Pi does not need `.venv` for host operations;
`prepare-host` installs no ROS or Docker packages. Install those host prerequisites
explicitly before proceeding. Retain the existing controller calibration,
web configuration, TLS certificate/key and signed SROS2 keystore at the paths
above. First-time HTTPS provisioning uses `setup-tls` below; signed SROS2 provisioning
remains separate.

Load both images before staging, or pull their exact registry digest references.
Staging verifies portable configuration digests and installed dependency/build manifests,
then runs installed configuration parsers and TLS validation in isolated containers
without hardware. It never interrupts the running pair. Offline image IDs are
content digests; local tags are never used for activation. Registry transport must
preserve the built image configuration digests; rebuild the release if content changes.

The classic Docker store identifies an image by its configuration digest; the
containerd store may report a manifest digest instead. Offline save/load can also
reconstruct that manifest. The CLI resolves the transport tag, pins the local
immutable ID, and verifies the exported raw configuration against `release.json`.
It then uses that local ID for containers and records it in target evidence.
The original manifest digest remains build provenance, not a required local ID.

The first verification on containerd temporarily exports each image separately.
Allow free space in the system temporary directory for the largest uncompressed
image archive and up to 15 minutes per export on slow storage. Verified digest
pairs are cached under `/var/lib/ubuntu_tank-container/image-identities`; unchanged
images do not need another export at boot. Discovery also accepts the original
manifest digest and previously verified local IDs, so registry pulls and subsequent
boots do not require the original build tag.

For host-tool updates, transfer `deploy.py`, `image_identity.py`, `tls_setup.py`,
`compose.yaml` and `ubuntu-tank-container.service` together. `prepare-host` rejects
incomplete bundles before cutover and refreshes installed copies. Existing C4
release manifests and images remain valid.

For an empty HTTPS certificate directory, after loading the images and stopping
native/container applications, run from the directory containing `release.json`:

```bash
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py setup-tls \
  --hostname rpitank --hostname rpitank.local --ip YOUR_PI_LAN_IP
# Optional: --release /path/to/release.json; repeat --hostname/--ip as needed.
```

This uses the verified web image, without network or devices, to create the missing
pair and append exact HTTPS origins on port 8443 to existing `web.yaml`. Pi Python
needs no additional packages. The configured certificate/key paths must be below
`/var/opt/ubuntu_tank/web/certs`. Both missing files are created as UID/GID 10001;
existing valid pairs remain unchanged and must cover every requested name/IP.
Partial, expired, mismatched or invalid identities fail without automatic rotation.
The command requires applications to be stopped, does not deploy/start them, and
closes any old admission before changing configuration. Interrupted installation
can leave a partial pair; restore a complete pair before retrying. Trust the public
certificate on browser devices separately; never distribute the private key.

`prepare-host` may run before or after first-time TLS setup, but `stage` requires
valid certificates. After setup, run `stage`, `deploy` and `target-test`.

From the target repository checkout:

```bash
# Explicit cutover: stops/disables/masks the native application units.
# It preserves files and records their previous ownership and service state.
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py prepare-host
# First provisioning without /dev/rrc: append --serial-device /dev/ttyACM0

sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py stage /path/to/release.json
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py deploy /path/to/release.json
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py target-test
# Explicitly repeat production stop-and-replace, then verify the installed pair:
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py target-test --redeploy

sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py status
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py logs
sudo /usr/bin/python3 docker/ubuntu_tank/deploy.py stop
```

After preparation, the installed host CLI is also available as
`sudo /usr/bin/python3 /opt/ubuntu_tank-container/deploy.py COMMAND`.
`ubuntu-tank-container.service` provides boot/shutdown ordering around Docker;
application processes remain supervised inside runtime. A manual first deploy
activates that unit, which verifies/redeploys the selected pair once more.
Every boot or container restart leaves the controller stopped. Only explicit
browser/terminal Start followed by Arm can enable motion.

The host keeps the original deployment-lock inode and a separate lifetime
hardware-owner lock. Runtime holds the latter even when stopped. The bridge uses
exclusive serial open as a second cooperating-owner check. Host inspection still
rejects other device holders; neither lock can exclude noncooperating software.

Deployment closes a root-owned admission gate before Stop. The operator revokes
ownership and pending Arm, and the runner stops its ROS process group. Both old
containers are removed before replacements start. A paused container receives
SIGKILL directly; shutdown never explicitly unpauses its queued control work. Startup rejects obsolete
pair generations or image builds. Compose supplies
`UBUNTU_TANK_DEPLOYMENT_DIR` (read-only host admission directory),
`UBUNTU_TANK_DEPLOYMENT_TOKEN` (unique replacement generation),
`UBUNTU_TANK_RELEASE_ID` and `UBUNTU_TANK_CONTEXT_SHA256` to both images.
These values come from the host CLI; do not override them. The host verifies both images, HTTPS, IPC
across the read-only web mount, the hardware lock and stopped/disarmed state
before opening admission. Approvals are bound to the host boot ID. A crash or
interruption before approval leaves control unavailable. Re-run `deploy` to
recover; there is no automatic rollback or controller restart.

`target-test` requires an already deployed, stopped controller and never Starts
or Arms it. It writes `/var/lib/ubuntu_tank-container/target-test.json`, binding
image digests, configuration hashes, boot ID and run ID. Its PASS covers only the
stopped integration checks. C4 was accepted for the owner-reduced scope of Pi
deployment, redeployment, reboot and shutdown/power-on recovery. USB reconnect,
competing-owner and interrupted-update tests were explicitly waived, not passed.
C5 covers controller-running failure injection and physical timing;
a container `web-acceptance` command is not implemented yet. Native acceptance
does not certify Docker.

For serial reconnect, stop the pair, reconnect the same observed adapter/USB
path, verify `/dev/rrc`, and redeploy the release. This recreates the device mapping.
A different device identity is rejected. Never add a broad `/dev` mount.

Docker logs retain three 10 MiB files per container. Host ROS logs use daily
logrotate with a 10 MiB threshold/four rotations and 14-day tmpfiles cleanup.
Inspect capacity during Pi acceptance; these are retention policies, not a quota.

## Manual Git fallback

1. Stop/disarm and run the container `stop` command. Confirm no holder remains
   with `sudo fuser -v /dev/rrc`; do not continue if ownership is unresolved.
2. Disable/stop `ubuntu-tank-container.service`. Preserve configuration, TLS,
   keystore and `/var/lib/ubuntu_tank-container/host-backup`.
3. Revert the migration commits in Git to restore the native tooling. Git does
   not undo host provisioning.
4. Remove the six native unit masks. Restore saved unit files and the saved udev
   rule from `host-backup`. If the rule did not previously exist, remove the new
   rule. `changes.json` records original unit enablement, active state and each
   configuration path's UID/GID/mode; restore those permissions. Do not restore
   an active controller merely because it was active before cutover.
5. Remove `/etc/systemd/system/ubuntu-tank-container.service`,
   `/etc/logrotate.d/ubuntu-tank-container` and
   `/etc/tmpfiles.d/ubuntu-tank-container.conf`. Reload systemd and udev. Restore
   only the recorded unit enablement; leave the controller stopped/disarmed.
6. Redeploy with the restored tooling and verify stopped/disarmed state before
   any separate Start/Arm. The new account, logs and installed container CLI may
   remain inert; delete them only after deciding their evidence is no longer needed.

Do not remove or replace either lock inode while any process still holds it.
