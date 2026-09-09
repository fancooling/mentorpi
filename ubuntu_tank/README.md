# MentorPi Native Tank Controller

Status: Milestone 1 completed; Milestones 2 and 3 implemented and hardware-free tested, with clean ARM64 target execution and acceptance still pending.
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

**Current Implementation Scope**: This delivery provides the hardware-free repository scaffold, native host preparation and ROS installation workflow, Lyrical source port, serial-bridge watchdog/error handling, and workspace build command. A clean Ubuntu 26.04 ARM64 rosdep/colcon build and installed-artifact test have not run. Full guarded bringup, SROS2 enforcement, native deployment, and physical acceptance remain Milestones 4-6; `arm`, `disarm`, `package`, `install`, `activate`, `rollback`, `start`, `stop`, and `bench` remain stubs. In the target architecture, actuator commands are strictly guarded by `ubuntu_tank_safety`, monitored by `ubuntu_tank_supervisor`, and commanded via renewable short leases with `ubuntu_tank_teleop`.

### Development and target environments

Milestone 2 does not require ROS 2 or Pi-specific drivers on a general-purpose
development workstation. Use that workstation for authoring, read-only lock
verification, and static, mocked, or hardware-free tests. Run `check-host`,
`prepare-host`, `install-ros`, EEPROM/device integration, and the Milestone 2
acceptance test on a clean Ubuntu 26.04 ARM64 Raspberry Pi. Later ROS builds and
integration tests require the target Pi or a separate compatible ARM64 build
host using the isolated disposable build root; they do not require ROS in the
development workstation's base operating system.

If Python-only workstation development dependencies are needed, install them in
the single `.venv` at the repository root, with their purpose and version
recorded by the project. Do not create nested environments, add a parallel Conda
environment, or install non-standard Python packages globally. Target/build-root
Python packages remain apt/ROS-managed and locked. ROS, native libraries, udev
rules, and kernel or USB drivers cannot be supplied by a Python environment. Do
not create or activate a Python virtual environment or Conda environment on the
target Raspberry Pi controller; the native controller mode uses host-managed apt
and locked system packages exclusively.

### Target-Pi Operator Procedure (Milestone 2 Acceptance)

Follow this exact sequence on a clean Raspberry Pi 5 with Ubuntu 26.04 LTS (Resolute):

1. **Preflight Check**:
   ```bash
   cd ubuntu_tank
   ./deploy.sh check-host
   ```
   Asserts Raspberry Pi 5 hardware model, ARM64 architecture, bootloader EEPROM firmware date (`>= 2024-05-17`), UTF-8 locale, minimum 5GB disk space, and time sync. Checks character device `/dev/rrc` (asserts exclusive access if present, prints informational message if not yet connected), and asserts zero conflicting Docker containers (`MentorPi`, `MentorPiFan`) or factory systemd units.

2. **Prepare Host & Establish Baseline**:
   ```bash
   ./deploy.sh prepare-host
   ```
   Acquires root `/run/lock/ubuntu_tank/deploy.lock`, records pre-upgrade baseline (`host-baseline-pre.txt`), upgrades base Ubuntu packages, configures locales, enables universe, and records accepted post-upgrade baseline (`host-baseline-post.txt`).

3. **Reboot if Required & Refresh Baseline**:
   If `/run/reboot-required` exists (or kernel was upgraded), reboot the target Pi:
   ```bash
   sudo reboot
   ```
   After reboot, rerun `./deploy.sh prepare-host` to verify the upgraded system and record the accepted baseline matching the running kernel:
   ```bash
   cd ubuntu_tank
   ./deploy.sh prepare-host
   ```

4. **Verify Dependency Lockfile**:
   ```bash
   ./deploy.sh verify-lock
   ```
   Pure data validation of the recorded package entries, cryptographic SHA256 hashes, architectures, and closure state in `versions.lock`. The committed lock is explicitly `direct-only`; live ROS/dependency installation fails closed until a clean ARM64 target transaction has been captured, reviewed, and marked `complete`.

