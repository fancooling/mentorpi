# MentorPi T1 Replacement Container Design

Status: `docker/original` implements the opt-in `runtime-core` candidate. The
hardware acceptance, rollback, optional tools, and AI gates in this document
remain open, so this is not yet a production-approved replacement.

Last inspected: 2026-09-07

## 1. Decision

Build a new, reproducible ARM64 image, provisionally named
`mentorpi-stack:<version>`, that contains the complete MentorPi ROS 2
application, device-facing nodes, vendor libraries, models, and operator tools.
On a robot it replaces, rather than accompanies, the factory `MentorPi`
container.

The existing `MentorPiFan` implementation and
[`MENTORPI_FAN_DESIGN.md`](MENTORPI_FAN_DESIGN.md) remain the low-risk sidecar
path. Nothing in this design changes
`docker/customization/docker-compose.yml` or authorizes running two robot stacks
at once. The replacement implementation uses its own `docker/original` build
context, Compose files, deployment command, and explicitly gated cutover action.

The replacement retains the container name `MentorPi` in its compatibility
mode. The installed host services and tools execute commands against that exact
name, so changing it would silently break boot, configuration synchronization,
and maintenance utilities.

## 2. Goals and non-goals

### Goals

- Reproduce all active factory-container functions from reviewable source and
  pinned dependencies.
- Make the high-level robot behavior editable, buildable, and versioned.
- Preserve the installed host contract: udev-created device names, Docker,
  systemd startup scripts, networking, and display/audio integration.
- Preserve the active MentorPi T1 configuration, calibration, maps, interfaces,
  and optional large-model functions without baking mutable state or secrets
  into the image.
- Replace blanket `privileged` access with the smallest device and permission
  set demonstrated to work.
- Make rollback to the untouched factory container possible without reflashing
  the SD card.

### Non-goals

- Rebuilding Raspberry Pi OS, the kernel, host udev rules, firmware, Docker, or
  host systemd units.
- Running the replacement beside the factory robot stack. Both would contend
  for serial/USB devices, ROS names, actuator topics, TF, and ports 8080/9090.
- Treating the existing repository extracts as authoritative. They are useful
  references, but extraction for this image must start from the mounted disk.
- Publishing vendor SDKs, model weights, maps, or source before their licenses
  and data sensitivity have been reviewed.
- Preserving build caches, logs, shell history, credentials, or arbitrary files
  from the factory container.

## 3. Evidence and source-of-truth policy

The authoritative input for this analysis is the read-only filesystem mounted
at `/mnt/rpi-rootfs`, backed by `/dev/mapper/loop37p2`. The inspection covered
the host configuration plus the Docker daemon metadata, imported image layer,
and container writable layer stored in that filesystem. The official Hiwonder
documentation is corroborating context only.

Use this precedence when facts disagree:

1. Active files in the factory container writable layer.
2. The factory image layer beneath that container.
3. Host files in the mounted Raspberry Pi OS.
4. Previously extracted repository copies.
5. Vendor documentation.

The active primary and third-party workspaces are marked as opaque OverlayFS
directories in the container writable layer. Their writable-layer trees are
therefore complete current views, not deltas that need to be combined with the
old workspaces in the imported image. Package database state is likewise
overridden by the active writable layer. A generic export of only the base
image would miss the current robot code and much of the installed software.

The inspection and source capture do not write to the mounted image. The
implementation records hashes for every extracted file and retains a private,
Git-ignored source snapshot. Before robot cutover, retain a separate forensic
backup of the original image/container for rollback. Any publishable build
input must remain a separately reviewed, sanitized, allowlisted snapshot.

## 4. Reconstructed factory system

### 4.1 Host and container baseline

