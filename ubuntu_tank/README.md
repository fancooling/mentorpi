# MentorPi Ubuntu Tank Controller

Application source, configuration and development tests for the MentorPi Tank
on a Raspberry Pi 5 running Ubuntu 26.04 ARM64 and ROS 2 Lyrical. Production uses
paired runtime and web containers. Use the
[container guide](../docker/ubuntu_tank/README.md) for building, transferring,
deploying and operating those images.

Status as of 2026-09-29: M14.5 runtime/API and M14.6 simplified browser controls
are implemented and locally validated. These changes have not been deployed to the Pi. M14.4
stopped-controller integration passed on the Pi; M15 physical web acceptance
also supplies the outstanding container C5 evidence.

## Runtime structure

The runtime container owns ROS, the STM32 serial bridge, guarded controller,
operator authority and process supervision. It is the sole owner of `/dev/rrc`.
The web container serves the Vue PWA and FastAPI API without ROS or direct
hardware access. It talks to runtime Unix sockets through a shared IPC mount.

`ubuntu_tank_operator` arbitrates browser, terminal and bench ownership.
`ubuntu_tank_supervisor` supplies controller lifecycle operations and independent
process monitoring. The controller starts stopped, disarmed and ownerless;
restart never restores motion authority. Host admission binds the running pair
to the deployed release and current boot before permitting controller startup.

This Ubuntu deployment must not run beside the factory `MentorPi` stack, its
customization sidecar or the historical native controller services.

## Source layout

Paths below are relative to this directory. Container build and host deployment
code lives separately in [`docker/ubuntu_tank/`](../docker/ubuntu_tank/).

| Path | Purpose |
| --- | --- |
| `src/controller/` | Tank kinematics, odometry and controller support code adapted from the vendor source. |
| `src/ros_robot_controller/` | STM32 serial SDK and ROS bridge, including write/watchdog safety. |
| `src/ros_robot_controller_msgs/` | Reused vendor ROS interfaces. |
| `src/ubuntu_tank_bringup/` | Guarded ROS launch, operator/status clients and bench support. |
| `src/ubuntu_tank_safety/` | Motor guard, arming gates, command freshness and zero enforcement. |
| `src/ubuntu_tank_supervisor/` | Controller runner, lifecycle IPC and container process supervision. |
| `src/ubuntu_tank_operator/` | Shared ownership, acquisition/release coordination, input leases and ROS command arbitration. |
| `src/ubuntu_tank_protocol/` | ROS-free schemas, constants, configuration, IPC clients, deployment admission and OpenAPI generation. |
| `src/ubuntu_tank_teleop/` | Terminal keyboard input and renewable control leases. |
| `src/ubuntu_tank_web/` | FastAPI routes, WebSocket transport and runtime IPC relay. |
| `web/` | Vue/TypeScript PWA, Vite build and Playwright browser scenarios. |
| `config/` | Controller defaults, Fast DDS, SROS2 and shared web settings. |
| `bin/` | Installed runtime, operator, lifecycle and web entrypoint wrappers. |
| `scripts/` | Acceptance tools, target checks and retained native build/install utilities. |
| `host/` | Retained native service and host configuration assets; current host provisioning is under `docker/ubuntu_tank/`. |
| `tests/` | Hardware-free behavior tests, source/dependency gates and target-test contracts. |
| `docs/` | Dependency, protocol, supervision and historical release documentation. |
| `debug/` | Historical diagnosis records. |
| `deploy.sh` | Development test runner and help only. |
| `versions.lock` | Locked dependency versions and artifact hashes. |
| `VERSION` | Application release-version data. |

`.work/`, `dist/`, frontend `dist/` and dependency/cache directories hold local
or generated artifacts; they are not the maintained application source.

## Control contract

The current public API is `/api/v1`, with protocol `3.0.0` and schema version 3.
Older clients are rejected before acquiring control. The four public control
mutations are:

| Action | Route | Effect |
| --- | --- | --- |
| Take control | `POST /api/v1/control/acquire` | Start the controller if needed, verify readiness and acquire without arming or moving. |
| Start | `POST /api/v1/control/start` | Explicit owner-only arming through the existing safety gates; movement needs fresh directional input. |
| Stop | `POST /api/v1/control/stop` | Immediately zero/disarm, retaining ownership and the running controller. Available to observers too. |
| Release control | `POST /api/v1/control/release` | Cancel setup or zero/disarm, shut down the controller and release ownership after confirmed shutdown. |