5. **Install ROS 2 Lyrical & Build Tools**:
   ```bash
   ./deploy.sh install-ros
   ```
   Requires `closure_status: complete` before any live mutation. Once a complete lock is committed, it verifies the root deployment lock and accepted baseline, downloads and verifies the official `ros2-apt-source` deb package via SHA-256, configures the repository, downloads candidate `.deb` packages into an isolated per-run apt cache, and derives a manifest from every downloaded artifact. `verify-closure` then checks the exact transaction package set, versions, architectures, independently resolved repository classes, and artifact hashes against `versions.lock` before apt installs from that cache with network downloads disabled. Live installation does not accept a caller-supplied candidate manifest.

6. **Install Locked Workspace Dependencies**:
   ```bash
   ./deploy.sh install-deps
   ```
   Requires a complete lock, downloads and verifies pinned rosdep snapshot sources (`index_v4`, `base`, `python`, `ruby`), configures the local snapshot URL, resolves workspace package keys with authentic rosdep resolution, asserts 100% presence in `versions.lock`, verifies that `install-ros` already installed every resolved package at its exact locked version, and checks dpkg and workspace closure without downloading packages.

7. **Apt / Network Interruption Recovery**:
   `install-ros` refuses network-enabled package recovery after the verified transaction begins and preserves its isolated archive cache on failure. Do not run `apt-get --fix-broken install` at that point because it can fetch artifacts outside the reviewed closure. Inspect the reported cache, restore the accepted host baseline or clean image, and retry only after resolving the underlying failure. `prepare-host` may use normal apt recovery before the locked ROS transaction starts.

8. **Milestone 2 Final Acceptance Verification**:
   ```bash
   ./deploy.sh check-host
   ./deploy.sh test
   ```

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
├── versions.lock                     # Authoritative locked dependency closure with SHA256 hashes
├── source-manifest.txt               # Complete source provenance and boundary audit
├── docs/
│   ├── RELEASE_MANIFEST_SPEC.md     # Version-plus-revision release ID and manifest schema
│   └── DEPENDENCY_CLOSURE.md        # Direct dependency verification, AST import audit, and locked closure specification
├── config/
│   ├── controller.yaml               # Production kinematic & safety configuration defaults
│   └── sros2/
│       └── README.md                 # Deny-by-default SROS2 governance specification
├── host/
│   ├── 99-mentorpi-rrc.rules         # Restricted and verified udev serial symlink template
│   ├── mentorpi-tank.service         # Hardened native systemd unit template
│   └── mentorpi-tank.env             # Non-secret runtime environment defaults
├── scripts/
│   ├── install_ros2.sh               # Ubuntu and ROS repository/package setup script (Milestone 2)
│   ├── build_workspace.sh            # rosdep and colcon build scaffold
│   ├── check_host.sh                 # Read-only OS, architecture, power, and device preflight (Milestone 2)
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
    ├── test_negative_boundary.sh     # Negative regression test proving rejection of undeclared dependencies
    ├── test_dependency_closure.sh    # Dependency closure, lock verification, and tamper detection gate
    ├── test_install_workflow.py      # Unit tests for host preflight, mutual exclusion, and lock verification
    └── test_milestone3_port.py       # Unit tests for Milestone 3 porting, watchdogs, heartbeats, and build script