| Item | Observed state |
| --- | --- |
| Host OS | Debian 12 (Bookworm), ARM64 Raspberry Pi OS |
| Host kernel artifacts | Raspberry Pi `6.12.96+rpt-rpi-2712` |
| Robot container | ID `adb8457c2eeca33b9aaca2386cf11b344580fd5290cd38395289698a0ebcc448`, name `MentorPi` |
| Factory image | local-only `ros:humble`, image `sha256:4863da8b74e6aa185bfd5f6a40ad4462e8fd6b5b66411881ed2be6dbccdb4898` |
| Container OS | Ubuntu 22.04.4 (Jammy), ARM64, ROS 2 Humble |
| Image provenance | one imported layer; no usable Dockerfile history |
| Container command | `tail -f /dev/null` |
| Runtime mode | root default, host network, restart always, privileged, writable root filesystem |
| Active package state | 2,152 installed Debian packages, including 403 `ros-humble-*` packages |
| Active primary workspace | `/home/ubuntu/ros2_ws`, about 997 MB including generated output |
| Active third-party workspace | `/home/ubuntu/third_party_ws`, about 332 MB including generated output |
| Exposed services | rosbridge websocket on 9090; web video server on 8080 |

The tag `ros:humble` is misleading: it names a locally imported 12 GB root
filesystem and must not be pulled from, pushed to, or confused with a public
image using the same tag. The replacement needs a distinct tag and a fully
qualified, digest-pinned base image.

The factory container writable layer is roughly 11 GB and includes about 7.3
GB under `/home`, a 2.1 GB swap file, temporary data, package caches, ROS build
outputs, user histories, and credential-bearing locations. `docker commit`,
`docker export`, or copying the layer wholesale is unsuitable for a production
artifact.

### 4.2 Host boot and compatibility contract

The installed boot path is:

```text
systemd start_node.service
  -> /home/pi/mentorpi/start_node.sh
     -> xhost +
     -> docker exec ... MentorPi ~/.stop_ros.sh
     -> docker exec -u ubuntu -w /home/ubuntu MentorPi
          source ~/.zshrc; ros2 launch bringup bringup.launch.py
```

`sync_typerc.service` continuously runs a host script that executes
`~/.sync_typerc.sh` in `MentorPi`. Host Wi-Fi, device-discovery, Servo, and lab
tools also hard-code the name `MentorPi`. The replacement compatibility mode
must therefore provide:

- a long-running container named `MentorPi` before `start_node.service` runs;
- user `ubuntu`, home `/home/ubuntu`, and a working zsh environment;
- `/home/ubuntu/ros2_ws`, `~/.zshrc`, `~/.robotrc`, `~/.stop_ros.sh`, and
  `~/.sync_typerc.sh` at their existing paths;
- the configuration-sharing path expected by the installed host scripts;
- host networking and ports 8080/9090 free for the robot stack;
- the current device aliases and input/audio devices when their features are
  enabled.

The serialized Docker metadata disagrees about one historical bind mount:
`HostConfig` records `/home/pi/docker_ros2/tmp:/home/ubuntu/share/tmp`, while
the stored mount-point list records `/home/pi/docker/tmp:/home/ubuntu/shared`.
The active host sync script expects `/home/ubuntu/shared/.typerc`. A powered
robot inspection must resolve this discrepancy; a replacement must not blindly
copy either metadata record.

### 4.3 Host device contract

The host is assumed to retain its udev rules and drivers. The container relies
on these stable names and permissions:

| Host resource | Purpose | Current rule/configuration |
| --- | --- | --- |
| `/dev/rrc` | STM32 robot controller | symlink for the CH343/ACM controller; opened at 1,000,000 baud |
| `/dev/ldlidar` | active MS200 lidar | USB-topology-based tty symlink; 230400 baud |
| `/dev/bus/usb` | Aurora depth camera and other hot-plug USB devices | vendor/product udev permissions |
| `/dev/input/js0` | gamepad | ROS joy input |
| `/dev/snd` | local ALSA devices | optional audio/voice functions |
| `/dev/wonderecho` | WonderEcho microphone array | optional large-model voice path |
| X11, PulseAudio, D-Bus | graphical vendor utilities | optional tools profile only |

