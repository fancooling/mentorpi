# MentorPi Fan Sidecar Design

Status: Initial observer release implemented and locally smoke-tested  
Last updated: 2026-09-05

## 1. Decision summary

Keep the Raspberry Pi OS and factory `MentorPi` container intact. Add one
independently built ARM64 container for read-only customization:

- Image: `mentorpi-fan:<version>`
- Container: `MentorPiFan`
- Compose service: `mentorpi-fan`

`MentorPiFan` consumes the factory web services from a static browser client. It
does not contain a ROS runtime and must not contain or start controller,
driving, or hardware-driver software in its initial version.

The factory container remains the only owner of the STM32, LiDAR, depth camera,
odometry, TF, and motion-control pipeline. The sidecar initially owns only the
custom UI and read-only visualization. Recording and non-actuating perception
remain safe future extensions.

## 2. Goals

- Preserve the factory Raspberry Pi OS, host services, udev rules, and Docker
  container.
- Reuse the factory STM32, Oradar MS200, and Aurora camera drivers without
  rebuilding or redeploying them.
- Add browser-based camera and LiDAR visualization.
- Permit image-recognition experiments that subscribe to factory camera topics
  and publish derived results.
- Make the customization image independently buildable, deployable, removable,
  and replaceable.
- Prevent the sidecar from competing for devices, ports, ROS node names, TF, or
  motion topics.
- Make rollback equivalent to stopping and removing `MentorPiFan`.

## 3. Initial non-goals

The first `mentorpi-fan` image will not provide:

- Chassis control, routing, autonomous navigation, or teleoperation.
- A publisher for `/cmd_vel`, `/controller/cmd_vel`, `/app/cmd_vel`, or
  `/ros_robot_controller/set_motor`.
- The `controller`, `ros_robot_controller`, `navigation`, `app`, `example`, or
  other packages that can command motors or servos.
- STM32, Oradar, Aurora/Deptrum, Angstrong, or alternative sensor drivers.
- Direct access to `/dev`, USB, serial ports, GPIO, I2C, or camera devices.
- Replacement of the factory `web_video_server` or `rosbridge_server`.
- Modification of the factory `MentorPi` container's files or image layers.

Motion control can be designed later as a separate, explicit safety project. It
must not be enabled merely by adding a new node to this image.

## 4. Factory baseline and source of truth

The authoritative vendor system is `MentorPi_T1_20260822.img`, mounted for
inspection at `/mnt/rpi-rootfs`. Repository copies are references or extracted
artifacts; they are not assumed to define the installed factory system.

The mounted image contains:

1. Raspberry Pi OS Bookworm on the host.
2. Host kernel drivers, udev rules, Docker, networking, Wi-Fi/AP management,
   discovery, buttons, LEDs, and startup services.
3. A factory Docker image locally tagged `ros:humble`.
4. A factory container named `MentorPi` whose writable layer contains most of
   the Hiwonder robot workspaces and software.

The local factory `ros:humble` tag must not be confused with the public Docker
Hub tag. The mounted metadata identifies it as an imported ARM64 Ubuntu 22.04.4
filesystem:

- Image ID:
  `sha256:4863da8b74e6aa185bfd5f6a40ad4462e8fd6b5b66411881ed2be6dbccdb4898`
- Created: 2024-03-09
- One imported root-filesystem layer with no useful Dockerfile history.
- Approximately 12 GB unpacked.
- 1,925 installed Debian packages, including 381 `ros-humble-*` packages.
- Includes ROS desktop, RViz2, RQt, Nav2, SLAM Toolbox, MoveIt, image/PCL
  support, ROS bag tools, Fast DDS, and build infrastructure.

The factory `MentorPi` writable layer is approximately 11 GB and contains
`/home/ubuntu/ros2_ws`, `/home/ubuntu/third_party_ros2`,
`/home/ubuntu/software`, Python additions, models, calibration, and driver
workspaces.

At boot, the host starts the persistent `MentorPi` container, whose configured
command is `tail -f /dev/null`. Host `start_node.service` subsequently uses
`docker exec` to run `ros2 launch bringup bringup.launch.py` inside it.

