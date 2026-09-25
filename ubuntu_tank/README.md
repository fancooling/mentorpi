# MentorPi Native Tank Controller

Current checkout: container refactor C1/C2 is implemented. The runner and lifecycle
adapter now require Supervisor; native startup commands below describe the
previous delivery. Images and Pi deployment remain pending in C3/C4. See
[C2 supervision and local tests](docs/CONTAINER_SUPERVISION.md).

Status: Milestones 1–9 implemented and certified ACCEPTED on live target hardware (5-stage software delivery, live battery telemetry, owner-observed 4-direction motion, accepted physical stop latencies across all 9 failure modes, and STM32 command-loss watchdog verified on elevated tracks).
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

**Current Implementation Scope**: This delivery provides the complete hardware-free repository scaffold, native host preparation and ROS installation workflow, complete 446-package dependency closure, Lyrical source port, serial-bridge watchdog/error handling, guarded bringup pipeline (`ubuntu_tank_bringup`), safe teleoperation (`ubuntu_tank_teleop`), SROS2 access-control policies, native systemd packaging and transactional deployment (Milestone 5), bench orchestration with simulated stop-latency checks (Milestone 6), production loopback Fast DDS profile packaging with systemd unit start-limit correction (Milestone 7), monotonic first-command deadline and 5-stage delivery verification (Milestone 8), and Milestone 9 physical acceptance closure with operator observation verification and physical latency bounds. Actuator commands are strictly guarded by `ubuntu_tank_safety`, monitored by `ubuntu_tank_supervisor`, and commanded via renewable short leases with `ubuntu_tank_teleop`. Bench output distinguishes software delivery, host serial writes, and physical track movement. Physical acceptance transitions to `ACCEPTED` only upon verified live operator observations of all four directions, post-burst complete stops, and valid stop latency measurements; mock mode and unobserved live runs strictly remain `INCOMPLETE`. On-ground motion remains forbidden.