Two host risks need separate remediation or explicit acceptance: the USB camera
rule contains a likely brace typo, and some serial aliases depend on physical
USB topology. Container construction cannot correct either host behavior.

## 5. Active robot configuration

The mounted active `.typerc` identifies the exact target as:

| Field | Value |
| --- | --- |
| Vendor version | `V2.0.1`, dated `2026-08-22` |
| Machine | `MentorPi_Tank` |
| Lidar | `MS200` |
| Depth camera | `aurora` |
| ROS domain | `0` |
| ASR language | English |
| Build-on-start flag | false |

`.robotrc` sources ROS 2 Humble, the primary workspace, and the third-party
workspace; sets ALSA and vendor library paths; and configures the ROS package
index mirror. The replacement should generate an equivalent environment from
declarative configuration rather than mutate shell startup files at runtime.
Compatibility symlinks/files can still be emitted at the paths expected by the
host tools.

Mutable state observed in the container includes IMU calibration, chassis
correction factors, servo offsets, SLAM maps, and an RTAB-Map database. These
are robot-specific data, not image content; see section 9.

## 6. ROS package and feature inventory

### 6.1 Primary workspace

The active primary source tree contains 17 built packages:

| Group | Packages | Function |
| --- | --- | --- |
| Boot/control | `bringup`, `controller`, `ros_robot_controller`, `sdk` | launch orchestration, STM32 protocol, kinematics, odometry, actuator I/O |
| Interfaces | `interfaces`, `ros_robot_controller_msgs`, `large_models_msgs` | custom topics, services, and messages |
| Robot model | `mentorpi_description` | URDF/xacro and TF model |
| Sensors/localization | `calibration`, `peripherals`, `slam`, `navigation` | IMU, lidar/camera launch, SLAM, Nav2 |
| Behaviors | `app`, `example`, `multi`, `yolov5_ros2` | tracking, line following, examples, perception |
| AI features | `large_models` | voice, LLM/VLM, navigation and tracking agents |

The workspace Git checkout is `master` at
`3a9480c763d0aaadb30744af422e0d4640380bdb`, but the active working tree has
tracked modifications in application behavior, large-model configuration,
lidar launch, robot description, and map files. Extraction must capture the
working tree, not merely that commit. Store a sanitized patch and file hashes
alongside the imported source; never log possible credential values from the
large-model configuration.

### 6.2 Third-party workspace

The active third-party tree contains 18 built packages:

- vendor/device packages: `ascamera`, `deptrum-ros-driver-aurora930`,
  `ldlidar_stl_ros2`, `oradar_lidar`, `sclidar_ros2`,
  `sllidar_ros2`, and `ydlidar_ros2_driver`;
- localization/navigation packages: `imu_calib`, `rf2o_laser_odometry`,
  `costmap_converter`, `costmap_converter_msgs`, `teb_local_planner`, and
  `teb_msgs`;
- services/fiducials: `web_video_server`, `async_web_server_cpp`,
  `apriltag_ros`, and `apriltag_msgs`;
- sensor filtering: `laser_filters`.

At extraction time, compare every tree against its upstream release. Install
unmodified, standard packages from a pinned ROS/apt source where possible.
Retain source for vendor-only packages, unavailable ARM64 packages, bundled
SDKs, or locally modified packages. In particular, the Aurora driver includes
an ARM64 vendor SDK and must be built and linked deliberately rather than
assumed to exist in a public ROS image.

### 6.3 OS and Python dependencies

The factory environment includes Nav2, SLAM Toolbox, RTAB-Map, rosbridge,
robot-localization, joystick, IMU and laser filters, image transport/CV bridge,
PCL, OpenCV, FFmpeg/GStreamer, PortAudio, and USB libraries. Python additions
include PyTorch 2.3.1, torchvision 0.18.1, MediaPipe 0.10.9, OpenCV headless
4.10, NumPy 1.24.3, Ultralytics, YOLOv5, scikit-learn, librosa, sounddevice,
and cloud/LLM clients.

