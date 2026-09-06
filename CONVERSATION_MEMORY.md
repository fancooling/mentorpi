# MentorPi Conversation Memory

Last updated: 2026-09-05

This is the repository-local handoff between sessions. Read `GEMINI.md` for the
current architecture and safety constraints; use `README.md` for commands.

## User intent

- Preserve the vendor Raspberry Pi OS, factory `MentorPi` container, host
  drivers, STM32 driver, LiDAR driver, and camera driver.
- Customize camera streaming, LiDAR presentation, telemetry, perception, and
  later robot behavior without rebuilding the vendor driver stack.
- Keep the first customization image free of controller and driving programs.
- Store enough context in the repository for future sessions to resume easily.

## Current implementation

- Custom image: `mentorpi-fan:latest`.
- Container: `MentorPiFan`.
- Compose project: `mentorpi-fan`.
- Canonical Compose file: `docker/docker-compose.yml`.
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

The rewritten `docker/deploy.sh` operates only on `mentorpi-fan`:

```bash
./docker/deploy.sh local
./docker/deploy.sh test
./docker/deploy.sh remote pi@ROBOT_IP
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
- Replaced the privileged `docker/docker-compose.yml` with the sidecar-only
  definition.
- Removed the duplicate `docker/docker-compose.fan.yml`.
- Removed legacy `host_setup/` and `container_env/` files.
- Rewrote `docker/deploy.sh`; it no longer builds `mentorpi:latest`, installs
  udev/systemd files, mounts `/dev`, or starts a replacement `MentorPi`.
- Removed `SETUP_GUIDE.md`; root `README.md` is now canonical.

Vendor code in `mentorpi/src`, extracted code in `third_party_src`, and
`tools_and_models` were deliberately retained as reference material for future
customization. They are not included in the sidecar build context.

## Validation history

- The ARM64 `mentorpi-fan` image built successfully and ran locally through
  QEMU on 2026-09-05.
- `MentorPiFan` reported healthy on port 8081.
- Runtime inspection showed `privileged=false`, `readonly=true`, user `nginx`,
  no binds/devices, `cap_drop=["ALL"]`, and `no-new-privileges=true`.
- Nginx configuration, JavaScript syntax, Compose rendering, and the health
  endpoint passed local checks.
- Camera and live LiDAR integration still require validation on a powered
  factory robot with hardware-backed ports 8080/9090.

## Codex continuity

- Codex local memories are enabled on this workstation with
  `[features] memories = true` in `~/.codex/config.toml`.
- `AGENTS.md`, `GEMINI.md`, and this file provide repository-specific continuity
  independent of workstation-local memory.
