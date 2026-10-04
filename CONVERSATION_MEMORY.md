# MentorPi Conversation Memory

Last updated: 2026-10-03

Keep this file concise. Read `AGENTS.md` for working rules, `GEMINI.md` for platform safety,
and `docs/DESIGN.md` for architecture, control contracts, and deployment workflows.

## 1. Current State & Release

- **Target & Packaging:** Raspberry Pi 5 ARM64 running Ubuntu 26.04 with ROS 2 Lyrical.
  Delivered as paired ARM64 Docker containers (`runtime` and `web`) managed via Docker Compose.
- **Current Accepted Release:**
  - Release ID: `4b320e602524306678496ec7a008e0e454f6d81a66301984f6d7518df92928db`
  - Source Commit: `2c5bb5d`
  - Protocol Version: `3.0.0`
- **Acceptance Status:**
  - Owner-scoped acceptance complete for Windows Chrome over Wi-Fi with tracks raised.
  - Physical stop timing/distance measurements were waived by the owner, not certified.
  - Android/iOS and mobile PWA testing are excluded (reserved for future client apps).
  - On-ground motion remains forbidden; operation is elevated/raised-track only.
- **Known Target Limitation:** The Pi lacks an RTC battery. If cold-booted without network,
  its system clock may precede TLS certificate validity, causing `ubuntu-tank-container.service`
  to fail until time is synced via NTP.

## 2. Key Operational Commands

- Fresh-Pi preparation: [host guide](docs/PI5_HOST_SETUP.md). The workstation
  wrapper supports `--first-install`, `--serial-device`, and repeatable
  `--tls-hostname`/`--tls-ip`; fresh-Pi execution of this path remains pending.

- **Local Validation:**
  ```bash
  .venv/bin/python ./ubuntu_tank/deploy.sh test
  ```
- **Build & Remote Deployment (from development workstation):**
  ```bash
  ./docker/ubuntu_tank/deploy.sh user@ROBOT_IP
  # Or reuse a completed build directory:
  ./docker/ubuntu_tank/deploy.sh user@ROBOT_IP --build-dir /path/to/build_output
  ```
- **Pi-Side Target Verification & Service Management:**
  ```bash
  # Check installed stopped integration:
  sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py target-test

  # Service status, restart, and recovery:
  sudo systemctl status ubuntu-tank-container.service
  sudo systemctl restart ubuntu-tank-container.service
  ```

## 3. Architecture & Safety Rules

- **Hardware Ownership:** Dedicated runtime container has exclusive access to `/dev/rrc`.
  Factory container (`MentorPi`) and customization sidecar (`MentorPiFan`) run on separate
  vendor media and must never coexist with this native Ubuntu controller.
- **Confinement:** Runtime uses `network_mode: none` with DDS restricted to loopback under SROS2.
  Web service exposes HTTPS (port 8443) only and has no ROS runtime or serial access.
- **Ephemeral State:** All inter-container sockets (`operator.sock`, `lifecycle.sock`) reside in
  host `/run/ubuntu_tank-container/ipc` (UID 10001, mode 0700). Ownership, arming, and leases
  are strictly ephemeral and clear on reboot or container restart.
- **Control Rules:**
  - Take Control acquires operator slot disarmed; Start arms only for a new direction press.
  - Direction movement requires press-and-hold (W/S/A/D or web UI).
  - 1.0 s input lease expiry pauses motion (`INPUT_PAUSED`); recovery requires control release,
    acknowledged neutral, and a fresh press.
  - Space or Stop disarms immediately while retaining ownership.
  - 300 s inactivity releases ownership and shuts down the controller.
- **Workflow:** Never commit or push without explicit user direction. Format Python code with
  `ruff format`, shell scripts with `shfmt`, and verify with `shellcheck`.

## 4. Planned Camera Feature

- All design documents belong in `docs/`.
- [Camera design](docs/DESIGN_CAMERA.md) defines live preview, JPEG capture, and Pi-side
  video recording in the existing web console.
- CAM-1 implemented: delivers `CameraPanel.vue` integrated beside drive controls on desktop
  and above on narrow screens, standalone review mode (`?review=camera`) with inert driving controls,
  bundled SVG placeholder, two action buttons (`Capture` and `Record` / `Stop recording`),
  simulated recording timer, feedback messages, example download link, and review fixtures.
- CAM-2 implemented:
  - Runtime camera worker (`ubuntu_tank_camera`) implementing V4L2 device streaming (`/dev/video0`)
    with explicit mock/simulation opt-in, JPEG frame validation, frame freshness tracking (`LIVE` -> `STALE`
    after 2.0s without frames), idle timeout release after 30s with zero streaming clients, disconnect/reconnect
    handling, and slow-viewer frame dropping.
  - Dedicated Unix domain socket IPC at `/run/ubuntu_tank/camera.sock` (mode 0700) with binary MJPEG framing.
  - Web API endpoints: `GET /api/v1/camera/status`, `GET /api/v1/camera/stream` (multipart/x-mixed-replace),
    and 503 stubs for future CAM-3/CAM-4 endpoints (`POST /api/v1/camera/captures`, `POST /api/v1/camera/recordings`).
  - Restricted device access: `/dev/video0` admitted alongside `/dev/rrc` for the `runtime` container in
    `docker/ubuntu_tank/install.py`; `web` container retains zero hardware device access.
  - Supervisor config: `[program:camera]` (`priority=25`, `autorestart=true`) decoupled from controller
    runtime monitor so camera issues never fault the motion controller.
  - Production UI in `CameraPanel.vue`: polls camera status every 1.5s, binds live `<img :src="streamUrl">`
    on live/stale states, pauses status polling on `visibilitychange` (when document is hidden), and disables
    Capture/Record with clear tooltips, while preserving 100% of CAM-1 review mode.
  - Verified with 16 camera worker tests (`test_camera_worker.py`), 51 browser PWA Playwright tests
    (`test_milestone13_browser_pwa.py`), container tests, and AST boundary/dependency closure gates.
  - Missing V4L2 hardware reports unavailable and retries; it never silently generates preview frames.
    Runtime camera mapping preserves writable `/dev/null` when absent and grants the observed video GID
    when present. Real-Pi no-camera startup and mode-0660 camera capture remain pending.
- CAM-3 through CAM-5 cover capture snapshot storage, recording/recovery, and real-Pi integration.