These observations are an inventory, not a lock file. Implementation must
generate:

- an exact dpkg selection/version manifest from the active package database;
- a Python distribution/version/hash manifest from the active environment;
- a ROS source manifest with repository URL, revision, patch, and license;
- hashes and provenance for SDK archives, models, and binary libraries; and
- an SBOM for the final runtime image.

Copying `/usr`, `/opt/ros`, or `site-packages` from the writable layer would
make ABI provenance and security updates unmanageable and is not the build
strategy.

## 7. Active launch and data flow

`bringup.launch.py` currently starts eight branches concurrently:

```text
bringup.launch.py
  +-- controller.launch.py
  |    +-- robot_state_publisher
  |    +-- ros_robot_controller (STM32 bridge)
  |    +-- odom_publisher
  |    +-- imu_calib -> complementary_filter
  |    `-- robot_localization EKF
  +-- depth_camera.launch.py          (Aurora RGB/depth/IR/point cloud)
  +-- lidar.launch.py                 (MS200 -> /scan_raw)
  +-- rosbridge_websocket_launch.xml  (TCP 9090)
  +-- web_video_server                (TCP 8080)
  +-- start_app.launch.py
  |    +-- lidar_controller
  |    +-- line_following
  |    +-- object_tracking
  |    `-- hand_gesture
  +-- joystick.launch.py
  `-- init_pose
```

The important control and sensor paths are:

```text
/dev/input/js0 -> joy -> joystick_control ---------+
                                                     |
app behaviors -> /app/cmd_vel ----------------------+-> odom_publisher
external navigation -> /cmd_vel (clamped) ---------+      |
controller/cmd_vel ---------------------------------+      +-> wheel RPS
                                                            |
                                                            `-> /ros_robot_controller/set_motor
                                                                   |
                                                              /dev/rrc -> STM32 -> tracks

/dev/rrc -> imu_raw -> imu_calib -> imu_corrected -> filter -> /imu --+
command-integrated odom_publisher -> /odom_raw -----------------------+-> EKF -> /odom + TF

/dev/ldlidar -> oradar_scan -> /scan_raw
Aurora USB -> vendor driver -> RGB/depth/IR/points under legacy /ascamera names
```

The STM32 bridge also publishes battery, button, joystick, and SBUS state, and
accepts LED, buzzer, OLED, RGB, motor, PWM-servo, and bus-servo commands. It
selects the tank controller mode and commands four zero motor values during
initialization.

Tank geometry in the active odometry code uses a 0.1368 m wheelbase, 0.1446 m
track, and 0.075 m wheel diameter. Odometry is integrated from commanded
velocity; no wheel-encoder feedback was identified. The EKF fuses that
command-derived odometry with IMU orientation. Its configuration names an
RF2O lidar-odometry input, but the default controller launch does not start the
RF2O node.

The Aurora launch remaps the vendor driver to legacy
`/ascamera/camera_publisher/...` RGB, depth, IR, camera-info, and point-cloud
topics. The active MS200 launch uses `/dev/ldlidar`, publishes `/scan_raw`, and
defaults to frame `laser_frame`.

## 8. Parity quirks that must be preserved or deliberately fixed

The active source is internally inconsistent in several places. These are
versioned compatibility decisions, not opportunities for silent cleanup:

- `bringup.launch.py` constructs `startup_check_node` but does not return it,
  so the current source does not launch it.
- `lidar.launch.py` constructs a laser-filter node but does not return it, so
  `/scan` is not produced by that path.
- The bringup-to-lidar argument names do not match the child launch's declared
  names; child defaults currently determine `/scan_raw` and `laser_frame`.
- RF2O is configured as an EKF input but is not in the default controller
  launch graph.
