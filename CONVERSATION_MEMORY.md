# MentorPi Conversation Memory

Last updated: 2026-09-07

This is the repository-local handoff between sessions. Read `GEMINI.md` for the
current architecture and safety constraints; use `README.md` for commands.

## User intent

- Preserve the vendor Raspberry Pi OS, factory `MentorPi` container as a
  rollback baseline, and the host drivers/startup contract.
- Customize camera streaming, LiDAR presentation, telemetry, perception, and
  later robot behavior without rebuilding the vendor driver stack.
- Keep the implemented sidecar image free of controller and driving programs.
- Store enough context in the repository for future sessions to resume easily.
- Keep the implemented sidecar as the low-risk path while designing a separate,
  opt-in full-stack image that can replace `MentorPi` when behavior ownership
  is desired. The two robot stacks must never run concurrently.

## Current implementation

- Custom image: `mentorpi-fan:latest`.
- Container: `MentorPiFan`.
- Compose project: `mentorpi-fan`.
- Canonical Compose file: `docker/customization/docker-compose.yml`.
- Build context: `docker/customization/` only.
- Dashboard: port 8081.
- Factory services reused: camera HTTP on 8080 and rosbridge on 9090.
- The image contains Nginx and static assets only, with no ROS, drivers,
  controllers, teleoperation, navigation, or hardware access.
- Browser code emits only rosbridge `subscribe` and `unsubscribe`; sidecar port
  8081 explicitly returns 404 for `/ros` and `/ros/`.

Runtime isolation uses a non-root user, read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, a constrained `/tmp` tmpfs, and no
bind mounts, devices, or privileged mode.

The repository is tracked in Git on branch `main`. `.gitignore` excludes the
52 GB `.img` disk image, extracted `third_party_src/`, core dumps,
transient ROS 2/Python build artifacts, PyTorch model weights (`*.pt`), and
oversized CAD meshes (`wheel_link.stl`).

## Deployment workflow

The deployment script at `docker/customization/deploy.sh` operates only on
`mentorpi-fan`:

```bash
./docker/customization/deploy.sh local
./docker/customization/deploy.sh test
./docker/customization/deploy.sh remote pi@ROBOT_IP
```

Every command rebuilds from the narrow customization context, then validates
Compose isolation, image architecture, runtime user, observer labels, Nginx
configuration, and absence of ROS/control operations. Remote deployment
preflights gzip, Docker, Compose v2, and ARM64; requires the factory `MentorPi`
container to be running; verifies the transferred immutable image ID; and
checks runtime isolation. Failed local or remote health/isolation checks
preserve an existing container if it was not replaced; otherwise they remove
the failed container and restore the previous image tag when available. The
script deliberately does not restart an old image through potentially
incompatible new configuration. It does not install host files or manage the
factory container.

The built image lives in the local Docker daemon, not the repository. The
current validated build has image ID
`sha256:66874357c1ff2c446c8d776534fce0426d5b488cf27713f06711658e83215986`.
Nginx binds to all interfaces (0.0.0.0 and [::]) on port 8081 with host
networking and CORS enabled on /video/ for remote browser streaming.

## Retired legacy deployment

On 2026-09-05, the reconstructed privileged full-stack deployment was retired:

- Removed the old full-stack `docker/Dockerfile`.
- Replaced the privileged Compose definition with the sidecar-only definition,
  now at `docker/customization/docker-compose.yml`.
- Removed the duplicate `docker/docker-compose.fan.yml`.
- Removed legacy `host_setup/` and `container_env/` files.
- Rewrote the deployment script, now at `docker/customization/deploy.sh`; it no
  longer builds `mentorpi:latest`, installs udev/systemd files, mounts `/dev`, or
  starts a replacement `MentorPi`.
- Removed `SETUP_GUIDE.md`; root `README.md` is now canonical.

Vendor code in `mentorpi/src`, extracted code in `third_party_src`, and
`tools_and_models` were deliberately retained as reference material for future
customization. They are not included in the sidecar build context.

## Full-stack replacement design

Current-state correction (2026-09-07): `docker/original/` is absent from both
the worktree and Git index. The entries below record prior work, not an available
workflow. Do not run, reference, or reuse its guard/deployment files unless that
directory is deliberately restored and revalidated.

On 2026-09-05, `/mnt/rpi-rootfs` was re-audited as the authoritative source for
host setup, Docker metadata, and active container code. The resulting
`docs/MENTORPI_REPLACEMENT_CONTAINER_DESIGN.md` defines the full-stack
replacement requirements; it does not change the sidecar Compose/deployment.

On 2026-09-07, the independent `docker/original` workflow implemented an
opt-in `runtime-core` candidate: sanitized private source capture, digest-pinned
ARM64 multi-stage build, separate core/operator Compose files, non-root and
non-privileged runtime policy, compatibility helpers, a disarmed-by-default
motor freshness guard, runtime SBOM/provenance, immutable staging, factory
backup, acceptance-gated cutover, automatic failure restore, and explicit
rollback. The capture records both workspace revisions and dirty tracked-file
counts and excludes credentials, generated outputs, mutable robot data, and
unsupported optional tools from the build context.