The current repository launch file shows that factory bringup starts the
controller chain, camera, LiDAR, rosbridge, web-video server, stock application
nodes, joystick control, and initialization together. The mounted workspace was
compared separately and no known difference was found in this bringup file.

## 5. Target architecture

```text
Raspberry Pi OS host
  kernel + udev + /dev + Wi-Fi + Docker
                   |
                   v
Factory MentorPi container
  STM32 + controller/odom + LiDAR + camera + factory web bridges
       | HTTP :8080                         | rosbridge WebSocket :9090
       v                                    |
MentorPiFan sidecar                         |
  Nginx + static dashboard + camera proxy   |
  UI :8081                                  |
       |                                    |
       +---------------> Browser <----------+
                          trusted robot network
```

Both containers use the host network. Nginx reaches the factory video service
at `127.0.0.1:8080`; the browser receives camera frames through the UI origin
and connects directly to the factory rosbridge on port 9090. The sidecar does
not create a second unrestricted rosbridge endpoint and does not join DDS
itself.

## 6. Ownership boundaries

| Resource | Factory `MentorPi` | `MentorPiFan` |
| --- | --- | --- |
| Raspberry Pi kernel and udev | Uses host facilities | No changes |
| `/dev/rrc` and STM32 | Exclusive owner | No access |
| `/dev/ldlidar` and Oradar | Exclusive owner | No access |
| Aurora camera USB device | Exclusive owner | No access |
| `/scan_raw` | Publishes | Browser subscribes through rosbridge |
| RGB image topic | Publishes | Browser displays through web-video server |
| `/odom` and battery | Publishes | Browser subscribes through rosbridge |
| Depth, point cloud, `/tf`, `/tf_static` | Publishes | Available to future read-only features |
| Factory HTTP video port 8080 | Owns | Reuses as a client |
| Factory rosbridge port 9090 | Owns | Reuses as a client |
| Custom UI port 8081 | No use | Owns |
| `/fan/*` derived topics | May consume | Reserved for a future perception worker |
| Motion and actuator topics | Exclusive owner | Prohibited |

## 7. Factory services reused by the sidecar

### 7.1 Native ROS 2 DDS

Native ROS 2 DDS is reserved for a future non-actuating perception worker; it is
not included in `mentorpi-fan:0.1.0`. If such a worker is later added, its
required compatibility settings are:

- ROS 2 Humble on Ubuntu Jammy ARM64.
- `ROS_DOMAIN_ID=0`, matching the factory preset.
- `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`, matching the factory default.
- Sensor-data QoS for camera and LiDAR subscriptions when required.
- Exact message definitions for any non-standard types that are consumed.

Any future worker should prefer standard messages and avoid vendor actuator
messages entirely.

### 7.2 Factory camera streaming

The factory bringup starts `web_video_server` with its default HTTP port 8080.
The UI can embed the existing RGB stream:

```text
http://ROBOT_IP:8080/stream?topic=/ascamera/camera_publisher/rgb0/image&qos_profile=sensor_data
```

It can request a snapshot through:

```text
http://ROBOT_IP:8080/snapshot?topic=/ascamera/camera_publisher/rgb0/image&qos_profile=sensor_data
```

If a safe recognition node publishes an annotated `sensor_msgs/msg/Image` on
`/fan/camera/annotated`, the same factory server should discover it through the
shared ROS graph and stream it without another video server:

```text
http://ROBOT_IP:8080/stream?topic=/fan/camera/annotated&qos_profile=sensor_data
```

### 7.3 Factory ROS WebSocket bridge

The factory bringup starts rosbridge on its default WebSocket port 9090. Browser
code can connect to:

```text
ws://ROBOT_IP:9090
```

It can subscribe to `/scan_raw`, `/odom`, `/tf`, `/tf_static`, and safe
`/fan/*` topics. Rosbridge is a JSON/WebSocket protocol, not a REST API.

Camera frames should normally use `web_video_server` rather than rosbridge JSON
because raw images are large. LiDAR and telemetry are appropriate for a
throttled WebSocket subscription.