- Historical ROS logs show older/different launch graphs and repeated driver
  termination; they are not a clean record of the current boot state.
- Most internal package manifests report version `0.0.0` and minimal upstream
  metadata, so image versioning and SBOM data must be supplied by this project.

First establish a parity build with automated regression tests around these
behaviors. Any fix then gets a separate feature flag or release note and a
recorded before/after ROS graph.

## 9. Target image architecture

### 9.1 Build inputs and repository layout

The implementation is isolated from the sidecar as follows:

```text
docker/original/
  Dockerfile
  compose.yml
  compose.operator.yml
  deploy.sh
  capture-source.sh
  runtime/                # entrypoint, health, compatibility, and stop helpers
  safety_src/             # motor-command freshness guard
  overrides/              # reviewed source-level compatibility overrides
  manifests/              # pinned build and runtime apt inputs
  .source/                # private, sanitized capture; excluded from Git
```

Do not reuse `docker/customization/docker-compose.yml`; it is the implemented
sidecar contract. The replacement build context is limited to
`docker/original/`; its `.dockerignore` excludes the private capture's forensic
metadata and any unsupported optional tools. Do not put the 52 GB disk image or
a broad repository root into a Docker build context.

The extraction allowlist includes:

- source from both active ROS workspaces, including active tracked changes;
- package metadata, launch/configuration files, URDF/xacro, and required
  scripts;
- vendor SDK headers/libraries required at runtime;
- explicitly selected tools from `/home/ubuntu/software`;
- model assets with checksums and proven runtime need; and
- robot-independent default configuration.

The denylist includes `build/`, `install/`, `log/`, `.git/`, `__pycache__/`,
swap files, caches, temporary files, `.ros/log`, shell histories,
`.git-credentials`, editor/agent state, API keys, tokens, user maps, RTAB-Map
databases, and generated recordings. Run secret and private-key scans before
the extracted tree is committed or uploaded.

### 9.2 Multi-stage build

Use a native ARM64 or `buildx --platform linux/arm64` multi-stage build:

1. Start from a fully qualified, digest-pinned Ubuntu 22.04/ROS 2 Humble base
   whose architecture is verified.
2. Install pinned apt/ROS build dependencies from recorded repositories and
   keys.
3. Install Python packages from an ARM64 lock with hashes. Build wheels in the
   builder where binary wheels are unavailable.
4. Build retained third-party packages, then the primary workspace, with
   deterministic `colcon` arguments.
5. Copy only installed ROS prefixes, required runtime libraries, selected
   tools, licenses, manifests, and entry scripts into the runtime stage.
6. Run as `ubuntu` by default with stable UID/GID compatible with persistent
   bind mounts. Use group access to devices, not root as the normal runtime.
7. Embed OCI labels for source revision, disk-image fingerprint, build date,
   SBOM, and configuration schema version.

The full-function release image must carry the optional GUI/calibration tools
and large AI dependencies as well as the always-on tank, sensor, ROS bridge,
and stock app functions. Runtime profiles control what starts and which host
resources are granted; development-only build targets may remain smaller.

TensorRT `.engine` files and similar binary accelerators are not portable by
filename alone. Preserve their provenance, but rebuild or validate them against
the target Pi, OS, accelerator, TensorRT version, and model hash before use.

### 9.3 Runtime profiles

Define three explicit profiles:

| Profile | Purpose | Additional resources |
| --- | --- | --- |
| `core` | controller, IMU/EKF, lidar, camera, stock apps, rosbridge, web video | `/dev/rrc`, `/dev/ldlidar`, USB camera access, host network |
| `operator` | joystick and local sound | `/dev/input/js0`, `/dev/snd` |
| `tools` | Servo/lab/calibration GUIs and voice array | X11/Pulse/D-Bus binds, `/dev/wonderecho`, extra tool assets |

