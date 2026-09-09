# MentorPi Native Tank Controller

Status: Milestone 1 (Repository scaffold, source provenance, dependency audit, and modular safety boundaries).
Target: Hiwonder MentorPi Tank (Raspberry Pi 5 ARM64 + STM32 RRC chassis controller)
Runtime: Native ROS 2 Lyrical on Ubuntu 26.04 LTS (No Docker).

---

## 1. Overview

This workspace provides a fresh, native controller-only implementation for the Hiwonder MentorPi Tank. It operates directly on a clean Ubuntu 26.04 installation without containerization, designed to support bounded tank motion:
- Drive forward (`W`)
- Drive reverse (`S`)
- Turn left (`A`)
- Turn right (`D`)
- Stop within 150 ms lease (`Space` or key repeat cessation)

**Current Implementation Scope**: This delivery is hardware-free repository scaffolding and modular unit-tested components. It does not yet build or deploy the full graph, arm the hardware, or drive physical motors. Full end-to-end guarded bringup, bridge hardening (watchdog and heartbeats), and deployment are assigned to future milestones (Milestones 2-6); operational commands in `deploy.sh` (`build`, `arm`, `disarm`, `install`, `service`, `bench`) are documented stubs. In the target architecture, actuator commands are strictly guarded by `ubuntu_tank_safety`, monitored by `ubuntu_tank_supervisor`, and commanded via renewable short leases with `ubuntu_tank_teleop`.

---

## 2. Target Architecture & Safety Invariants

1. **Disarmed by default**: `motor_guard` initializes in the disarmed state on every boot, crash, or restart.
2. **Explicit arming**: Arming requires an explicit operator action via service call or `./deploy.sh arm --ack-tracks-raised` and is never persisted.
3. **Four-motor validation**: Every command must contain unique motor IDs {1, 2, 3, 4} with finite, bounded RPS values (`<= max_rps`).
4. **Monotonic freshness**: Commands older than 250 ms cause immediate disarm and repeated 4-motor zero command publication.
5. **Trusted AND-gating**: `ubuntu_tank_supervisor` notifies systemd watchdog only while both guard and bridge heartbeats are strictly fresh. Socket heartbeats compare Linux `SO_PASSCRED` PID and UID credentials with the expected runtime identity to catch accidental or misconfigured senders. This personal, single-owner deployment trusts owner-approved same-UID software; inherited pipe heartbeats remain available as defense in depth. One healthy child cannot mask a hung child.
6. **Renewable teleop leases**: Teleop emits 150 ms command leases (< 250 ms guard timeout). Halts motion within the configured 150 ms lease after key repeat ceases, terminal input ends, or focus is lost (as raw terminal input does not report key-up events).
7. **Fail-closed zeroing**: Process signals (SIGINT, SIGTERM), exceptions, or timeouts trigger repeated zero commands.

---

## 3. Workspace Layout

```text
ubuntu_tank/
├── README.md                         # Canonical operator guide
├── deploy.sh                         # Unified deployment and operations entrypoint
├── VERSION                           # Release version (1.0.0)
├── versions.lock                     # Draft dependency scope specification for Milestone 2 lock (schema lockfile-v1)
├── source-manifest.txt               # Complete source provenance and boundary audit
├── docs/
│   ├── RELEASE_MANIFEST_SPEC.md     # Version-plus-revision release ID and manifest schema
│   └── DEPENDENCY_CLOSURE.md        # Direct dependency verification, AST import audit, and target closure specification
├── config/
│   ├── controller.yaml               # Production kinematic & safety configuration defaults
│   └── sros2/
│       └── README.md                 # Deny-by-default SROS2 governance specification
├── host/
│   ├── 99-mentorpi-rrc.rules         # Restricted and verified udev serial symlink template
│   ├── mentorpi-tank.service         # Hardened native systemd unit template
│   └── mentorpi-tank.env             # Non-secret runtime environment defaults
├── scripts/
│   ├── install_ros2.sh               # Ubuntu and ROS repository/package setup scaffold
│   ├── build_workspace.sh            # rosdep and colcon build scaffold
│   ├── check_host.sh                 # Read-only OS, architecture, power, and device preflight
│   ├── recover_activation.sh         # Boot-time write-ahead transaction recovery runner
│   └── verify_runtime.sh             # ROS graph, topic ownership, and zero-state verification
├── src/
│   ├── ros_robot_controller_msgs/    # Complete reused vendor ROS 2 interfaces
│   ├── ros_robot_controller/         # Reused vendor STM32 serial bridge with full dependencies
│   ├── controller/                   # Reused vendor tank kinematics and odometry publisher
│   ├── ubuntu_tank_safety/           # Disarmed-by-default motor guard package
│   ├── ubuntu_tank_supervisor/       # Trusted AND-gating systemd watchdog supervisor
│   └── ubuntu_tank_teleop/           # Safe keyboard teleoperation with renewable leases
└── tests/
    ├── test_source_boundary.sh       # Provenance, AST import, allowlist, and zero-perception gate
    └── test_negative_boundary.sh     # Negative regression test proving rejection of undeclared dependencies
```