### 7.4 UI and camera proxy on port 8081

To give the UI and camera stream one HTTP origin, `MentorPiFan` reverse-proxies
only the existing factory video service:

```text
http://ROBOT_IP:8081/          -> custom static UI
http://ROBOT_IP:8081/video/... -> 127.0.0.1:8080
ws://ROBOT_IP:9090/            -> factory rosbridge, direct from browser
```

With host networking, `127.0.0.1` inside `MentorPiFan` refers to the shared host
network namespace, so the camera proxy can reach the factory service without
Docker port mappings.

An Nginx-style proxy configuration is:

```nginx
location /video/ {
    proxy_pass http://127.0.0.1:8080/;
}
```

The sidecar does not bind ports 8080 or 9090. Only port 8081 is newly owned by
the sidecar. Avoiding a `/ros` proxy also avoids adding another endpoint capable
of transporting the complete bidirectional rosbridge protocol.

## 8. ROS data contract

The implemented dashboard's default read-only factory inputs are:

- `/scan_raw`: `sensor_msgs/msg/LaserScan`
- `/ascamera/camera_publisher/rgb0/image`: `sensor_msgs/msg/Image`
- `/odom`: `nav_msgs/msg/Odometry`
- `/ros_robot_controller/battery`: `std_msgs/msg/UInt16`

Depth images, point clouds, camera info, IMU, `/tf`, and `/tf_static` remain
available for future read-only features.

The implemented browser dashboard publishes no ROS topics. Possible future
sidecar outputs include:

- `/fan/camera/annotated`: annotated image
- `/fan/vision/detections`: recognition results
- `/fan/lidar/points`: throttled or simplified LiDAR representation
- `/fan/status`: sidecar health and version

Every future output must be informational. No `/fan/*` node may bridge or remap
its output to a factory motion or actuator topic.

The exact live topics and QoS must be captured from a powered factory robot
before hardware integration is considered validated. A mounted disk proves
installed configuration but not runtime publication or hardware health.

## 9. LiDAR web representation

The factory image contains RViz configurations but no built-in LiDAR browser
viewer was found. The sidecar UI will subscribe to `/scan_raw` through the
existing rosbridge connection.

For scan element `i`:

```text
angle = angle_min + i * angle_increment
x = range[i] * cos(angle)
y = range[i] * sin(angle)
```

The implemented Canvas renderer:

- Throttles updates to approximately 10 Hz for browser display.
- Keeps only the latest scan.
- Discards non-finite and out-of-range samples.
- Draws the scan in the robot-local frame.
- Shows connection state, scan age, sample count, and scale.

Map and route overlays are deferred with driving/navigation, although read-only
display of `/map` may be added without enabling motion.

## 10. Image build and runtime constraints

The implemented frontend-only sidecar uses the following fully qualified,
multi-architecture, digest-pinned Nginx base:

```dockerfile
FROM docker.io/library/nginx:stable-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c
```

Omitting ROS from this release makes the no-controller boundary structural and
keeps the image independent of the vendor ROS filesystem. A later perception
worker should use a separately reviewed, digest-pinned ROS Humble base.

Do not build with an unqualified `FROM ros:humble` on the vendor Pi. That tag
already identifies Hiwonder's imported image locally. Do not pull or retag the
factory `ros:humble` image.

Build for `linux/arm64`, preferably off-device, then transfer only the resulting
`mentorpi-fan:<version>` image to the Pi.

The production container must have:

- `network_mode: host`
- A unique `MentorPiFan` container name
- No `privileged` mode
- No `/dev` bind mount
- No added Linux capabilities
- `no-new-privileges:true`
- A read-only root filesystem
- A writable `tmpfs` mount for `/tmp`
- A non-root runtime user

The implemented Compose shape is:

```yaml
name: mentorpi-fan

services:
  mentorpi-fan:
    image: mentorpi-fan:latest
    container_name: MentorPiFan
    restart: unless-stopped
    network_mode: host
    user: nginx
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=16m
```