Start with explicit devices. For USB hot-plug, bind `/dev/bus/usb` and use a
narrow device-cgroup rule for USB character devices only if tests prove it is
needed. The final stack should use `init: true`, capability drop, no-new-
privileges, a bounded `shm_size`, health checks, and writable mounts only for
the paths listed below. `privileged: true` is allowed only as a temporary
diagnostic comparison and cannot pass production acceptance.

### 9.4 Persistent and secret data

| Container path or logical data | Storage policy |
| --- | --- |
| robot type, camera/lidar selection, ROS domain | versioned host configuration rendered at start |
| IMU calibration | per-robot read-write bind/volume with backup |
| chassis and servo corrections | per-robot read-write bind/volume with backup |
| SLAM maps | named versioned data directory, never overwritten on upgrade |
| RTAB-Map database | separate persistent volume with size policy |
| recordings/captured images | explicit external data directory |
| logs and temporary files | bounded tmpfs or rotated data volume |
| model weights | checksum-addressed read-only artifact mount or image layer |
| API credentials | Docker secret or root-owned host file; never image, Git, or `.typerc` |

Initially preserve legacy in-container paths using bind destinations or
symlinks. Applications should later consume a documented configuration schema
so state no longer depends on a mutable Ubuntu home directory.

## 10. Startup ownership modes

Exactly one component may own ROS startup.

### 10.1 Compatibility mode: existing host owns launch

This is the first migration target because the user-supplied premise says host
startup scripts already exist. The container stays alive without starting ROS;
`start_node.service` executes `ros2 launch bringup bringup.launch.py` as user
`ubuntu`, as on the factory image. The image supplies compatible shell helpers
and the name `MentorPi`.

Compatibility mode minimizes host change, but Docker health describes only the
container, not the `docker exec` launch process. The health check must therefore
query the required ROS graph and hardware readiness, and the host must not mark
the robot ready merely because the container is running.

### 10.2 Supervised mode: container owns launch

The preferred steady state uses a signal-aware entrypoint as PID 1 to launch
and supervise bringup. In this mode `start_node.service` may start the Compose
stack but must not execute a second ROS launch. Other host helpers can retain
the `MentorPi` name. A single configuration setting selects the mode, and
deployment validation must reject configurations that enable both owners.

In either mode, replace the factory `ps | grep | kill -9` helper with a scoped,
graceful shutdown path. It must stop velocity production, command all four
motors to zero through the live controller path, confirm the stop if possible,
then terminate ROS nodes and only escalate signals after a bounded grace
period.

## 11. Safety architecture

The reconstructed control path has two critical properties:

- odometry is integrated from commanded motion rather than verified encoder
  feedback; and
- no stale-velocity timeout was found in the active high-level publisher.

The presence and timing of any STM32 firmware watchdog are unproven. Until a
physical fault-injection test proves otherwise, assume the last non-zero motor
command can persist.

The replacement must add or prove all of the following before track-on-ground
testing:

1. A single velocity arbiter owns the actuator-facing command path. Joystick,
   navigation, and app behaviors cannot write motors independently.
2. Commands carry freshness/lease semantics. Loss of the active producer,
   control loop, ROS graph, serial link, or container causes a zero command
   within a measured and documented bound.
3. SIGINT, SIGTERM, `docker stop`, launch failure, health failure, and host
   shutdown all take the same safe-stop path. SIGKILL is not the normal stop
   mechanism.
4. Automatic restart cannot resume previous motion or start an autonomous
   behavior without an explicit new enable command.
5. The STM32 bridge is initialized before behavior nodes and remains available
   until after the final zero command.
6. A human controls robot power or a physical emergency stop throughout bench
   and track tests. Initial motor tests are performed with tracks off the
   ground and the area clear.

Startup should be staged instead of launching every branch concurrently:

```text
device presence -> STM32 bridge -> controller ready and zeroed
  -> IMU/localization -> camera/lidar readiness
  -> velocity arbiter -> apps/navigation -> network bridges -> healthy
```

