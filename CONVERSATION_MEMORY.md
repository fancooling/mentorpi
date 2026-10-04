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
  Verified with 50 Playwright tests (`npm test` / `test_milestone13_browser_pwa.py`).
  Owner UI approval is required before starting CAM-2 or backend/hardware integration.
- CAM-2 through CAM-5 cover live preview, capture, recording/recovery, and real-Pi
  integration and safety validation. Camera model/driver and resource limits remain unverified.