Because host networking is used, the Compose file must not declare `ports:`.
It must manage only `MentorPiFan`; it must never recreate, stop, or remove the
factory `MentorPi` container.

## 11. Startup and dependency behavior

Compose cannot express `depends_on` for a container it does not manage. The
sidecar therefore needs application-level readiness behavior:

1. Start its UI immediately and show the factory connection as unavailable.
2. Retry rosbridge and a user-started camera stream with bounded backoff.
3. Report camera and LiDAR state independently.
4. Never attempt to restart or reconfigure the factory container.

An optional additive host systemd service may order `MentorPiFan` after Docker
and `start_node.service`, but readiness checks are still required because
systemd ordering does not guarantee ROS graph readiness.

## 12. Security

The factory launch does not visibly configure TLS or authentication for
rosbridge or `web_video_server`. Rosbridge can potentially publish topics and
call services, so ports 8080 and 9090 must not be exposed to an untrusted
network.

If the port-8081 UI later adds authentication, host firewall rules must still
block untrusted clients from reaching the original 8080/9090 listeners. UI
authentication alone does not secure the factory services.

The sidecar image and configuration must not contain factory Wi-Fi passwords,
API keys, shell history, credentials, or other secrets extracted from the disk
image.

## 13. Validation and acceptance criteria

Before deploying to the Pi:

- Build succeeds for `linux/arm64`.
- The image is tagged `mentorpi-fan:<version>` and never `ros:humble`.
- Runtime inspection shows no privileged/device configuration.
- Image inspection confirms there is no `/opt/ros`; source scans show no
  controller, driver, Nav2, teleop, or stock driving applications.
- Static scans find no publisher or command invocation targeting prohibited
  motion and actuator topics.

On a powered robot:

- `MentorPi` remains running with its original image ID and configuration.
- The browser reaches the factory ROS graph directly through port 9090.
- Camera, LiDAR, odometry, and battery telemetry are visible in the dashboard.
- The UI is available on port 8081.
- Port 8080 remains owned only by the factory web-video server.
- Port 9090 remains owned only by the factory rosbridge server.
- RGB streaming works through `/video/stream?...` or the direct factory URL.
- LiDAR renders from `/scan_raw` without starting another LiDAR driver.
- Any later recognition output appears only on `/fan/*` topics.
- `ros2 topic info --verbose /controller/cmd_vel` shows no publisher belonging
  to `MentorPiFan`.
- No sidecar node publishes `/ros_robot_controller/*` actuator commands.
- Stopping and removing `MentorPiFan` has no effect on the factory robot stack.

## 14. Rollback

Rollback must require only stopping and removing the sidecar:

```bash
docker stop MentorPiFan
docker rm MentorPiFan
```

The factory `MentorPi` container and `start_node.service` are not part of the
sidecar Compose project and must continue operating normally.

The deployment script captures the previous sidecar image and container IDs
before rollout. If deployment fails before replacement, the prior container is
left running. If the new container fails health or runtime-isolation checks, it
is removed and the previous image tag is restored when available. The script
does not restart an old image through potentially incompatible new
configuration. Cleanup never operates on the factory container.

## 15. Implementation and remaining robot checks

Version 0.1.0 is implemented in:

- `docker/customization/Dockerfile`
- `docker/customization/nginx.conf`
- `docker/customization/web/`
- `docker/customization/docker-compose.yml`
- `docker/customization/deploy.sh`

The local ARM64 build, Nginx configuration, HTTP health endpoint, Compose
project isolation, and container security settings were validated on
2026-09-05. Hardware-dependent checks still require a powered factory robot:

- Capture `ros2 node list`, `ros2 topic list`, `ros2 topic info --verbose`, and
  relevant QoS from a powered factory robot.
- Confirm the actual camera and LiDAR topics for the installed hardware.
- Confirm factory ports 8080 and 9090 are listening after boot.
- Define the first recognition model and its CPU/memory budget on Raspberry Pi
  5.
- Make disk-image inspection read-only; the current `pi_enter.sh` copies QEMU
  files and edits `ld.so.preload` inside the mounted image.