The factory boot contract hard-codes container name `MentorPi` and launches ROS
with `docker exec`. Active configuration is MentorPi Tank V2.0.1 (2026-08-22),
ROS 2 Humble on Ubuntu 22.04 ARM64, MS200 lidar, Aurora depth camera, and ROS
domain 0. The active primary and third-party workspaces are opaque writable-
layer trees, so a fresh allowlisted extraction must preserve their working-tree
changes rather than use the imported base image or older repository copies.

Production gates include an exact live ROS/QoS baseline, resolution of
conflicting recorded `.typerc` bind paths, non-privileged device access, a
graceful zero-motor shutdown path, and a proven stale-command/STM32 watchdog.
No replacement cutover or on-ground motion is authorized until those safety
tests and a factory rollback drill pass.

## Validation history

- On 2026-09-07, `./docker/original/deploy.sh capture` completed against
  `/mnt/rpi-rootfs`, including source hashes, secret-pattern checks, package
  manifests, workspace revision metadata, and the disk-image checksum.
- The replacement's hardware-free `deploy.sh test` builds and inspects the real
  ARM64 image, validates Compose isolation and runtime provenance, loads ROS
  packages and launch files, runs motor-guard regressions, and verifies bounded
  signal shutdown without starting or replacing a `MentorPi` container. Record
  the final result and immutable image ID here only after the current test run
  completes.

- After `deploy.sh` and `docker-compose.yml` moved into
  `docker/customization/` on 2026-09-06, the deployment script's complete test
  command passed from the new path with the Compose context resolved to that
  directory.
- The ARM64 `mentorpi-fan` image built successfully and ran locally through
  QEMU on 2026-09-05.
- `MentorPiFan` reported healthy on port 8081.
- Runtime inspection showed `privileged=false`, `readonly=true`, user `nginx`,
  no binds/devices, `cap_drop=["ALL"]`, and `no-new-privileges=true`.
- Nginx configuration, JavaScript syntax, Compose rendering, and the health
  endpoint passed local checks.
- Camera and live LiDAR integration still require validation on a powered
  factory robot with hardware-backed ports 8080/9090.

## Native Ubuntu 26.04 motion controller design

On 2026-09-07, `docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md` was revised into a
design-only plan for a clean Ubuntu 26.04 Pi 5 with native ROS 2 Lyrical. The
first phase is controller-only: guarded forward, reverse, left, and right motion.
All authored source, configuration defaults, tests, deployment automation,
systemd/udev templates, and operator instructions live under a self-contained
repository `ubuntu_tank/` workspace. Production does not run from that checkout:
immutable root-owned releases install under
`/opt/ubuntu_tank/releases/<release-id>`, with
`/opt/ubuntu_tank/current` atomically selecting the active release. Host-specific
configuration lives under `/etc/opt/ubuntu_tank`, persistent mutable state under
`/var/opt/ubuntu_tank`, and volatile state under `/run/ubuntu_tank`. A dedicated
non-root `ubuntu-tank` account runs the systemd service. Install, activation,
rollback, start, and arming are separate operations; activation/rollback use a
write-ahead recovery journal and leave the service stopped and disarmed. No
implementation exists yet.

The design copies the complete `ros_robot_controller_msgs`,
`ros_robot_controller`, and the complete controller Python module directory from
`mentorpi/src` after comparing those candidates against authoritative active
copies under `/mnt/rpi-rootfs`; legacy controller launch surfaces that require
Nav2 or peripherals are excluded. No reusable guard exists in the current
worktree, so the design calls for one narrowly scoped new `ubuntu_tank_safety`
package plus a minimal AND-gating heartbeat supervisor. It adapts only the
relevant vendor keyboard logic into renewable, timeout-bounded motion leases.
Narrow porting changes remove environment and
absolute-path assumptions, complete dependency metadata, parameterize the serial
and kinematic contract, disable unused bridge and controller command endpoints,
expose read-only guard state, and add monotonic guard/bridge watchdogs. Production
discovery is localhost-only and distinct deny-by-default SROS2 enclaves protect
each command boundary. Camera, LiDAR, navigation, AI, joystick, Docker runtime,
and on-ground motion are outside this phase. Six trackable milestones
cover repository provenance, clean-host ROS installation, the Lyrical port,
guarded bringup, versioned native deployment, and raised-track acceptance.

## Codex continuity

- Codex local memories are enabled on this workstation with
  `[features] memories = true` in `~/.codex/config.toml`.
- `AGENTS.md`, `GEMINI.md`, and this file provide repository-specific continuity
  independent of workstation-local memory.