Acquisition and release can return pending operations; clients poll the private
operation endpoint until completion or failure. Failed shutdown blocks driving
and preserves retry authority. Disconnect cleanup stops/disarms and revokes
ownership; it is not confirmation of a completed explicit Release operation.
Public `controller/start`, `controller/stop` and `control/arm` routes are removed;
internal safety and lifecycle operations remain.

Use Take control, then Start, then hold a direction to move. Releasing a direction
stops motion while remaining armed. The center button shows Stop while armed or
busy; Stop retains ownership. Space always stops, even on a focused Start button.
Release control cancels setup or shuts down the controller before confirming
release. A shutdown error leaves Release available for retry.

Switching tabs or losing window focus stops motion and disarms while retaining
ownership. Returning requires fresh status and an explicit Start before driving.
The ownership inactivity timer continues in the background (default five minutes);
expiry releases ownership and stops the controller. A real connection loss still
revokes control.

The browser polls status every second while visible and immediately on resume.
Failed or stale status disables driving and clears the local session. After
session loss or inactivity expiry, use Take control again; reconnect never
reacquires or starts automatically. Observers and unknown states offer Stop only.
A waiting PWA update appears in the dashboard. Update Now releases this session
and waits for confirmed shutdown before activating the worker and reloading;
failed shutdown leaves the current page available for retry.

Input expiry pauses motion. Browser recovery requires release of held controls,
fresh neutral acknowledgment and a new press; it never replays buffered motion.
Terminal and bench clients stop and require explicit rearming after a stall.
Focus loss, disconnect and hard faults still disarm.

Both containers load shared `config/web/web.yaml` settings from the installed
configuration. `lease_duration_sec` defaults to 1 second;
`control_idle_timeout_sec` defaults to 300 seconds. The latter releases ownership
and shuts down the controller after inactivity. Accepted owner actions renew
it; polling, challenges and automatic neutral traffic do not. Configuration
changes require the stopped deployment/restart procedure in the container guide;
existing valid installed overrides are preserved.

See the [controller and web interface reference](docs/CONTROL_INTERFACES.md),
[dependency and schema-generation guide](docs/WEB_DEPENDENCY_CLOSURE.md),
[generated OpenAPI](docs/openapi_v1.json) and
[web design](../docs/MENTORPI_WEB_CONTROL_DESIGN.md) for schemas, deadlines,
recovery rules and milestone acceptance criteria.

## Development and validation

Run commands below from the repository root. Use the single repository `.venv`
for local Python work; do not install dependencies into global Python or create
component virtual environments. Pi host deployment uses system Python as
specified in the container guide; container dependencies are image-managed.

```bash
# Hardware-free development checks, including frontend build and browser tests.
./ubuntu_tank/deploy.sh test

# Focused ownership lifecycle and input-recovery checks.
.venv/bin/python ubuntu_tank/tests/test_milestone145_control_lifecycle.py -v
.venv/bin/python ubuntu_tank/tests/test_milestone143_input_recovery.py -v

# Frontend checks using the existing locked Node dependencies.
npm --prefix ubuntu_tank/web run type-check
npm --prefix ubuntu_tank/web run build
```

The browser test harness starts local HTTP/IPC services and runs Playwright;
it requires local socket access and the Chrome executable configured in
`web/playwright.config.ts`. The full runner also checks safety, supervision,
protocol/API behavior and source/dependency boundaries. Tests using unavailable
ROS facilities may skip. Local results do not certify Pi services, serial/DDS
behavior or physical motor stopping.

## Build and deploy to Pi 5

### One command from the development computer

For an already provisioned Pi, run from the repository root with its SSH alias
from `~/.ssh/config`. The wrapper uses `python` from `PATH`: activate your virtual
environment first, or use an already configured global Python development environment.

```bash
./docker/ubuntu_tank/deploy.sh YOUR_PI_SSH_ALIAS

# Reuse a completed build; it is smoke-tested again before transfer.
./docker/ubuntu_tank/deploy.sh YOUR_PI_SSH_ALIAS \
  --build-dir ubuntu_tank/.work/build-m14-6

# Override the parent directory used to retain releases on the Pi.
./docker/ubuntu_tank/deploy.sh YOUR_PI_SSH_ALIAS \
  --remote-dir /home/ubuntu/robot-releases
```

