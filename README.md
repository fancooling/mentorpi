# MentorPi Container Workflows

`mentorpi-fan` is a customization sidecar for a Hiwonder MentorPi tank that
keeps the vendor Raspberry Pi OS and factory `MentorPi` container intact. The
factory container remains the sole owner of the STM32 controller, LiDAR,
camera, odometry, TF, and motion-control pipeline.

The repository documents an opt-in replacement candidate for `docker/original`,
but that directory is absent from the current checkout. If deliberately
restored, it is a separate, mutually exclusive workflow: never start it beside
the factory robot stack or treat its hardware-free validation as authorization
for robot cutover.

The custom image contains only Nginx and a static browser dashboard. It has no
ROS runtime, controller, navigation, teleoperation, hardware driver, actuator
API, device mount, or privileged capability.

The clean Ubuntu 26.04 Pi 5 controller uses the paired runtime/web containers
in [the container guide](docker/ubuntu_tank/README.md). C1–C4 implementation is
present; C4 deployment, redeployment, reboot and power-on checks passed on the Pi.
The owner waived other C4 fault tests. M15 supplies owner-scoped C5 acceptance;
physical timing remains waived. See the [operator guide](ubuntu_tank/docs/OPERATOR_GUIDE.md)
and [M16 handoff](docs/M16_RELEASE_HANDOFF.md) for current evidence and recovery. This
mode uses separate boot media and must never coexist with the factory stack,
sidecar or replacement mode on a running robot.

To build the Ubuntu controller on a new computer, follow the
[development-host setup](docker/ubuntu_tank/README.md#new-development-host).
`requirements-build.txt` supplies host Python build dependencies;
`requirements-dev.txt` adds local tests and tooling in the same repository `.venv`.

## Architecture

| Interface | Existing factory owner | Customization use |
| --- | --- | --- |
| Camera HTTP, port 8080 | `web_video_server` | Read-only proxy at `/video/` |
| ROS WebSocket, port 9090 | `rosbridge_server` | Browser subscribes directly |
| Dashboard HTTP, port 8081 | None | `MentorPiFan` owns this port |
| STM32, LiDAR, camera devices | Factory `MentorPi` | No sidecar access |

The sidecar UI sends only rosbridge `subscribe` and `unsubscribe` operations,
and port 8081 explicitly rejects `/ros`. The factory port 9090 is still a
complete bidirectional ROS interface, so keep the robot on a trusted network.

## Prerequisites

- Docker Engine with the Compose v2 plugin on the development computer.
- ARM64/QEMU container emulation when building or testing on x86-64.
- For remote deployment, SSH access to an ARM64 Raspberry Pi whose factory
  `MentorPi` container is already running.
- The current user must be able to run Docker on both the local and remote host.

## Automated deployment

The deployment script manages only `MentorPiFan`. It never installs host rules,
opens devices, stops the factory container, or changes the vendor OS.
Every deployment validates the sidecar-only Compose policy, immutable image ID,
ARM64 architecture, runtime user, observer provenance labels, absence of ROS and
control operations, and Nginx configuration. On failure, it preserves an
existing sidecar that was not replaced; otherwise it removes the failed
container and restores the previous image tag when available. It does not
automatically restart an old image through potentially incompatible new
configuration.

Build and start locally:

```bash
./docker/customization/deploy.sh local
```

Build locally, transfer the ARM64 image, and start it on the vendor Pi:

```bash
./docker/customization/deploy.sh remote pi@ROBOT_IP
```

Validate Compose isolation and the complete observer-image policy:

```bash
./docker/customization/deploy.sh test
```

After deployment, open `http://ROBOT_IP:8081/`.

## Replacement candidate (currently absent)

When restored, the replacement workflow is intended to capture sanitized build
input from the mounted factory disk, build the ARM64 runtime-core image, and
perform hardware-free policy, package, launch, safety-guard, and shutdown
checks. The following commands are unavailable in the current checkout:

```bash
./docker/original/deploy.sh capture
./docker/original/deploy.sh test
```

When that workflow exists, robot staging, backup, cutover, and rollback must be
separate commands. Cutover requires every acceptance gate and a physical
tracks-raised acknowledgment. The current repository retains only
[`docs/MENTORPI_REPLACEMENT_CONTAINER_DESIGN.md`](docs/MENTORPI_REPLACEMENT_CONTAINER_DESIGN.md)
for the outstanding hardware, parity, licensing, tools, and AI gates; there is
currently no replacement operator guide or executable to run.

## Manual local workflow

Run from the repository root:

```bash
docker compose -f docker/customization/docker-compose.yml build
docker compose -f docker/customization/docker-compose.yml up -d --no-build
docker compose -f docker/customization/docker-compose.yml ps
curl http://127.0.0.1:8081/healthz
```

Open `http://localhost:8081/`. Without factory services on local ports 8080 and
9090, the dashboard remains available but correctly reports the camera and ROS
graph as unavailable.

The resulting image is stored in the local Docker daemon as
`mentorpi-fan:latest`. It is not written to a repository file unless exported
with `docker save`.

## Dashboard defaults

- RGB camera: `/ascamera/camera_publisher/rgb0/image`
- LiDAR: `/scan_raw`
- Odometry: `/odom`
- Battery: `/ros_robot_controller/battery`

Camera and LiDAR topic names can be changed in the dashboard if the installed
factory image uses different names.

## Verify isolation

On the deployment host:

```bash
docker inspect MentorPiFan --format '{{json .HostConfig.Privileged}} {{json .HostConfig.Binds}} {{json .HostConfig.Devices}} {{json .HostConfig.CapDrop}} {{json .HostConfig.ReadonlyRootfs}}'
```

The expected result is:

```text
false null null ["ALL"] true
```

## Stop the customization sidecar

Locally:

```bash
docker compose -f docker/customization/docker-compose.yml down
```

On the Pi:

```bash
ssh pi@ROBOT_IP 'cd "$HOME/mentorpi-fan/docker/customization" && docker compose -f docker-compose.yml down'
```

The Compose project is explicitly named `mentorpi-fan`, so these commands do
not stop or remove the factory `MentorPi` container.

## Repository layout

- `docker/customization/`: the Nginx image, sole Compose definition for
  `MentorPiFan`, local and remote deployment tool, proxy configuration, and
  browser dashboard.
- `docker/original/`: reserved path for private-source capture and the mutually
  exclusive replacement workflow; absent from the current checkout.
- `mentorpi/src/`: vendor-supplied high-level ROS 2 source retained as behavior
  and interface reference.
- `third_party_src/`: extracted driver and SDK source retained for comparison
  with the factory system; it is not copied into `mentorpi-fan`.
- `tools_and_models/`: calibration utilities and model assets retained for
  future perception work.
- `MentorPi_T1_20260822.img`: authoritative vendor disk image used for offline
  inspection; it must remain outside every Docker build context.
- `docs/MENTORPI_FAN_DESIGN.md`: architecture, ownership, and safety rationale.
- `docs/MENTORPI_REPLACEMENT_CONTAINER_DESIGN.md`: requirements, evidence,
  architecture, and remaining acceptance gates for the opt-in replacement.
- `GEMINI.md` and `CONVERSATION_MEMORY.md`: persistent repository context.

The root `.dockerignore` excludes disk images, archives, build artifacts, and
nested repository metadata. The sidecar build context is further limited to
`docker/customization/`, so the 52 GB vendor image and reference source trees
cannot be sent to Docker during a sidecar build.