The [September 13 native-Pi diagnosis](debug/NO_MOTION_DIAGNOSIS_20260913.md)
identified blocked DDS discovery and demonstrated a loopback-unicast fix using a
mock board with production confinement intact. Milestone 7 productionizes this fix
with packaged loopback Fast DDS XML profile, systemd unit start-limit correction,
and transactional host environment migration. Milestone 8 added the first-command
deadline and 5-stage correlated delivery validation. Milestone 9 implements physical
acceptance closure and observation verification; live target execution on elevated
tracks has verified 5-stage software delivery and owner-confirmed 4-direction motion.
See [design Milestones 8–9](../docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md#milestone-8--bounded-arming-and-verified-delivery-acceptance)
for delivery acceptance and physical validation specifications. Increasing
bench duration does not repair a disconnected command path.

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
   Pure data validation of the recorded package entries, cryptographic SHA256 hashes, architectures, and closure state in `versions.lock`. The committed lock provides the complete 446-package transitive closure (`closure_status: complete`). The original 424-package ROS/controller closure was installed and built on the clean ARM64 target; the 22-package web addition was captured from official Resolute ARM64 artifacts, with target installation still pending.

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
├── docs/
│   ├── RELEASE_MANIFEST_SPEC.md     # Version-plus-revision release ID and manifest schema
│   └── DEPENDENCY_CLOSURE.md        # Direct dependency verification, AST import audit, and locked closure specification
├── config/
│   ├── controller.yaml               # Production kinematic & safety configuration defaults
│   └── sros2/
│       ├── README.md                 # SROS2 architecture specification
│       ├── governance.xml            # DDS Security governance policy (deny-by-default, encryption)
│       ├── policies.xml              # SROS2 enclave profiles for 5 roles
│       └── permissions/              # Per-enclave DDS permissions (controller, guard, bridge, operator, status)
├── host/
│   ├── 99-mentorpi-rrc.rules         # Restricted and verified udev serial symlink template
│   ├── mentorpi-tank.service         # Hardened native systemd unit template
│   ├── mentorpi-tank-operator.service # Shared operator authority service
│   ├── mentorpi-tank-stack.target     # One-command controller/operator lifecycle group
│   └── mentorpi-tank.env             # Non-secret runtime environment defaults
├── scripts/
│   ├── install_ros2.sh               # Ubuntu and ROS repository/package setup script (Milestone 2)
│   ├── build_workspace.sh            # rosdep and colcon build scaffold
│   ├── check_host.sh                 # Read-only OS, architecture, power, and device preflight (Milestone 2)
│   ├── recover_activation.sh         # Boot-time write-ahead transaction recovery runner
│   ├── verify_runtime.sh             # ROS graph, topic ownership, and zero-state verification
│   └── sros2_policy.py               # SROS2 security policy and enclave isolation verification
├── src/
│   ├── ros_robot_controller_msgs/    # Complete reused vendor ROS 2 interfaces
│   ├── ros_robot_controller/         # Reused vendor STM32 serial bridge with full dependencies
│   ├── controller/                   # Reused vendor tank kinematics and odometry publisher
│   ├── ubuntu_tank_bringup/          # Guarded bringup and safe teleoperation launch pipelines
│   ├── ubuntu_tank_safety/           # Disarmed-by-default motor guard package
│   ├── ubuntu_tank_supervisor/       # Trusted AND-gating systemd watchdog supervisor
│   └── ubuntu_tank_teleop/           # Safe keyboard teleoperation with renewable leases
└── tests/
    ├── test_source_boundary.sh       # Provenance, AST import, allowlist, and zero-perception gate
    ├── test_negative_boundary.sh     # Negative regression test proving rejection of undeclared dependencies
    ├── test_dependency_closure.sh    # Dependency closure, lock verification, and tamper detection gate
    ├── test_install_workflow.py      # Unit tests for host preflight, mutual exclusion, and lock verification
    ├── test_milestone3_port.py       # Unit tests for Milestone 3 porting, watchdogs, heartbeats, and build script
    └── test_milestone4_bringup.py    # Unit tests for Milestone 4 guarded bringup, teleop leases, fault injection, SROS2
```

---

### Source documentation

Each maintained code/configuration file explains its purpose and rationale in
top-of-file documentation. Files copied or refactored from Hiwonder MentorPi also
identify the original source path and meaningful local adaptations. Preserve
vendor copyright/license notices. Git records contents and revision history;
there is no source manifest or per-file source-hash maintenance step.

`VERSION` is plain release-version data, and each empty `src/*/resource/*` file
registers its package with the ament index; comments must not change these data
formats. The `controller` and `ros_robot_controller` resource markers were copied
from their corresponding `mentorpi/src/driver/<package>/resource/<package>` paths.
Markdown files document their purpose in their opening section. Generated build
output is not manually annotated. Dependency-download and generated release/build
checksums verify artifacts separately from source documentation.

## 4. Verification and Testing

Run the automated test suite without hardware:
```bash
./deploy.sh test
```

This verifies:
1. **Source Boundary Gate** (`tests/test_source_boundary.sh`):
   - Checks directory layout, declared imports/dependencies, and controller-only scope.
   - Runs without a source inventory or per-file source hashes; Git tracks revisions.
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
    - Complete isolated apt-archive enumeration, unreadable/duplicate artifact rejection, required architecture/repository fields, unlocked-transitive exposure, live candidate-override rejection, and package metadata synchronization (53 tests total).
9. **Milestone 4 Guarded Bringup and Safe Teleop Tests** (`tests/test_milestone4_bringup.py`):
   - Guarded bringup launch file wiring explicit topic pipeline (`/controller/cmd_vel` -> `/ubuntu_tank_safety/motor_input` -> `/ros_robot_controller/set_motor_guarded`).
   - `OnProcessExit` fail-closed shutdown handlers for `motor_guard`, `bridge`, and `controller`.
   - Complete removal of legacy non-motor command endpoints (`/app/cmd_vel`, `cmd_vel`, `set_pose`, `set_odom`, buzzer, oled, rgb, servos) in `controller_only` mode.
   - Transient-local guard state reporting on `/ubuntu_tank_safety/state` and `/ubuntu_tank_safety/armed`.
   - Repeated 4-motor zero command emission on `destroy_node`, explicit disarm, watchdog timeout, and invalid command inputs.
   - Safe keyboard teleoperation with renewable 150 ms leases and immediate space bar stop.
   - Fault injection regressions: wall-clock jump immunity, ROS simulated time pause immunity, and one-child-healthy/one-child-hung supervisor detection.
   - SROS2 access control policies enforcing deny-by-default, rejecting unauthorized motor command injection or non-operator arming (24 tests total).
10. **SROS2 Security Policy Verification** (`scripts/sros2_policy.py`):
    - Validates `governance.xml` enforcing participant authentication and metadata/data payload encryption.
    - Validates distinct least-privilege profiles and DDS permissions across all 5 enclaves (`controller`, `guard`, `bridge`, `operator`, `status`).

---

## 5. Implementation Status & Next Milestones

- [x] **Milestone 1**: Repository scaffold and provenance (Completed).
- [x] **Milestone 2**: Target-Pi Ubuntu and ROS installation workflow (Completed and verified natively on target Pi 5).
- [x] **Milestone 3**: Lyrical port, dependency closure, and serial bridge hardening (Completed and verified natively on target Pi 5).
- [x] **Milestone 4**: Guarded bringup launch graph, teleop leases, fault injection, and SROS2 security enclaves (Completed and verified).
- [ ] **Milestone 6**: Raised-track bench acceptance and latency validation (Software simulation and orchestrator tooling verified; physical target-Pi bench testing pending).

### Milestone 5 release installation and security updates

On the supported Ubuntu 26.04 ARM64 Pi 5, stop the managed service before
installing or activating a release. Both operations rerun strict host preflight
and hardware exclusion while holding the deployment lock. Busy or unsupported
hosts are rejected before provisioning or changing release assets. The CLI does
not permit disabling these live checks.

```bash
sudo ./deploy.sh stop  # for an existing deployment
sudo ./deploy.sh install dist/ubuntu-tank-RELEASE-arm64.tar.zst --operator-user ubuntu
sudo ./deploy.sh activate RELEASE
```

Replace `RELEASE` with the packaged release ID and `ubuntu` with the authorized
operator login. `--operator-user` defaults to the non-root sudo caller; direct
root invocations must specify it. Log out and back in after initial installation
to apply the `ubuntu-tank-operators` and `ubuntu-tank-status` group memberships.
The operator can then use the sourced release's arm/disarm, teleop and status
commands without root. Operator access does not confer access to `/dev/rrc`,
service participant keys, or private CA keys. A read-only observer may be added
to `ubuntu-tank-status` alone by the administrator.

Initial activation requires exactly one connected `1a86:55d4` adapter to bind
the udev template to its serial number, or its stable USB port if no usable serial
exists. Later activations retain this discriminator. With a port-bound identity,
keep the controller connected to that port; relocating it requires an explicit
administrator update of the recorded rule while stopped. Ambiguous selection,
wildcards and unresolved identity placeholders are rejected.

Security credentials live at `/etc/opt/ubuntu_tank/security/keystore`, a stable
symlink to a complete generation. Governance and permissions use S/MIME signed
by the permissions CA. Installation of another release preserves active grants;
activation validates and signs the selected release's policies using existing
CA/participant identities, then selects the generation while stopped. Legacy
PEM signatures are repaired on activation. Incomplete existing key/certificate
pairs fail closed and require restoration from a known backup.

Transaction snapshots include signed policies, identities, ownership and modes.
Rollback and interrupted-activation recovery restore that security state with
the matching release. Old credential generations remain on disk for recovery;
there is no automatic garbage collection. Snapshots made before security-state
support cannot safely restore a policy baseline and are rejected.

`./deploy.sh test` includes production OpenSSL signing, legacy repair and policy
rollback tests. Its DDS participant check runs with native ROS 2 available and
explicitly skips otherwise. It creates no motion endpoints and opens no serial
device. Run the suite with `/opt/ros/lyrical/setup.bash` sourced on the Pi to
exercise the actual pinned middleware before controller startup.

### First production package and rebuilds

After `install-ros`, `install-deps`, `build`, and `test` on the native Ubuntu
26.04 ARM64 host, authorize the isolated builder and package as your normal user:

```bash
sudo -v
./deploy.sh package
```

Packaging ignores the development `install/` tree. If `.work/native-rootfs` is absent,
the builder bootstraps it offline from the host's installed `/usr`, runtime
libraries and `/opt/ros`, after verifying installed versions against
`versions.lock`. It copies a small set of system configuration files and creates
fresh root-only account records; it does not copy home directories, robot
releases, host credentials or physical devices. Allow disk space for a second
copy of the system tools/ROS plus build output. Bootstrap never changes host apt
packages. Keep apt upgrades idle while building.

To prepare or inspect this step explicitly:

```bash
python3 scripts/prepare_build_root.py --dry-run
sudo python3 scripts/prepare_build_root.py
```

An existing root is preserved. To bootstrap again after changing the dependency
lock, select a fresh directory under `.work` with `--rootfs` when invoking
`scripts/build_disposable_root.sh`. The builder uses `systemd-nspawn` when
available, otherwise chroot with temporary proc mounted for the build; it builds
with the final `/opt/ubuntu_tank/releases/RELEASE/install` prefix. No synthetic
install option is needed for this native workflow. This bootstrap is not an
x86-to-ARM cross-build installer.

Each completed tree has `production-build.json` recording its actual prefix,
source identity and installed file hashes. Packaging rejects a supplied tree
with missing/mismatched provenance; stale automatic build candidates are rebuilt.
Installation checks the production prefix again. An identical release archive
may be installed again, but a different payload with the same ID is rejected.
Use a new release ID for changed software.

The operator agent writes ROS logs under `/var/opt/ubuntu_tank/operator-log`,
owned by `ubuntu-tank-operator:ubuntu-tank-operators`. Its launcher accepts
`UBUNTU_TANK_OPERATOR_LOG_DIR` to select another pre-provisioned writable log
directory; it does not reuse the controller-only `ROS_LOG_DIR`. Installation and
boot tmpfiles rules grant the operator group access to `/run/ubuntu_tank` and
pre-create `/run/ubuntu_tank/operator.lock` as
`ubuntu-tank-operator:ubuntu-tank-operators` mode `0660`.

Teleop, bench, and one-shot operator commands fail closed when the shared agent
is unavailable. Direct ROS control requires the explicit `--direct-ros` option
and holds `operator.lock` until its ROS publishers are destroyed, so it cannot
overlap an agent start or restart. Arming is not reported successful until a
fresh, complete four-motor zero `bridge_write` observation is received within
the original 250 ms transaction deadline.

Administrative lifecycle commands use `mentorpi-tank-stack.target` to start or
stop the controller and operator agent together while retaining their separate
users, device permissions, logs, and restart policies. Both services declare
`PartOf=mentorpi-tank-stack.target`, so target stop/restart operations propagate
to each service. The target is the boot and `deploy.sh` grouping surface; the
services remain independently addressable so the operator and future web status
path can remain available while the motion controller is intentionally stopped.

```bash
sudo systemctl enable mentorpi-tank-stack.target
sudo systemctl start mentorpi-tank-stack.target
sudo systemctl stop mentorpi-tank-stack.target
```

### Host speed and keyboard settings

`controller.max_linear_speed` and `controller.max_angular_speed` are applied at
the controller command boundary, including commands from callers other than the
keyboard. Excess velocity is clamped; non-finite velocity becomes a stop.
`deploy.sh teleop` reads `UBUNTU_TANK_CONFIG`, defaulting to
`/etc/opt/ubuntu_tank/controller.yaml`, and applies `teleop.linear_speed`,
`teleop.angular_speed` and `teleop.lease_duration_sec`. Keyboard speeds are capped
by the controller limits. IPC additionally negotiates speeds downward to the
agent caps before acquiring ownership; bench uses the same negotiation, so its
default 0.8 rad/s request becomes 0.5 rad/s with the default agent. Requested lower
speeds are never increased. `teleop --speed` and `--angular-speed` override host
keyboard speeds; the agent caps still apply. IPC keyboard input expires at the
shorter of the configured lease and 150 ms. A missing/invalid configuration
prevents teleop startup.
Restart the controller after changing its limits and restart teleop after changing
keyboard settings. All commands still pass through the motor guard's RPS bound.

Production roots/output use `.work/native-rootfs` and `.work/native-build`;
hardware-free fixtures use separate paths and are marked synthetic. Live
installation rejects synthetic output.

### Milestone 6 & 9 raised-track controller acceptance and physical closure

Under NO circumstances does Milestone 6 or 9 authorize on-ground motion. All bench tests
strictly require the tank chassis to be physically elevated so tracks rotate clear of
any surface.

```bash
# Software simulation and kinematic verification (hardware-free / regression):
./deploy.sh bench --ack-tracks-raised --mock

# Live target-Pi bench execution without physical certification (verifies software delivery only):
./deploy.sh bench --ack-tracks-raised

# Live target-Pi bench execution with interactive operator observation input (Milestone 9 physical closure):
./deploy.sh bench --ack-tracks-raised --interactive-observations

# Live target-Pi bench execution with pre-recorded physical observations file:
./deploy.sh bench --ack-tracks-raised --physical-observations /path/to/observations.json
```

Run bench as the operator with the managed controller service already active.
The client holds a read-only shared deployment lock through cleanup and verifies
that the serial bridge belongs to that service; unrelated device owners still
block acceptance. Any failed preflight or geometry check skips all actuation.
Each burst explicitly arms, verifies state, publishes bounded commands, correlates
5-stage delivery observations down to STM32 serial frame writes, and verifies
disarming before pausing. Command/state delivery alone does not certify physical
acceptance: `--interactive-observations` or `--physical-observations` validates
real track motion, direction match, post-burst complete stops, measured physical
stop latencies, and STM32 command-loss contingency. Absent valid physical observations,
the suite reports `SOFTWARE_DELIVERY_PASSED` with physical acceptance `INCOMPLETE`.


**Mandatory Safety Rules & Preflight Checklist**:
1. **Elevate Tracks**: Physically prop the tank chassis so both left and right tracks rotate freely without surface contact.
2. **Emergency Disconnect**: Ensure the physical battery power switch is within immediate reach.
3. **USB Identity**: Confirms STM32 RRC serial interface matching USB ID `1a86:55d4` at `/dev/rrc`.
4. **Battery Health**: Battery voltage must be >= 9.6V (3.2V/cell 3S LiPo cutoff) to prevent brownouts during motor spin. In live mode, fresh telemetry from `StatusClientNode` under the `/ubuntu_tank/status` SROS2 enclave is strictly required; missing or unauthenticated telemetry fails closed without falling back to synthetic voltage.
5. **Mutual Exclusion**: Verifies zero conflicting Docker containers (`MentorPi`, `MentorPiFan`, `runtime-core`) and exclusive deployment lock.

**Accepted Tank Geometry and Conservative Limits**:
- **Wheelbase**: 0.1368 m (tread ground contact length)
- **Track Width**: 0.1446 m (center-to-center track distance)
- **Sprocket Diameter**: 0.075 m (sprocket pitch diameter)
- **Correction Factors**: left = 1.0, right = 1.0 (calibrated neutral baseline)
- **Max Linear Speed**: 0.5 m/s (bench test nominal: 0.2 m/s)
- **Max Angular Speed**: 2.0 rad/s (bench test nominal: 0.8 rad/s)
- **Max Motor RPS**: 2.0 RPS (hard guard limit per motor)

**Calculated Kinematic Motor Polarity Table (physical direction unverified)**:
| Motion | Command | Motor RPS [M1 (FL), M2 (RL), M3 (FR), M4 (RR)] | Polarity Result |
|---|---|---|---|
| Forward | `linear.x > 0` | Left < 0 RPS, Right > 0 RPS | **PASS** |
| Reverse | `linear.x < 0` | Left > 0 RPS, Right < 0 RPS | **PASS** |
| Spin Left (CCW) | `angular.z > 0` | All 4 motors > 0 RPS | **PASS** |
| Spin Right (CW) | `angular.z < 0` | All 4 motors < 0 RPS | **PASS** |
| Stop | `linear.x = 0, angular.z = 0` | All 4 motors = 0.0 RPS | **PASS** |

**Stop Latency Validation across Failure Conditions (§10.3)**:
*(Software simulation verifies timing bounds; live physical measurements require target-Pi hardware execution and active controller observations)*
| Failure Condition | Simulated Latency | Accepted Bound | Simulation Status | Physical Status | Mechanism |
|---|---|---|---|---|---|
| `keyboard_lease_expiry` | ~155 ms | <= 200 ms | **PASS** | *Pending Target Pi* | TeleopLeaseManager 150 ms lease expiry -> zero velocity published |
| `terminal_loss` | ~160 ms | <= 200 ms | **PASS** | *Pending Target Pi* | Terminal input loss / disconnect -> key repeat ceases -> lease expiry (150 ms) -> zero velocity published |
| `teleop_crash` | ~260 ms | <= 300 ms | **PASS** | *Pending Target Pi* | Teleop process crash / stream silence -> guard freshness timeout (250 ms) -> 4-motor zero emitted |
| `guard_freshness_timeout` | ~260 ms | <= 300 ms | **PASS** | *Pending Target Pi* | MotorGuard 250 ms monotonic timeout -> 4-motor zero emitted |
| `guard_crash` | ~120 ms | <= 250 ms | **PASS** | *Pending Target Pi* | Guard crash -> supervisor detects dead child / heartbeat silence -> terminates bridge with safe zero fallback |
| `bridge_crash` | ~125 ms | <= 250 ms | **PASS** | *Pending Target Pi* | Bridge crash -> supervisor detects dead child / heartbeat silence -> terminates siblings |
| `supervisor_child_crash` *(alias)* | ~120 ms | <= 250 ms | **PASS** | *Pending Target Pi* | Supervisor process monitoring -> child exit -> sibling termination -> zero_motors(count=4) |
| `service_stop_sigterm` | < 1 ms | <= 100 ms | **PASS** | *Pending Target Pi* | Signal handler catches SIGTERM -> zero_motors(count=4) dispatched before exit |
| `serial_disconnect` / `serial_loss` | ~510 ms | <= 600 ms | **PASS** | *Pending Target Pi* | Silence watchdog (500 ms) trips -> fatal fault -> port closure & zero fallback |
| `host_shutdown` | < 1 ms | <= 100 ms | **PASS** | *Pending Target Pi* | Systemd host shutdown SIGTERM -> zero_motors(count=4) dispatched before poweroff |

**STM32 Command-Loss Characterization (§10.3)**:
- **Host Zero Delivery**: Host zero delivery timing (<= 300 ms) must be instrumented on target Pi; synthetic default timings are never substituted in live mode.
- **STM32 Firmware Watchdog**: Command-loss stopping timeout (<= 1000 ms) requires live bench verification on target hardware; safe stop observation alone does not certify timing claims without instrumented measurements.
- **Operator Emergency Disconnect**: Physical battery switch accessible within immediate reach during all bench operations.
- **On-Ground Authorization**: `FORBIDDEN`. Raised-track controller acceptance does not authorize on-ground use.

### Milestone 15 raised-track web acceptance

Milestone 15 validates the installed browser and PWA control path on the real Pi 5.
It does not authorize on-ground motion. Mechanically elevate the chassis, keep the
battery disconnect within immediate reach, start the managed controller and web
services, and run the command as the authorized operator:

```bash
./deploy.sh web-acceptance \
  --ack-tracks-raised \
  --require-target \
  --expected-release-id RELEASE_ID \
  --interactive-observations
```

Use the installed HTTPS page and installed Android/iOS PWAs while answering the
prompts. The workflow requires button and keyboard motion in all four directions,
every safety and PWA case including screen lock, and all browser, network, service,
guard, bridge, serial, and host-stop fault measurements. Record the physical
observer, client versions, instruments, network conditions, raw evidence references,
and the separate event-to-agent, zero-write, and physical-stop timing components.

The runner holds a shared deployment lock throughout the live session, permits only
the serial bridge owned by `mentorpi-tank.service`, verifies `/dev/rrc` USB identity
`1a86:55d4`, correlates the session with the installed release and web configuration,
and requests a fail-closed disarm before verifying the final live status. A deployment
attempt or unrelated serial owner makes preflight fail.

`--physical-observations FILE` is useful for validating an evidence document or for
`--mock` regression work, but static input alone never reports `ACCEPTED` on the Pi.
It remains `PENDING_PHYSICAL_ACCEPTANCE`. When executing the physical `host_shutdown`
measurement, the runner offers to save a campaign checkpoint before the Pi powers down.
After the Pi reboots, resume and complete the certified run with
`--resume-campaign dist/web-acceptance-campaign.json`; the runner verifies release and
web-configuration identity and confirms host reboot via kernel boot ID. The JSON and Markdown
reports are written under `dist/` by default. The physical Pi 5, connected robot, motors, installed
clients, fault injections, and measurement instruments are required for final acceptance.

### Shared protocol package (container refactor C1)

Web and runtime now consume `ubuntu_tank_protocol` directly. It contains the
ROS-free message contracts, web configuration validation, operator IPC client,
lifecycle wire limits, and API generator. See
[dependency and generation instructions](docs/WEB_DEPENDENCY_CLOSURE.md#3-protocol--api-compatibility-specification).
C2 replaces native lifecycle operations and runner supervision. See
[C2 runtime supervision](docs/CONTAINER_SUPERVISION.md); Docker delivery follows
in C3–C6.