Choose one command above. The default remote parent is `~/mentorpi-releases`
under the SSH user's home; each run creates a unique release subdirectory.
Relative overrides are also home-relative. The SSH user must be able to write
there and run sudo; passwords are requested through the remote terminal.

The wrapper builds both ARM64 images, smoke-tests them, exports and transfers the
images, manifest and five host tools, then runs Stop → prepare-host → stage →
deploy → target-test → status on the Pi. Existing configuration and certificates
are retained. It finishes stopped, disarmed and ownerless, without motor tests.
The Pi must already have the installed C4 CLI and prerequisites listed in step 3.
The wrapper uses the transferred `install.py` to stop the old pair, so it can
upgrade a Pi whose installed CLI is still named `deploy.py`. Preparation installs
the new name and refreshes the systemd boot/stop commands.

Local output defaults to a unique directory under `ubuntu_tank/.work`.
`--output PATH` selects a new build directory; `--build-dir PATH` reuses a completed
one. The default builder is `mentorpi-c3`; override with `--builder NAME`.
On x86, the wrapper copies the emulator from that builder's first local container;
use `--emulator /absolute/path/to/buildkit-qemu-aarch64` for a remote builder or
another trusted emulator. Native ARM64 development hosts need no emulator.

Failures stop subsequent steps and retain local/remote files for diagnosis.
Stdout and stderr remain visible and are saved to `deploy-<run ID>.log` in the
selected build directory on exit, including failed runs. During execution, the
log is spooled to a temporary file beside that directory so a new build can
still require a nonexistent output directory. Each retry gets a separate log.
Ensure space for the exported images and transfer archive on the development
computer, and for the images and verification exports on the Pi. No cleanup or
rollback is automatic. After SSH interruption, check Pi status before retrying:
remote work may still be running. Success covers stopped integration only;
continue with step 4 for browser and raised-track acceptance.

The following steps describe the equivalent manual workflow.

### 1. Build and smoke-test on the development computer

Use the container workflow for image and target verification. On the current
x86_64 development computer, select the existing `mentorpi-c3` builder explicitly;
the default Docker builder fails with `exec /bin/sh: exec format error` when
executing ARM64 build steps. Run these commands from the repository root:

```bash
# Build paired ARM64 images into a new output directory.
build_output="ubuntu_tank/.work/container-build-$(date -u +%Y%m%dT%H%M%S%N)"
.venv/bin/python docker/ubuntu_tank/build.py \
  --builder mentorpi-c3 \
  --output "$build_output"

# Copy the builder's emulator to a reusable local tools directory.
emulator_path="$PWD/ubuntu_tank/.work/emulators/buildkit-qemu-aarch64"
mkdir -p "$(dirname "$emulator_path")"
docker cp buildx_buildkit_mentorpi-c30:/usr/bin/buildkit-qemu-aarch64 "$emulator_path"

# Verify the resulting images without mapping robot hardware.
.venv/bin/python docker/ubuntu_tank/smoke.py \
  "$build_output/release.json" \
  --emulator "$emulator_path"
```

The output directory must not already exist, including after a failed build;
rerun the `build_output` assignment for each retry. Run the smoke test in the
same shell so it uses that build's output directory.
The builder and its container name above refer to existing workstation resources.
On another computer, use its ARM64-capable Buildx builder and copy the emulator
from that builder's container. Native ARM64 hosts can skip the emulator copy and
omit `--emulator`. BuildKit emulation
does not make ordinary Docker containers ARM64-capable; the smoke-test option
uses the emulator only inside its test containers without changing host emulation.

### 2. Export and transfer the release

Continue in the same development-computer shell after the smoke test passes.
If resuming an existing build, set `build_output` to its directory first
(for example, `build_output=ubuntu_tank/.work/build-m14-6`). Do not generate a
new timestamp or rebuild just to export it. Run from the repository root.
Set `pi_host` to your Pi's SSH alias or `user@hostname`. Export both image tags
from the generated manifest and copy the host tools from the same checkout.
Run each command only after the preceding command succeeds:

