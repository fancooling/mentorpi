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

Use the container workflow for image and target verification:

```bash
# Build paired ARM64 images into a new output directory.
.venv/bin/python docker/ubuntu_tank/build.py \
  --output ubuntu_tank/.work/container-build

# Verify the resulting images without mapping robot hardware.
.venv/bin/python docker/ubuntu_tank/smoke.py \
  ubuntu_tank/.work/container-build/release.json
```

An ARM64-capable Buildx builder is required. Image build/smoke, transfer/staging,
Pi deployment and physical acceptance are separate steps. Follow the
[container guide](../docker/ubuntu_tank/README.md) for host preparation,
configuration/TLS, staging, deployment and stopped `target-test` execution.
`deploy.sh` no longer provides native build, install, start, arm, teleop or
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
