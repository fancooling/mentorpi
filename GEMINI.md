# MentorPi Robot Tank Project Context

This file provides persistent architecture and safety context for agents and
developers. Read it with `CONVERSATION_MEMORY.md` before changing the repository.
The root `README.md` is the canonical build and deployment guide.

## Factory-image and sidecar mode

- Target: Hiwonder MentorPi Tank with Raspberry Pi 5 ARM64, STM32 chassis
  controller, Oradar MS200 LiDAR, and Aurora/Astra depth camera.
- Preserve the vendor Raspberry Pi OS and its factory `MentorPi` container.
- The factory container remains the only owner of hardware devices, drivers,
  odometry, TF, and motion control.
- Deploy one independent customization sidecar: image
  `mentorpi-fan:latest`, container `MentorPiFan`, Compose project
  `mentorpi-fan`.
- The sidecar contains only Nginx and static browser assets. It has no ROS
  runtime, controller, navigation, teleoperation, driver, or actuator code.
- A separate opt-in replacement candidate is documented for `docker/original`,
  but that directory is absent from the current checkout. If restored, its
  `runtime-core` image remains mutually exclusive with the factory stack and
  does not supersede the factory container or rollback baseline until all
  acceptance gates pass.

## Native Ubuntu 26.04 controller mode (pre-deployment implementation)

`docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md` defines a second target mode for a
clean Ubuntu 26.04 Pi 5 with native ROS 2 Lyrical. Milestones 1-3 have a
hardware-free implementation and regression suite, but the complete target apt
closure, native ROS build, host installation, and physical deployment do not
yet have target-Pi acceptance. This mode is controller-only and uses a guarded native
service for bounded forward, reverse, left, and right motion.

This is a personal, single-owner robot. The owner controls the Ubuntu image and
all software, services, and user accounts installed on it. Treat owner-approved
local software, including processes running as the dedicated service account,
as trusted. A malicious or compromised same-UID process is outside the native
controller threat model. Local PID/UID heartbeat checks need only catch
accidental senders and configuration mistakes; they are not required to provide
hostile same-user isolation. This trust assumption does not relax motion-safety
requirements for stale commands, crashed or hung processes, serial loss, or
unexpected service restarts.

The native mode is not installed on the vendor Raspberry Pi OS and must never
run beside the factory `MentorPi` container, the observer sidecar, or a restored
replacement container. Preserve the verified vendor image on separate media;
transition between platform modes only while powered down. In native mode,
application releases live below `/opt/ubuntu_tank`, configuration below
`/etc/opt/ubuntu_tank`, and the dedicated service is the sole `/dev/rrc` owner.
Its application rollback does not restore the vendor OS.

## Source of truth

`MentorPi_T1_20260822.img` is the authoritative vendor disk image and is mounted
at `/mnt/rpi-rootfs` when inspected. Repository source directories are reference
or separately supplied material; do not assume they exactly match the software
running in the factory container.

That precedence applies to investigations of the factory image and its active
runtime. For the native Ubuntu controller design and implementation,
`mentorpi/src/` is the primary code reference. Use `/mnt/rpi-rootfs` only when
repository code and direct inspection of the clean Ubuntu target cannot resolve
a required missing, hardware-specific, or contradictory fact, and record each
fallback explicitly.

The factory image locally tagged `ros:humble` inside the vendor disk is an
imported ARM64 Ubuntu/ROS filesystem, not the public Docker Hub base with the
same tag. Never pull, retag, replace, or rebuild it as part of sidecar deployment.

## Repository layout

- `docker/customization/`: complete sidecar deployment, including its Dockerfile,
  Compose definition, deployment script, Nginx configuration, and web UI. The
  Compose project manages only `MentorPiFan`.
- `docker/original/`: reserved path for the isolated replacement workflow; it
  is absent from the current checkout, so do not run or cite it as available.
- `mentorpi/src/`: vendor-supplied high-level ROS source retained for behavior
  and interface research; excluded from the sidecar image.
- `third_party_src/`: extracted drivers and SDKs retained for comparison;
  excluded from the sidecar image.
- `tools_and_models/`: calibration and model assets retained for future
  perception work; excluded from the sidecar image.
- `docs/MENTORPI_FAN_DESIGN.md`: detailed ownership, interface, and security
  design.

The former reconstructed full-stack Dockerfile, privileged Compose service,
host installers, and container environment files were removed. Do not
reintroduce them into the customization deployment or confuse them with the
currently absent opt-in replacement workflow documented for `docker/original`.

## Runtime interfaces

- Factory `web_video_server`: port 8080. Nginx exposes it read-only under
  `http://ROBOT_IP:8081/video/`.
- Factory rosbridge: port 9090. The browser connects directly and the bundled
  JavaScript sends only `subscribe` and `unsubscribe` operations.
- Custom dashboard: port 8081, the only port owned by `MentorPiFan`.
- Default topics: RGB
  `/ascamera/camera_publisher/rgb0/image`, LiDAR `/scan_raw`, odometry `/odom`,
  and battery `/ros_robot_controller/battery`.

The factory rosbridge remains a complete bidirectional ROS interface. Keep the
robot on a trusted network. Port 8081 must not proxy rosbridge.

## Deployment constraints

- Build for `linux/arm64` from the digest-pinned public Nginx base.
- Never include `MentorPi_T1_20260822.img` in a Docker build context.
- Never configure `privileged`, `/dev` mounts, bind mounts, or added Linux
  capabilities for the sidecar.
- Keep the root filesystem read-only, drop all capabilities, use the non-root
  `nginx` user, and retain `no-new-privileges`.
- Remote deployment must verify the factory `MentorPi` container is running and
  must never stop, recreate, or modify it.

Run `./docker/customization/deploy.sh help` for the supported local, remote, and
test commands.

For replacement work, first verify that `docker/original/` has been deliberately
restored. Only then use its documented help. Never run a replacement cutover
until its acceptance file is complete, a verified factory backup exists, the
live device/bind contracts have been checked, and the tracks-raised physical
safety acknowledgment is valid.

## Git versions and review revisions

- Create a new Git version (commit) only for a new feature or milestone.
- Fold revisions addressing code review comments into the existing commit for
  that feature or milestone by amending or squashing them; do not leave separate
  review-fix commits or create a new version for those revisions.
- Every commit must include an informational body explaining what changed and
  why, including relevant operational effects and validation. Keep that body
  current when incorporating review revisions.