Readiness must be based on required topics/services and fresh sensor data, not
fixed sleeps. The existing `/ros_robot_controller/init_finish` service is a
candidate controller gate, but its semantics need live verification.

## 12. Network and security boundary

Host networking is required for initial parity, but rosbridge on 9090 and web
video on 8080 have no container boundary from the LAN. Treat both as trusted-
network services. Before use on an untrusted network, add host firewall policy
or an authenticated TLS gateway and confirm that rosbridge cannot be used to
publish actuator commands anonymously.

The installed startup script also runs `xhost +`, granting broad X-server
access. Retain it only during compatibility testing of graphical tools; the
headless core profile should not need X11, and the tools profile should use a
narrow per-user or per-container authorization.

Production acceptance requires:

- non-root runtime with only required device groups;
- no privileged mode and no broad `/dev:/dev` bind;
- all capabilities dropped unless one is justified by a test;
- no-new-privileges and a read-only root filesystem after write-path audit;
- secrets external to the image and logs;
- dependency, vulnerability, secret, and license scans; and
- immutable image digest verification during deployment.

The large-model package can call external AI services. It is optional at first
cutover and must have an explicit outbound-network and credential policy before
enablement.

## 13. Implementation phases

### Phase A: freeze and capture

1. Record the raw image checksum, partition identity, Docker image/container
   metadata, active OverlayFS paths, and file hashes.
2. On a powered robot, capture a clean factory boot: container inspect, mount
   bindings, ROS nodes/topics/services/actions, message types, QoS, TF tree,
   ports, device ownership, and resource use.
3. Make private rollback artifacts for the factory image and current container.
   Treat them as sensitive because their files may contain credentials.
4. Extract only the allowlisted active source/configuration, recording the base
   Git revision and sanitized working-tree patch.

### Phase B: reproducible build

1. Create the separate full-stack build context and manifests.
2. Resolve every apt, Python, ROS, SDK, and model dependency to a version and
   hash; document exceptions.
3. Build third-party and primary workspaces in a multi-stage ARM64 build.
4. Run unit, launch-syntax, package, interface, and source-policy tests.
5. Generate an SBOM and scan the source and final image for secrets/licenses.

### Phase C: parity without hardware motion

1. Run ARM64 user-mode/QEMU checks for entrypoint, imports, shared libraries,
   package discovery, and launch parsing.
2. On the Pi, start with motor power isolated or tracks raised.
3. Validate device enumeration and passive telemetry before enabling actuator
   writes.
4. Compare the live ROS graph, topic types/QoS, TF, services, ports, and camera/
   lidar output to the factory baseline.

### Phase D: safety and feature validation

1. Prove the velocity timeout and zero-on-shutdown behavior using instrumented
   commands and a measured deadline.
2. Inject process crashes, serial disconnect, sensor disconnect, SIGTERM,
   Docker stop, reboot, and network loss.
3. Exercise joystick, line following, object tracking, SLAM, Nav2, Servo tools,
   and optional AI profiles separately.
4. Verify calibration and map persistence across replacement and rollback.

### Phase E: opt-in cutover

1. Stop/disable the launch owner long enough to prevent an automatic factory
   restart.
2. Stop the factory `MentorPi`; verify no device-owning ROS process remains.
3. Start the replacement as the only `MentorPi` and run pre-motion acceptance.
4. Re-enable exactly one launch owner.
5. Record the deployed image digest, configuration hash, data backup, and test
   result.

Cutover commands belong in a reviewed implementation runbook. They should not
be inferred from the sidecar deployment script, which intentionally never
manages the factory container.

## 14. Acceptance matrix