```bash
pi_host="YOUR_PI_SSH_ALIAS"
pi_release_dir="mentorpi-releases/$(basename "$build_output")"

# Read the exact image tags from this build's manifest.
runtime_tag="$(.venv/bin/python -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["images"]["runtime"]["local_tag"])' \
  "$build_output/release.json")"
web_tag="$(.venv/bin/python -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["images"]["web"]["local_tag"])' \
  "$build_output/release.json")"

# Export both images into one archive; building alone does not create this file.
docker image save -o "$build_output/images.tar" "$runtime_tag" "$web_tag"

mkdir -p "$build_output/host-tools"
cp docker/ubuntu_tank/{install.py,image_identity.py,tls_setup.py,compose.yaml,ubuntu-tank-container.service} \
  "$build_output/host-tools/"
ssh "$pi_host" "mkdir -p '$pi_release_dir'"
scp -r "$build_output/images.tar" "$build_output/release.json" \
  "$build_output/host-tools" "$pi_host:$pi_release_dir/"
printf 'On the Pi, run: cd ~/%s\n' "$pi_release_dir"
ssh "$pi_host"
```

The transfer contains `images.tar`, `release.json` and the five files in
`host-tools/`. The Pi does not need the build context, runtime/web manifest
directories, build metadata or development-computer emulator.

### 3. Deploy on the Pi 5

This update procedure assumes the Pi already has Ubuntu 26.04 ARM64, Docker,
Compose, logrotate, `/dev/rrc`, controller/web configuration, TLS certificates
and a signed SROS2 keystore. For a new Pi, complete the
[host prerequisites and TLS setup](../docker/ubuntu_tank/README.md#host-preparation-and-deployment)
first. Keep the installed calibration, browser origins, certificates and keys;
do not overwrite them with development defaults.

In the Pi SSH session, run the `cd` command printed above, then the commands
below. Stop browser control before updating. The stop and preparation steps
interrupt the existing deployment; preparation also disables native services.
Use the Pi's system Python, without a virtual environment:

```bash
sudo docker image load -i images.tar

# Stop the existing pair before refreshing the installed host tools.
sudo /usr/bin/python3 host-tools/install.py stop
sudo /usr/bin/python3 host-tools/install.py prepare-host

sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py stage "$PWD/release.json"
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py deploy "$PWD/release.json"
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py target-test
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py status
```

Run each command only after the preceding command succeeds. Initial image
verification can take up to 15 minutes per image on slow storage and needs
temporary space for an uncompressed image export. If deployment fails, inspect
`sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py logs`, resolve the
error, then repeat deployment and verification.

### 4. Verify before browser driving

Require `PASS_STOPPED_INTEGRATION` from `target-test` and confirm the controller
is inactive, disarmed and ownerless. Evidence is saved to
`/var/lib/ubuntu_tank-container/target-test.json`. This verifies stopped services;
it does not certify motor behavior.

Open the Pi's configured HTTPS address on port 8443 and refresh/update the PWA.
Confirm the new Start / Stop controls are visible. Raised-track testing then
follows Take control → Start → direction/release → Stop → Release control;
Release must confirm controller shutdown. Follow the
[Milestone 15 checklist](../docs/MENTORPI_WEB_CONTROL_DESIGN.md#milestone-15--deployment-and-raised-track-web-acceptance)
for physical and network acceptance.

The legacy `ubuntu_tank/deploy.sh` no longer provides native build, install, start, arm, teleop or
acceptance commands.

Tests that move motors or use connected robot hardware require the explicit
target procedure and raised-track acknowledgment. Keep the power disconnect
accessible. On-ground motion remains unauthorized. Historical native acceptance
does not certify the container delivery or revised web-control contract.

## Source documentation and history

Maintained code/configuration documents its purpose and non-obvious behavior.
Vendor adaptations identify their origin and preserve copyright/license notices.
Git records source revisions; there is no source-manifest maintenance step.
Dependency and generated release checksums verify artifacts separately. Empty
ament resource markers and `VERSION` remain package/release data.

Historical native installation and acceptance are recorded in the
[native controller design](../docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md) and Git
history. Retained native scripts and service files do not constitute a supported
current deployment path. For current milestones and validation boundaries, use
the [web design](../docs/MENTORPI_WEB_CONTROL_DESIGN.md),
[container design](../docs/MENTORPI_CONTAINER_REFACTOR_DESIGN.md) and
[project checkpoint](../CONVERSATION_MEMORY.md).