```

---

## 4. Verification and Testing

Run the automated test suite without hardware:
```bash
./deploy.sh test
```

This verifies:
1. **Source Boundary & Provenance Gate** (`tests/test_source_boundary.sh`):
   - 95 payload files validated with provenance out of 96 Git-tracked files (`source-manifest.txt` intentionally self-excluding).
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
5. **Dependency Closure & Lock Verification Gate** (`tests/test_dependency_closure.sh`):
   - Validates `versions.lock` as structured data with 100% pinned versions, architectures, repositories, and SHA-256 hashes.
   - Audits that 100% of workspace declared external dependencies across all tags are locked.
   - Executes 8 automated negative regression tests proving rejection of unpinned versions, forbidden perception packages, invalid hashes, omitted dependencies, unpinned URLs, candidate 64-char hash mismatches, unlocked transitive packages, and missing candidate manifests.
6. **Installation Workflow Unit Tests** (`tests/test_install_workflow.py`):
   - Preflight OS, architecture, Raspberry Pi 5 model, authentic EEPROM output parsing, JSON escaping, locale, disk, time, fail-closed malformed EEPROM, fail-closed missing fuser with serial device, and non-mock process table clean scan (11 tests).
   - Mutual exclusion enforcement rejecting `MentorPi`, `MentorPiFan`, replacement containers, unreadable Docker daemons, non-character device paths, and serial device contention (6 tests).
   - Authoritative lock verification, malformed schema rejection, and incomplete/complete transitive-closure live gates (4 tests).
   - Dry-run validation of `install-ros`, `prepare-host`, `install-deps`, and solver closure candidate validation and tamper rejection (5 tests).
   - Security guards asserting all mock/override variables are strictly rejected for live mutations, help options never mutate, and unknown options fail (3 tests).
   - Host-global deployment lock mutual exclusion and contention rejection (1 test).
   - Apt state recovery fault injection and deb artifact preservation (1 test).
   - Reboot sequence handling pending reboot exit code 2 and kernel baseline mismatch rejection (2 tests).
   - Non-mutating upstream inputs verification asserting `ros2-apt-source` deb and all 4 pinned rosdep snapshot sources (`index_v4`, `base`, `python`, `ruby`) exist and match pinned SHA256 (1 test).
   - External working directory test asserting gate passes when invoked from `/tmp` (1 test).
7. **Modular Unit & Integration Tests**:
   - **Motor Guard Safety Unit Tests** (`ubuntu_tank_safety`): Disarmed startup, explicit arm/disarm, 4-motor validation, NaN/Inf rejection, speed limit enforcement, 250 ms monotonic timeout, and repeated zero emission (11 tests).
   - **Supervisor Unit Tests** (`ubuntu_tank_supervisor`): AND-gating state machine, Linux `SO_PASSCRED` PID/UID credential checks, unconfigured PID rejection, and inherited kernel pipes (17 tests).
   - **Supervisor Credential Integration Tests** (`ubuntu_tank_supervisor`): Verifies running `supervisor_node` rejects credential PID mismatches, fails closed when PIDs are unset, and operates on inherited pipes (3 tests). Malicious same-UID PID-file replacement is outside the single-owner host threat model.
   - **Teleop Lease Unit Tests** (`ubuntu_tank_teleop`): W/A/S/D mapping, 150 ms renewable lease, timeout zeroing, space bar stop, and unsafe lease rejection (8 tests).
8. **Milestone 3 Porting and Hardening Unit Tests** (`tests/test_milestone3_port.py`):
   - Elimination of `MACHINE_TYPE` environment variable across all sources and build failure closed if set.
   - Removal of hardcoded `/home/ubuntu/software` configuration paths from all source files.
   - STM32 bridge Board SDK mock mode initialization, buffer packet capture with CRC8 calculation, 50 ms read polling, 100 ms write timeout, 500 ms receive-silence faulting, signal-safe close, and fail-closed serial open/read/write errors.
   - Bridge 250 ms monotonic freshness watchdog triggering repeated 4-motor zero packets on command loss and suppressing supervision after fatal serial faults.
   - Bridge non-ROS supervisor heartbeat emission via pipe FD and socket datagrams.
   - Controller kinematics parameterization, applied/validated correction factors, controller-only topic routing to safety guard, and command integration docstrings.
   - Launch argument declaration and node parameter mapping in `ros_robot_controller.launch.py`.
   - Supervisor independent child deadline evaluation and single watchdog authority.
   - Colcon build script with `--dry-run`, constrained `--clean`, `--merge-install`, and `--packages` flags.
   - Complete isolated apt-archive enumeration, unreadable/duplicate artifact rejection, required architecture/repository fields, unlocked-transitive exposure, live candidate-override rejection, and package metadata synchronization (42 tests total).

---

## 5. Implementation Status & Next Milestones

- [x] **Milestone 1**: Repository scaffold and provenance (Completed).
- [ ] **Milestone 2**: Target-Pi Ubuntu and ROS installation workflow (`check-host`, `prepare-host`, `verify-lock`, `verify-closure`, `install-ros`, `install-deps`) (In-progress: workflow implementation and hardware-free gates complete; clean-Pi hardware acceptance pending).
- [ ] **Milestone 3**: Lyrical port, dependency closure, and serial bridge hardening (In-progress: code porting, fatal error handling, watchdogs, build tooling, and hardware-free unit tests complete; clean target Pi 5 build and installed-artifact tests pending).
- [ ] **Milestone 4**: Guarded bringup launch graph and SROS2 security enclaves.
- [ ] **Milestone 5**: Native host deployment under `/opt/ubuntu_tank` and systemd confinement.
- [ ] **Milestone 6**: Raised-track bench acceptance and latency validation.
