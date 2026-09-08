# MentorPi Fan Container

`mentorpi-fan` is the observer-only web sidecar for the factory MentorPi robot.
It serves a dashboard on port 8081 and reuses the factory container's camera
HTTP service on port 8080 and rosbridge WebSocket on port 9090.

The image contains only Nginx and static browser assets. It has no ROS runtime,
controller, navigation, teleoperation, hardware driver, actuator API, device
mount, or privileged capability.

## Public HTTP surface

- `GET /`: camera, LiDAR, odometry, and battery dashboard.
- `GET /healthz`: sidecar liveness check; returns `ok` without testing factory
  hardware or services.
- `GET /video/*`: read-only reverse proxy to factory `web_video_server` on
  `127.0.0.1:8080`.

The dashboard connects directly to the factory rosbridge at
`ws://ROBOT_IP:9090`; the sidecar deliberately does not expose a second
rosbridge proxy. Its JavaScript sends only `subscribe` and `unsubscribe`
operations. The factory port still transports the complete rosbridge protocol,
so the robot must remain on a trusted network until an authenticated,
allow-listed gateway is implemented.

After a user starts the camera, a failed stream retries with bounded exponential
backoff. The rosbridge connection also reconnects automatically.

## Local build and smoke test

From the repository root:

```bash
docker compose -f docker/customization/docker-compose.yml build
docker compose -f docker/customization/docker-compose.yml up -d
docker compose -f docker/customization/docker-compose.yml ps
curl http://127.0.0.1:8081/healthz
```

Or build, start, and wait for health with:

```bash
./docker/customization/deploy.sh local
```

Open `http://localhost:8081/`. Without factory services on local ports 8080 and
9090, the dashboard correctly reports the camera and ROS bridge as unavailable;
the UI and health endpoint still work.

When the ARM64 image runs on an x86 host through QEMU, Nginx may log
`io_setup() failed (38: Function not implemented)` while probing asynchronous
file I/O. The HTTP server remains usable and healthy; this emulation-only probe
does not occur on the native ARM64 Raspberry Pi kernel.

Stop only the sidecar with:

```bash
docker compose -f docker/customization/docker-compose.yml down
```

This Compose file never defines or manages the factory `MentorPi` container.
Its explicit project name is `mentorpi-fan`, so Compose does not treat the
factory container as part of this project's lifecycle.

## Raspberry Pi deployment

The deployment script verifies that the remote host is ARM64 and that its
factory `MentorPi` container is running. It then builds, transfers, and starts
only the sidecar:

```bash
./docker/customization/deploy.sh remote pi@ROBOT_IP
```

A failed rollout preserves the prior container if it was not replaced;
otherwise it removes the failed container and restores the prior image tag when
available. Browse to `http://ROBOT_IP:8081/` after deployment.

## Runtime verification

```bash
docker inspect MentorPiFan --format '{{json .HostConfig.Privileged}} {{json .HostConfig.Binds}} {{json .HostConfig.Devices}} {{json .HostConfig.CapDrop}} {{json .HostConfig.ReadonlyRootfs}}'
docker logs MentorPiFan
curl http://127.0.0.1:8081/healthz
```

The inspection result must be `false null null ["ALL"] true`. The factory
container remains the exclusive owner of hardware and robot control.