---

## 4. Verification and Testing

Run the automated test suite without hardware:
```bash
./deploy.sh test
```

This verifies:
1. **Source Boundary & Provenance Gate** (`tests/test_source_boundary.sh`):
   - 92 payload files validated with provenance out of 93 Git-tracked files (`source-manifest.txt` intentionally self-excluding).
   - SHA-256 integrity match between manifest and disk.
   - Authoritative Git object-store validation for all reused code from `mentorpi/src`.
   - Zero fallbacks to `/mnt/rpi-rootfs`.
2. **Directory Layout Assertion**:
   - Confirms all required top-level directories (`config`, `host`, `scripts`, `docs`, `src`, `tests`) and tracked scaffold files exist and are tracked in Git.
3. **AST-Based Python Import & Dependency Audit**:
   - Parses AST of every Python source file in `src/`.
   - Verifies that 100% of runtime Python imports (including cross-workspace packages) are formally declared in package manifests (`package.xml`).
   - Rejects undeclared direct imports and cross-package dependencies.
   - Validated by automated negative regression testing (`tests/test_negative_boundary.sh`) proving gate failure when dependencies are omitted.
4. **Direct Dependency Audit & Controller-Only Allowlist**:
   - Audits direct package dependencies against the strict controller-only allowlist (recursive transitive resolution and package hashes reserved for Milestones 2 & 3).
   - Rejects any perception packages (`cv_bridge`, `image_transport`, `laser_geometry`, `nav2_*`, `robot_localization`).
   - Verifies direct AST imports from `sensor_msgs`: permits only non-perception messages (`Imu`, `Joy`, `JointState`) and forbids camera/LiDAR messages (`Image`, `LaserScan`, `PointCloud2`, `CameraInfo`).
   - Scans source text for any forbidden perception, vision, or AI tokens.
5. **Modular Unit & Integration Tests**:
   - **Motor Guard Safety Unit Tests** (`ubuntu_tank_safety`): Disarmed startup, explicit arm/disarm, 4-motor validation, NaN/Inf rejection, speed limit enforcement, 250 ms monotonic timeout, and repeated zero emission (11 tests).
   - **Supervisor Unit Tests** (`ubuntu_tank_supervisor`): AND-gating state machine, Linux `SO_PASSCRED` PID/UID credential checks, unconfigured PID rejection, and inherited kernel pipes (17 tests).
   - **Supervisor Credential Integration Tests** (`ubuntu_tank_supervisor`): Verifies running `supervisor_node` rejects credential PID mismatches, fails closed when PIDs are unset, and operates on inherited pipes (3 tests). Malicious same-UID PID-file replacement is outside the single-owner host threat model.
   - **Teleop Lease Unit Tests** (`ubuntu_tank_teleop`): W/A/S/D mapping, 150 ms renewable lease, timeout zeroing, space bar stop, and unsafe lease rejection (8 tests).

---

## 5. Next Milestones

- **Milestone 2**: Ubuntu and ROS installation workflow (`check-host`, `prepare-host`, `verify-lock`, `install-ros`).
- **Milestone 3**: Lyrical port, dependency closure, and serial bridge hardening.
- **Milestone 4**: Guarded bringup launch graph and SROS2 security enclaves.
- **Milestone 5**: Native host deployment under `/opt/ubuntu_tank` and systemd confinement.
- **Milestone 6**: Raised-track bench acceptance and latency validation.