| Area | Required evidence |
| --- | --- |
| Provenance | source snapshot and patches match mounted image; all build inputs hashed |
| Architecture | final image is Linux ARM64 and starts on the target Pi |
| ROS build | all required primary and retained third-party packages build from clean source |
| Controller | `/dev/rrc`, battery, buttons, IMU, LED/buzzer/OLED, servo and motor interfaces work |
| Motion safety | stale input, node death, TERM, Docker stop, serial loss, and reboot reach zero motion within the accepted bound |
| Lidar | MS200 produces fresh `/scan_raw` with correct frame/range; intentional filter/RF2O decisions tested |
| Camera | fresh RGB, depth, IR, camera-info and point cloud appear under compatible topic names |
| Localization | IMU calibration/filter, command odometry, EKF and TF match the documented graph |
| User input | joystick mapping, limits, deadman/enable behavior and disconnect behavior pass |
| Stock apps | line following, object tracking, hand gesture and lidar controller activate/deactivate cleanly |
| Navigation | SLAM/map persistence, localization and Nav2 validated on the intended sensor graph |
| Network | ports 8080/9090 work from the intended subnet and are blocked elsewhere by policy |
| Optional AI | voice, `/dev/wonderecho`, audio, models and external API secrets work only when profile enabled |
| Persistence | calibration, maps, database and configuration survive rebuild/reboot; logs remain bounded |
| Isolation | non-root, no privileged flag, no broad `/dev` bind, minimal writable paths/capabilities |
| Operations | health/readiness, metrics, log rotation, immutable digest deployment and rollback drill pass |

No successful feature demo waives the motion-safety tests.

## 15. Rollback design

The factory image ID, its private backup, container configuration, bind mounts,
and host start ownership must be captured before cutover. Rollback is:

1. command zero motion and remove robot motor power;
2. stop the replacement and its restart policy;
3. restore the saved factory container under the exact name `MentorPi` with its
   verified original image and mounts;
4. restore the original launch-owner setting;
5. boot with tracks raised and repeat passive telemetry plus safe-stop checks;
6. restore robot data only from a compatible, pre-cutover backup.

The replacement must never delete or retag the only factory artifact. A
rollback drill is a release gate, not a post-failure experiment.

## 16. Known unknowns and blocking gates

- The exact live bind path for `.typerc` must be resolved from the running
  factory Pi; stored Docker metadata conflicts.
- The clean current ROS graph and QoS settings must be captured from a powered
  factory boot. Old log files represent multiple software revisions.
- STM32 firmware watchdog behavior, motor-command persistence, and measurable
  stop latency are unknown. This blocks on-ground operation.
- The camera udev typo and topology-dependent serial aliases need host-level
  validation on a clean new Raspberry Pi OS.
- Dirty primary/third-party source must be reviewed for behavior changes and
  secrets before extraction.
- Vendor SDK, model, map, and source redistribution rights are not established;
  keep initial artifacts private.
- TensorRT engines and other binary models need target-hardware compatibility
  evidence.
- External AI-service credentials and network dependencies are intentionally
  excluded from the image and require a separate enablement decision.
- It is not yet known which GUI/audio bindings are required for headless core
  operation versus optional operator tools.

These unknowns do not prevent constructing a reproducible build, but the first
three prevent claiming deployment parity or motion safety.

## 17. References

- Mounted source: `/mnt/rpi-rootfs` (authoritative inspection input).
- Existing sidecar design: [`MENTORPI_FAN_DESIGN.md`](MENTORPI_FAN_DESIGN.md).
- Existing sidecar runtime: [`../docker/customization/docker-compose.yml`](../docker/customization/docker-compose.yml).
- Hiwonder, [MentorPi T1 getting ready](https://wiki.hiwonder.com/projects/MentorPi-T1/en/latest/docs/1.getting_ready.html).
- Hiwonder, [remote tool installation and container access](https://wiki.hiwonder.com/projects/MentorPi-T1/en/latest/docs/2.remote_tool_installation_and_container_access.html).

The vendor pages confirm that the supplied robot is Raspberry Pi 5/ROS 2 based
and that robot modules run in Docker. Package contents, launch behavior,
container settings, and safety conclusions in this design come from the
mounted filesystem, not those pages.
