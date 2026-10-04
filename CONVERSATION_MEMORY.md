# MentorPi Conversation Memory

Last updated: 2026-10-04

Keep this file concise. Read `AGENTS.md` for working rules, `GEMINI.md` for platform safety,
and `docs/DESIGN.md` for architecture, control contracts, and deployment workflows.

## 1. Current State & Release

- **Target & Packaging:** Raspberry Pi 5 ARM64 running Ubuntu 26.04 with ROS 2 Lyrical.
  Delivered as paired ARM64 Docker containers (`runtime` and `web`) managed via Docker Compose.
- **Historical M16 Accepted Release:**
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

## 4. Camera implementation and Pi deployment (2026-10-04)

- [Camera design](docs/DESIGN_CAMERA.md). Pi test evidence is temporary local output;
  `docs/*_PI_VALIDATION.md` is ignored. Keep durable outcomes in this checkpoint.
- CAM-1 review UI remains available. CAM-2 live preview now supports the actual
  Aurora 930 (`3251:1930`) through bundled ARM64 Deptrum SDK 1.1.22; RGB NV12
  becomes JPEG at 640×400, nominal 10 fps. No host ROS or kernel camera driver.
- Host preparation enrolls one camera by serial and maintains private USB nodes
  under `/dev/ubuntu-tank-camera-usb`, mode 0660, group 10001. Runtime alone gets
  this read-only USB mount; web has no hardware access. SDK runs in a bounded
  subprocess; camera IPC stays separate from operator/lifecycle sockets.
- Capture releases hardware after 30 seconds without viewers, retries failures,
  and discovers late-arriving cameras even after idle timeout. Browser outages
  show unavailable and reconnect using a fresh stream URL. Capture/Record remain
  disabled until CAM-3/CAM-4.
- Deployed release: `c1043ee01cf34a58eadc6e8ed23c5818fdd805bbb6a9cd042239e6c065fac66a`,
  built from `bec2c892` plus uncommitted camera changes. Build directory:
  `ubuntu_tank/.work/cam2-aurora-complete`; builder `mentorpi-c3`.
- Pi service active; runtime/web healthy, stopped/disarmed/ownerless. Final stream:
  150 JPEG frames at 640×400, 9.55 fps. Late camera arrival recovered in 1.145 s
  without a viewer; Chrome reload and 35-second network outage/reconnect passed.
  Earlier same-path checks passed helper hang recovery, USB authorization recovery,
  installed udev node recreation and idle release. Owner confirmed live video
  streaming works. No motors moved.
- Software validation: full development run 587 Python cases (one native ROS skip),
  latest focused camera suite 22 passed, browser suite 52 passed, independent review
  PASS. Physical cable unplug/replug remains pending, as do motor-load checks,
  V4L2 target validation and fresh-Pi first-install validation.
