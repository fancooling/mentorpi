# MentorPi Native Tank Controller Design

Status: Milestones 1–5 implemented; native no-motion blocker diagnosed with mock hardware. Milestone 6 physical acceptance remains incomplete; corrective Milestones 7–9 are planned.

Date: 2026-09-13

Target: Hiwonder MentorPi Tank, Raspberry Pi 5 ARM64, STM32 RRC controller

Host: Clean Ubuntu 26.04 LTS installation with network access and sudo

Runtime: Native ROS 2 Lyrical; Docker is not used

## 1. Purpose

This document defines a fresh, controller-only installation for a MentorPi Tank.
Starting from an already-installed Ubuntu 26.04 system, the operator will install
ROS 2 Lyrical, prepare the Ubuntu host, deploy the required ROS packages, and run
the tank controller directly on the Raspberry Pi 5.

The first delivery supports only these movements:

- drive forward;
- drive backward;
- turn left; and
- turn right.

The design deliberately reuses the existing MentorPi controller code instead of
rewriting the STM32 protocol, motor mapping, or tank kinematics. All authored
source, configuration defaults, host-installation templates, tests, deployment
automation, and operator instructions will live below one repository directory:
`ubuntu_tank/`. Production installation is a separate, versioned, root-owned
release under `/opt/ubuntu_tank`; the robot will not run from a developer checkout
or a user's home directory.

### 1.1 Target-Pi diagnosis and revised acceptance status (2026-09-13)

The [native-Pi diagnosis](../ubuntu_tank/debug/NO_MOTION_DIAGNOSIS_20260913.md)
records tests against installed release `1.0.0-g8c67ddd`. Production discovery
attempted multicast sends to `239.255.0.1:7400` that failed with `EPERM` under
`IPAddressDeny=any` / `IPAddressAllow=localhost`. The operator could arm and send
velocity to the controller while the guard and bridge received no motor commands.
Increasing burst duration does not fix that delivery failure.

Explicit loopback unicast discovery restored delivery while retaining the IP
filter, SROS2 Enforce, service identity, and filesystem/device confinement:

| Native diagnostic run | Controller receipts | Guard receipts | Bridge receipts |
| --- | ---: | ---: | ---: |
| Production discovery, one 0.5 s burst | 14 | 0 | 0 |
| Loopback unicast, one 0.5 s burst | 14 | 14 | 19 |
| Loopback unicast, four 2 s bursts | 176 | 176 | 196 |

The working four-direction run observed 40 nonzero commands per direction at
the mock SDK, with signs `--++`, `++--`, `++++`, and `----`, plus stop calls.
These are native callback/mock SDK results. Real serial opening was prohibited;
no physical actuation occurred. The profile is a demonstrated candidate, not
an installed production correction or a uniquely minimal configuration.

Earlier Milestone 6 claims of physical movement, confirmed motor polarity,
measured host zero delivery, and characterized STM32 command-loss behavior are
withdrawn. Publication and arm/disarm state do not establish downstream receipt,
successful serial writes, or movement. Milestones 7–9 below implement the
correction and close those evidence gaps; Milestone 6 stays open until its
physical exit criteria actually pass.

## 2. Assumptions and scope

### 2.1 Starting assumptions

- Ubuntu 26.04 LTS ARM64 is already installed and booting on the Raspberry Pi 5.
- The operator has a normal non-root account, sudo access, and Internet access.
- The Pi boot EEPROM is new enough for Ubuntu 26.04. The deployment preflight
  will report the EEPROM version and stop with a link to Ubuntu's update
  procedure if it does not meet Ubuntu's published minimum.
- The STM32 RRC controller is connected by USB serial and is expected to appear
  as the verified `/dev/rrc` symlink at 1,000,000 baud.
- Power wiring, board revision, polarity, and supply limits have been verified
  against the exact hardware documentation before the motors are energized.
- Initial motion testing is performed with the tracks raised clear of the work
  surface and with an immediately accessible power disconnect.
- The workspace and its resulting installation are for the owner's personal use
  on this robot. The owner controls the Ubuntu image and every installed package,
  service, user account, and workload. Publishing or distributing the workspace
  or release artifacts is outside this design's scope.
- Owner-approved local processes are trusted. A malicious or compromised process
  running as the controller service UID is outside the threat model. Heartbeat
  identity checks are intended to catch accidental cross-talk and configuration
  errors, not to enforce isolation from hostile same-UID software.

This design does not reinstall Ubuntu, change cloud-init, or prescribe unverified
power-supply values. It begins after the clean operating system is installed.

### 2.2 Included

- Ubuntu package, locale, ROS repository, and ROS 2 Lyrical setup;
- USB serial access for the STM32 controller;
- MentorPi ROS message and service interfaces;
- the vendor serial protocol implementation;
- tank `Twist`-to-motor kinematics;
- disarmed-by-default motor command guarding;
- bounded command-line motion tests;
- safe keyboard teleoperation;
- a native systemd service; and
- build, test, deployment, status, arm, disarm, and rollback instructions.

### 2.3 Excluded from this phase

- camera, LiDAR, SLAM, Nav2, localization, and obstacle avoidance;
- AI, speech, vision, web dashboard, and rosbridge;
- joystick support;
- servo positioning and calibration applications;
- accurate odometry or TF publication; and
- Docker or compatibility with the factory container runtime.

The reused controller currently publishes command-integrated `odom_raw`. That
output is not wheel-encoder odometry and is not an acceptance criterion for this
controller-only phase.

### 2.4 Mutually exclusive target modes

This native Ubuntu controller is a separate target mode from the vendor
Raspberry Pi OS plus factory `MentorPi` container. It is installed only on a
clean Ubuntu 26.04 system; it must never be installed beside, started beside, or
given devices concurrently with the factory stack or either Docker workflow.
Before physical testing, preserve a verified restorable copy of the vendor image
on separate media. Application `rollback` changes `/opt/ubuntu_tank` releases;
it does not restore the vendor operating system. Platform rollback requires the
robot to be powered down and the verified vendor image/media restored before the
factory stack again owns hardware.

## 3. Safety invariants

The controller is an actuator system. The following are design requirements,
not optional enhancements:

1. The STM32 bridge must never receive normal motor commands except through the
   motor guard.
2. The motor guard starts disarmed after every launch, crash, service restart,
   reboot, or explicit disarm.
3. Arming is an explicit operator action and is never persisted.
4. Only a complete command containing unique motor IDs 1 through 4, finite RPS
   values, and values within the configured limit may reach the bridge.
5. A command older than 250 ms causes the guard to disarm and repeatedly publish
   four zero motor values. Arming must also start a monotonic first-command
   deadline of at most 250 ms; receiving no valid command must not leave the
   guard armed indefinitely (implemented in Milestone 8).
6. Invalid input, teleop loss, process exit, SIGINT, SIGTERM, serial loss, or
   launch failure must result in repeated zero commands where communication is
   still possible and must shut down the controller graph.
7. Keyboard motion is lease-to-run. Each recognized key event authorizes a
   short motion interval, and failure to receive a fresh event produces zero.
8. Motion-test commands are finite and self-terminating. The instructions must
   not use an unbounded nonzero `ros2 topic pub --rate` command.
9. No on-ground testing is allowed until raised-track tests include measured
   stop latency and a separately proven STM32 behavior after host or serial loss.
10. Production DDS discovery is localhost-only, security enforcement is
    fail-closed, and only protected service/operator enclaves may arm or publish
    accepted motion commands.
11. Command freshness uses monotonic time at both the guard and serial-bridge
    boundaries. Loss of either process heartbeat makes systemd stop the complete
    graph; no wall-clock adjustment can extend a motion lease.

ROS topic remapping and publisher-count checks are not access-control boundaries.
Tests must inspect the running graph, fail if any node other than the motor guard
publishes the actuator-facing topic, and separately prove the enforced SROS2
permissions with unauthorized participants.

## 4. Source-reuse plan

For this native controller, `mentorpi/src/` is the primary code reference and
reuse source. Implementation starts there, copies or adapts the smallest
controller-relevant packages, and does not require a routine comparison with the
factory image before work can proceed.

Use `/mnt/rpi-rootfs` only when the repository cannot answer a necessary
question, specifically when:

- a required file, dependency, launch contract, or configuration value is
  missing or incomplete in `mentorpi/src/`;
- a hardware-specific runtime fact such as the active serial identity or board
  configuration cannot be established from repository code plus direct
  `udevadm` inspection on the clean Ubuntu target; or
- observed behavior contradicts the repository reference and the active factory
  copy is needed to diagnose the difference.

A fallback inspection must document the exact `/mnt/rpi-rootfs` path, why the
fallback was necessary, and the resulting decision in the affected file's
top-level documentation. If no file is adopted, record the decision in the
relevant design section. No source-file hash or separate provenance registry is
required. Files from the mounted image never silently replace the repository
baseline; any adopted difference is an explicit adaptation recorded in Git.

| Delivered component | Existing source | Reuse decision | Required adaptation |
| --- | --- | --- | --- |
| `ros_robot_controller_msgs` | `mentorpi/src/driver/ros_robot_controller_msgs/` | Copy the complete package without reducing its message/service set | Retain all generated interfaces required by the bridge |
| `ros_robot_controller` | `mentorpi/src/driver/ros_robot_controller/` | Copy the complete package, including `ros_robot_controller_sdk.py` | Add controller-only mode that exposes no non-motor command APIs; parameterize machine type, serial device, and baud rate; complete dependency metadata; implement signal-safe zero and serial close |
| `controller` | `mentorpi/src/driver/controller/` | Reuse the complete Python module directory and required package scaffolding, including `ackermann.py` | Do not install legacy launch/config surfaces that require Nav2 or peripherals; export only the controller executable needed here; remove environment/path assumptions; parameterize geometry and correction values; complete dependency metadata |
| `ubuntu_tank_safety` | New safety package; no guard implementation exists in the current repository | New code only because the reused vendor stack has no adequate command watchdog or arming boundary | Implement the narrowly specified validation, timeout, repeated-zero, state reporting, and access-control behavior in this design, with complete tests |
| `ubuntu_tank_supervisor` | New minimal process supervisor | New code only because systemd cannot AND-gate independent child health by itself | Launch the graph, receive separate non-ROS monotonic guard/bridge heartbeats, notify systemd only while both are fresh, and fail closed |
| `ubuntu_tank_teleop` | `mentorpi/src/peripherals/peripherals/teleop_key_control.py` | Reuse the keyboard mapping, terminal polling, and ROS message logic in a controller-only package | Replace latched commands with a short renewable motion lease, publish periodic fresh commands and zero on timeout/signals, and remove unused servo behavior |
| `ubuntu_tank_bringup` | New integration package | New code only where no reusable controller-only package exists | Own launch, configuration, graph shutdown policy, and operator-facing services |
| `deploy.sh` | New native-host workflow | Reuse only applicable shell hardening and validation patterns from current repository scripts | Implement idempotent Ubuntu/ROS installation, build, test, systemd installation, and safe operations without Docker |

The standalone `mentorpi/src/driver/sdk` package is not required: the STM32
`Board` implementation used by the bridge is already contained in
`ros_robot_controller`. The full `peripherals` package is also excluded because
most of it belongs to cameras, LiDAR, IMU visualization, or joystick support.
Only the relevant keyboard-control logic is carried forward.

## 5. Source, build, and production filesystem layout

The repository workspace and production installation have different purposes.
`ubuntu_tank/` is the self-contained source and build root. A packaged release
is installed below `/opt/ubuntu_tank` and activated through a stable `current`
symlink. Nothing in the running controller may depend on the repository location,
the build tree, `mentorpi/`, `docker/`, the factory image, or
`/home/ubuntu/software`.

### 5.1 Repository and build workspace

```text
ubuntu_tank/
├── README.md                         # Canonical operator guide
├── deploy.sh                         # Single deployment and operations entrypoint
├── VERSION                           # Release version input
├── versions.lock                     # Pinned ROS/Ubuntu dependency inputs and hashes
├── config/
│   ├── controller.yaml               # Version-controlled production defaults
│   └── sros2/                        # Deny-by-default governance/policy templates
├── host/
│   ├── 99-mentorpi-rrc.rules         # Restricted and verified serial symlink
│   ├── mentorpi-tank.service         # Native systemd unit
│   └── mentorpi-tank.env             # Non-secret runtime environment
├── scripts/
│   ├── install_ros2.sh               # Ubuntu and ROS repository/package setup
│   ├── build_workspace.sh            # rosdep and colcon build
│   ├── check_host.sh                 # Read-only OS, architecture, power, and device checks
│   ├── recover_activation.sh         # Boot-time write-ahead transaction recovery
│   └── verify_runtime.sh             # ROS graph, topic ownership, and zero-state checks
├── src/
│   ├── ros_robot_controller_msgs/    # Complete reused vendor package
│   ├── ros_robot_controller/         # Complete reused package plus narrow porting patch
│   ├── controller/                   # Reused Python controller core; legacy launch excluded
│   ├── ubuntu_tank_safety/            # New guarded actuator boundary
│   ├── ubuntu_tank_supervisor/        # New AND-gated process/heartbeat supervisor
│   ├── ubuntu_tank_teleop/           # Safe adaptation of vendor keyboard teleop
│   └── ubuntu_tank_bringup/          # Native controller launch and configuration
├── tests/
│   ├── test_source_boundary.sh
│   ├── test_dependency_closure.sh
│   ├── test_launch_graph.sh
│   └── test_shutdown_behavior.sh
├── dist/                              # Generated release artifacts; never committed
├── .work/                             # Disposable packaging root; never committed
└── build/ install/ log/               # Generated by colcon; never committed
```

The checkout may be placed anywhere the deployment operator can write. Examples
use `/path/to/mentorpi/ubuntu_tank` deliberately: the checkout location is not a
runtime interface. Colcon `build/`, `install/`, and `log/` directories are local
build products, not the production installation.

### 5.1.1 File-level source documentation

Design decision (2026-09-13): deprecate `source-manifest.txt` and all requirements
to generate, maintain, validate, count, or package its entries. Git is the source
of truth for file contents, revisions, and change history. Do not replace the
manifest with another inventory, per-file SHA-256 fields, or a mandatory header
schema enforced by a new provenance gate.

Every maintained source, test, script, and configuration file must explain its
purpose and non-obvious rationale in documentation at the top of the file. A
file primarily copied or refactored from vendor code must also identify:

- the vendor and original repository path (or upstream URL when appropriate);
- whether it is directly copied or substantially adapted/refactored; and
- the meaningful local adaptations and why the code is retained here.

Use ordinary prose in the language's existing module documentation or comment
syntax. For Python, use the module docstring after any shebang/encoding lines;
for shell, CMake, ROS interfaces, and configuration, use leading comments; for
XML, put comments after the XML declaration. Preserve original copyright and
license notices. Do not insert comments into formats that prohibit them or
non-code package markers; explain those in the owning package's README instead.
Newly authored files need a purpose/rationale, not a fabricated vendor origin.
An existing adequate header need not be duplicated. Routine edits do not require
repeating Git commit IDs or updating hashes in file headers.

Example for a vendor-derived Python module:

```python
"""Translate guarded motor commands into the MentorPi STM32 serial protocol.

Adapted from Hiwonder MentorPi:
mentorpi/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py.
Retained to preserve the board's packet format and motor polarity. Local changes
add bounded serial I/O and repeated stop attempts on shutdown.
"""
```

### 5.1.2 Manifest retirement implementation checklist

Implemented on 2026-09-13. The source manifest and its workflow dependencies
have been removed; the following migration steps are complete:

- [x] Move useful origin, adaptation, and rationale descriptions into file-level
  documentation, then delete `ubuntu_tank/source-manifest.txt`.
- [x] Remove manifest parsing, coverage counts, source hashes, byte-for-byte
  vendor equality checks, manifest-based fallback checks, and the required-file
  assertion from `tests/test_source_boundary.sh`. Retain directory-layout,
  Python import/dependency, controller allowlist, and perception-exclusion checks.
- [x] Remove manifest hash rewriting from `tests/test_negative_boundary.sh`;
  keep regression tests for omitted package dependencies.
- [x] Remove the manifest from release packaging in
  `scripts/deployment_manager.py`, generated release layouts, and any fixtures
  or packaging assertions that require it. Newly built releases must install
  without a source manifest; old archives may contain it as unused metadata.
- [x] Update `deploy.sh` test labels, `ubuntu_tank/README.md`, and active
  contributor instructions in `AGENTS.md`, `GEMINI.md`,
  `.agents/rules/git_versioning.md`, and `.agents/skills/ack-review/SKILL.md`.
  Remove hash-refresh and manifest-status-based formatting rules; retain
  meaningful vendor notices and avoid unrelated vendor-code reformatting.
- [x] Verify the boundary and negative tests and release packaging without the
  manifest. Confirm no executable workflow requires or regenerates it.

This retires source-provenance bookkeeping. Download integrity checks in
`versions.lock` and generated build/release artifact checksums serve separate
purposes and do not depend on `source-manifest.txt`; they are not a replacement
source registry. Historical acceptance records may mention the retired gate but
must not be interpreted as current requirements.

### 5.2 Target Pi production installation

```text
/opt/ros/lyrical/                         # ROS installation managed by apt
/opt/ubuntu_tank/
├── libexec/
│   └── recover-activation              # Release-independent boot recovery runner
├── current -> releases/<release-id>      # Atomically selected active release
└── releases/
    └── <release-id>/                     # Immutable after successful install
        ├── bin/
        │   └── mentorpi-tank-run         # Non-interactive ROS launch wrapper
        ├── install/                      # Colcon install tree used at runtime
        ├── src/                          # Deployed source and provenance copy
        ├── config/
        │   └── controller.yaml           # Release configuration default/schema
        ├── host/
        │   ├── 99-mentorpi-rrc.rules     # Matching udev asset
        │   ├── mentorpi-tank.service     # Matching systemd asset
        │   ├── mentorpi-tank-recover.service
        │   └── mentorpi-tank.env         # Matching environment default
        ├── deploy.sh                     # Matching recovery/inspection entrypoint
        ├── README.md                     # Matching operator instructions
        └── release-manifest.txt          # Release contents, ABI, prefix, and checksums
/etc/opt/ubuntu_tank/
├── controller.yaml                       # Host-specific controller configuration
├── mentorpi-tank.env                     # Non-secret service environment
└── sros2/                                # Host-generated keys and policies
/var/opt/ubuntu_tank/
├── ros-log/                              # ROS file logs
└── deployment/
    ├── activation-journal                # Current/previous release and transaction state
    └── snapshots/<transaction-id>/       # Root-only config and host-asset snapshots
/run/ubuntu_tank/                         # Volatile service runtime state
/run/lock/ubuntu_tank/deploy.lock         # Root-owned deployment/recovery lock
/etc/systemd/system/mentorpi-tank.service
/etc/systemd/system/mentorpi-tank-recover.service
/etc/tmpfiles.d/ubuntu-tank.conf
/etc/udev/rules.d/99-mentorpi-rrc.rules
```

This follows the conventional separation for add-on software: static package
content under `/opt`, host-specific configuration under `/etc/opt`, persistent
mutable data under `/var/opt`, and ephemeral process state under `/run`. ROS 2
Lyrical itself remains apt-managed under `/opt/ros/lyrical`.

The production service invokes `/opt/ubuntu_tank/current/bin/mentorpi-tank-run`
and runs only installed artifacts from `/opt/ubuntu_tank/current/install`; it
must not import Python modules or launch files from the deployed `src/`
provenance copy. Build intermediates and colcon logs are not included in a
release. Service output goes to journald, while any ROS file logs are explicitly
directed to `/var/opt/ubuntu_tank/ros-log`.

### 5.3 Ownership, activation, and rollback

- Each release directory is installed by root and becomes non-writable to the
  service account before it can be activated.
- The service runs as a dedicated non-root `ubuntu-tank` account with only the
  dedicated device-group access required for `/dev/rrc`; it does not run as root
  or as the deployment operator.
- `current` is changed with an atomic symlink replacement only after release
  structure, ownership, manifest checksums, ROS dependencies, and configuration
  have passed validation.
- Installation and activation are distinct. Installing a release does not start
  the service, select that release, or arm the motors.
- Activation records the previous release and transitions to the new release in
  a stopped, disarmed state. Matching systemd and udev assets are staged with
  the release, backed up, and applied only as part of activation. Rollback
  restores those host assets and atomically restores the previous symlink, then
  also remains stopped and disarmed until an explicit `start`.
- A release ID is unique and path-safe, derived from the project version plus a
  source revision. Existing release content is never overwritten with different
  bytes.
- A colcon install tree is not assumed to be relocatable. Packaging builds and
  tests it with its final absolute prefix,
  `/opt/ubuntu_tank/releases/<release-id>/install`, inside a clean ARM64 Ubuntu
  26.04 disposable build root. That root is materialized below the repository's
  ignored `ubuntu_tank/.work/rootfs/` directory and entered with a documented
  `systemd-nspawn` workflow; its internal `/opt` is therefore not the host's
  `/opt`. Packaging may use `sudo` only to create and enter this isolated root;
  compilation and tests run there as an unprivileged build account. It never
  mounts host `/opt`, `/etc`, `/var/opt`, `/run`, or devices. Source is mounted
  read-only inside it, and only `dist/` is copied out. The installer extracts the
  artifact at the same absolute prefix seen inside the build root and rejects a
  host whose architecture or ROS ABI does not match the artifact. Tests reject
  setup hooks, metadata, or shebangs that retain checkout, `.work`, or temporary
  paths.
- Configuration is preserved across releases and is never silently overwritten.
  Before activation, root records the current configuration, environment,
  security policy, systemd unit, and udev rule in a checksummed transaction
  snapshot under `/var/opt/ubuntu_tank/deployment/snapshots`. A changed schema
  requires an explicit, validated forward migration and downgrade path. The
  activation journal's commit marker is written only after the new release
  passes all non-starting validation; failure or rollback restores the prior
  compatible snapshot. Live health is checked later by explicit `start`.

Activation is a crash-consistent transaction, not an assumption that several
`/etc` writes are atomic together. Its required order is:

1. acquire the release-independent `/run/lock/ubuntu_tank/deploy.lock`, then
   validate the candidate, migration/downgrade paths, and available rollback;
2. snapshot the current symlink target and host state; `fsync` every snapshot
   file and directory; then write and `fsync` a `PREPARED` transaction record
   and its containing directory;
3. disarm, send repeated zero, stop the service, and confirm it is inactive;
4. stage and `fsync` candidate host files on their destination filesystems, then
   replace each file and reload udev/systemd;
5. atomically replace and `fsync` the `current` symlink, run non-starting release
   structure/import/config-policy validation, and leave the service stopped; and
6. write and `fsync` the committed journal state, then release the lock.

The root-owned `mentorpi-tank-recover.service` runs the release-independent
`/opt/ubuntu_tank/libexec/recover-activation` before the controller at boot. It
uses the same deployment lock. If it finds `PREPARED` or `ACTIVATING` rather
than a committed transaction, it restores the previous symlink and checksummed
host snapshot, reloads host assets, and leaves the controller stopped and
disarmed until recovery validates. Installation tests interrupt activation after
every durable boundary and prove this recovery path does not depend on
`current` or any versioned release executable.

## 6. Ubuntu 26.04 and ROS 2 Lyrical setup

The eventual `deploy.sh` host-preparation commands will perform the following
documented operations idempotently. `README.md` will also show them for manual
recovery.

### 6.1 Host preflight

The read-only preflight must verify:

```bash
test "$(dpkg --print-architecture)" = "arm64"
. /etc/os-release
test "${VERSION_ID}" = "26.04"
uname -m                            # expected: aarch64
rpi-eeprom-update                  # report current bootloader version
```

It must also report free disk space, time synchronization, the current kernel,
and whether another process already owns `/dev/rrc`. It must detect Docker when
present and reject factory `MentorPi`, `MentorPiFan`, replacement containers,
factory boot units, or any conflicting ROS/hardware-owner process. The same
mutual-exclusion check runs immediately before install, activation, start, and
arm so a stale earlier preflight cannot authorize a changed host. It must not
alter the host.

### 6.2 Base Ubuntu environment

The installed Ubuntu image is an explicit external baseline, not a bit-for-bit
artifact produced by this project. `./deploy.sh prepare-host` updates it from its
configured official Ubuntu repositories, installs the base prerequisites, and
records before/after apt sources, package manifests, image metadata, kernel, and
timestamp. This step is intentionally a security-current baseline operation; it
may produce newer Ubuntu package versions on a later date and is not described
as reproducible. It starts no robot service.

Follow the ROS 2 Lyrical Ubuntu instructions rather than assuming the image
already knows about the ROS repositories:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y locales software-properties-common curl
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
sudo add-apt-repository universe
```

If this baseline operation creates `/run/reboot-required`, the workflow stops
and requires a reboot plus a fresh `check-host` before continuing. After the
baseline is accepted, the ROS/application dependency lock and release manifest
record exactly which host baseline they were validated against.

Install the `ros2-apt-source` package for Ubuntu Resolute using the version and
SHA-256 recorded in `versions.lock`, following the procedure published by ROS.
`install-ros` parses the lock as data; it does not execute it as shell code:

```bash
./deploy.sh verify-lock
curl --fail --location --output /tmp/ros2-apt-source.deb "${PINNED_ROS_APT_SOURCE_URL}"
printf '%s  %s\n' "${PINNED_ROS_APT_SOURCE_SHA256}" /tmp/ros2-apt-source.deb | sha256sum --check --strict
sudo dpkg -i /tmp/ros2-apt-source.deb
sudo apt update
```

The displayed variables are values printed by `verify-lock` for manual recovery;
they are not fetched from a `latest` endpoint. The script aborts on a missing
lock value, failed download, checksum mismatch, or different Ubuntu codename.
Refreshing the lock is a separate reviewed operation, never a side effect of
installation.

### 6.3 ROS and controller dependencies

`./deploy.sh install-ros` installs the supported ROS base and development
tooling only after `versions.lock` records `closure_status: complete`; no second
undocumented install command exists. The committed lock remains `direct-only`,
so live installation currently fails before mutation pending clean-target
closure capture and review.

The completed lock must cover the full apt dependency closure (package name,
version, architecture, repository identity, and downloaded artifact hash), not
only the direct packages. The current direct-only lock records the rosdep
rules/index revision that will be used to derive that closure. It includes
`ros-lyrical-ros-base`, `ros-dev-tools`,
Python serial/YAML dependencies, SROS2 policy/keystore tooling, EEPROM inspection
tooling, `zstd`, and the selected `systemd-nspawn`/Ubuntu-rootfs tooling used by
packaging. Installation fails rather than silently selecting a different
version. If the official repositories no longer retain the lock, a deliberate
lock refresh and full revalidation are required; the design does not claim an
unprovided immutable Ubuntu/ROS archive.

Do not install NumPy, pygame, OpenCV, Nav2, or perception packages unless a later
milestone introduces a demonstrated controller dependency.

Initialize rosdep once, then resolve dependencies from the copied package
manifests and cross-check every apt result against `versions.lock`:

```bash
sudo rosdep init                 # skip only when already initialized
source /opt/ros/lyrical/setup.bash
cd /path/to/mentorpi/ubuntu_tank
./deploy.sh install-deps
rosdep check --from-paths src --ignore-src --rosdistro lyrical
```

Every retained ROS package must declare accurate build, execution, and test
dependencies before rosdep is treated as a reliable installation mechanism.
`install-deps` uses rosdep for resolution and a downloaded rosdep index matching
the locked revision and hash. It does not invoke apt installation: every
resolved package must already have been installed at its exact locked version
by the verified `install-ros` transaction. It fails on an unresolved key,
dependency absent from the lock, missing package, or version mismatch.
After any base upgrade, `install-ros` checks `/run/reboot-required` and stops
before ROS installation, build, service installation, or activation until the
operator reboots and reruns `check-host`.

### 6.4 Persistent runtime environment

Interactive shells may source ROS for convenience, but production must not rely
on `.bashrc`. A non-interactive launch wrapper will source, in order:

1. `/opt/ros/lyrical/setup.bash`;
2. `/opt/ubuntu_tank/current/install/setup.bash`.

The installed systemd unit will use
`EnvironmentFile=/etc/opt/ubuntu_tank/mentorpi-tank.env`, pass
`/etc/opt/ubuntu_tank/controller.yaml` to bringup, and run as the dedicated
`ubuntu-tank` service account. The environment file will contain only non-secret
settings such as `ROS_DOMAIN_ID=0` and
`ROS_LOG_DIR=/var/opt/ubuntu_tank/ros-log`. Production also sets
`ROS_LOCALHOST_ONLY=1`, `ROS_SECURITY_ENABLE=true`, and
`ROS_SECURITY_STRATEGY=Enforce`; the matching SROS2 keystore and permissions live
under `/etc/opt/ubuntu_tank/security/keystore`. Controller, guard, bridge, teleop/operator,
and read-only status processes use distinct enclaves and credentials; there is
no shared "service" credential. The controller may subscribe only to the accepted
`Twist` input and publish only to the guard input. The guard alone may subscribe
to that input, expose arm/disarm and state, and publish guarded motor output. The
bridge may subscribe only to guarded motor output and may not publish or call
arming/motion interfaces. Teleop/operator credentials, readable only by members
of a dedicated `ubuntu-tank-operators` group, may call arm/disarm and publish to
the accepted `Twist` input but not the guard input or output. Status credentials
are read-only. All unlisted actions are denied.
After porting, `MACHINE_TYPE` will be a ROS parameter and is not required as an
environment variable. The installer creates the persistent
`/var/opt/ubuntu_tank` hierarchy, while systemd creates `/run/ubuntu_tank` for
each service run; both have narrowly scoped ownership. Service stdout and stderr
go to journald.

Localhost-only discovery prevents LAN participants from joining this
controller-only graph; it does not authorize local users. SROS2 permissions and
filesystem protection of enclave keys provide the local authorization boundary.
Installation and acceptance tests must prove that an uncredentialed local ROS
process cannot discover protected interfaces, arm, disarm, publish accepted
motion, or publish directly to the actuator-facing topic.

### 6.4.1 Shared Fast DDS loopback discovery contract (planned)

Package `ubuntu_tank/config/fastdds/loopback.xml` as a root-owned, read-only
release asset. Start from the exact successful XML in the diagnosis: disable
built-in transports, select UDPv4 with interface whitelist `127.0.0.1`, and
explicitly supply loopback default user-data unicast, metatraffic unicast, and
initial-peer locators. An interface whitelist alone failed in earlier tests.
Keep the tested settings together until native regression evidence justifies
any simplification.

Before creating any ROS context, every service and CLI entrypoint must apply:

```text
RMW_IMPLEMENTATION=rmw_fastrtps_cpp
ROS_DOMAIN_ID=0
ROS_LOCALHOST_ONLY=1
ROS_AUTOMATIC_DISCOVERY_RANGE=SYSTEM_DEFAULT
FASTDDS_DEFAULT_PROFILES_FILE=/opt/ubuntu_tank/releases/<release-id>/config/fastdds/loopback.xml
ROS_SECURITY_ENABLE=true
ROS_SECURITY_STRATEGY=Enforce
```

`SYSTEM_DEFAULT` lets the XML control discovery rather than allowing ROS discovery
options to modify it; see the upstream
[rmw_fastrtps discovery configuration](https://github.com/ros2/rmw_fastrtps#change-participant-discovery-options).
The diagnosis tested domain 0. Other domains require explicit validation and are
not covered by that result. SROS2 identities and key permissions remain separate
for controller, guard, bridge, operator, and status.

Resolve the active release once under the deployment lock and derive the absolute
profile path from that release. Service launch, bench, teleop, arm/disarm, status,
and runtime verification must use the same resolver before ROS initialization,
including distinct bench preflight/operator contexts. Do not rely on interactive
shell exports, a checkout path, or a profile beneath `/home` or `/tmp`. Reject
missing, malformed, unreadable, or incompatible configuration before starting
participants; do not fall back to default discovery. Validate or reject inherited
transport/discovery overrides that conflict with the managed configuration.

Activation must migrate existing host environment files, not just seed defaults
on first install. Preserve unrelated owner configuration and calibration, include
managed discovery settings in transaction snapshots, and restore matching profile,
environment, unit, and security state on rollback or interrupted activation.
The old release's known delivery defect must remain documented; restoring its
files does not make it an accepted motion baseline. Configuration selection must
never implicitly start or arm the controller.

### 6.5 systemd confinement and supervision

The production unit is a reliability and motion-safety boundary on a trusted,
single-owner host. It is not a security boundary against malicious software
running as the controller service UID. Its reviewed baseline will include:

- empty capability and ambient-capability sets, `NoNewPrivileges=yes`,
  `RestrictSUIDSGID=yes`, and a non-root service identity;
- `ProtectSystem=strict`, `ProtectHome=yes`, private temporary storage, protected
  kernel tunables/modules/control groups, and write access only to the exact
  `/var/opt/ubuntu_tank` and `/run/ubuntu_tank` paths;
- a closed device policy permitting read/write only to the verified RRC character
  device, without general `/dev` access;
- only the Unix and localhost IP address families required by the selected DDS,
  with non-loopback networking denied in addition to `ROS_LOCALHOST_ONLY=1`;
- a bounded stop timeout, escalation that cannot leave a stuck process alive,
  restart-rate limits, and restart-on-failure only into a disarmed state;
- `Type=notify` with `NotifyAccess=main`, where only the main
  `ubuntu_tank_supervisor` can notify systemd after AND-gating separate guard and
  bridge health heartbeats; and
- conservative resource limits validated not to starve the watchdog or serial
  zero path.

Retain `IPAddressDeny=any` and `IPAddressAllow=localhost`; removing the filter or
allowing multicast is not the production remedy. Move `StartLimitIntervalSec=30s`
and `StartLimitBurst=5` from `[Service]` to `[Unit]`, then verify the effective
restart limit on the target. These corrections are pending Milestone 7.

Exact directives and limits are implementation outputs because they must be
tested with Lyrical's DDS and `/dev/rrc`. Automated gates use
`systemd-analyze security`, inspect the effective unit properties, attempt
forbidden filesystem/device/network access, and exercise stop timeout, forced
kill, heartbeat loss, and restart throttling.

## 7. STM32 serial setup

Repository sources already provide the serial contract, so no mounted-image
lookup is needed for the initial design:

- `mentorpi/src/driver/ros_robot_controller/scripts/99-ttyACM0.rules` supplies
  the `ttyACM0` convention plus vendor ID `1a86`, product ID `55d4`, and the
  ModemManager-ignore intent, though its rules are permissive and separate;
- `mentorpi/src/peripherals/scripts/99-ttyACM0.rules` supplies the broader
  `ttyACM*` match and `/dev/rrc` symlink intent, though it is also permissive and
  contains a malformed trailing property value; and
- `mentorpi/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py`
  confirms `/dev/rrc` and 1,000,000 baud as the bridge defaults.

The native rule deliberately combines the verified predicates, corrects the
property syntax, adds a persistent identity discriminator, and replaces the
permissive group/mode with dedicated least-privilege ownership:

```udev
SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", ATTRS{serial}=="<verified-serial>", GROUP="mentorpi-rrc", MODE="0660", SYMLINK+="rrc", ENV{ID_MM_PORT_IGNORE}="1"
```

Before installing the rule, `check-host` must use `udevadm info` to confirm the
actual device attributes and substitute a persistent serial number. If the board
does not expose one, the reviewed rule must use a stable physical-path
discriminator. If neither discriminator is available or more than one matching
device exists, deployment stops; VID/PID uniqueness observed once is not enough.
Consult `/mnt/rpi-rootfs` only if the repository rule and direct target-device
inspection leave a necessary factory naming or identity detail unresolved, and
record that fallback and its rationale in the rule's leading comments.

Host installation will:

1. create the dedicated `mentorpi-rrc` group and non-login `ubuntu-tank` service
   account, adding that account only to the device group;
2. install the reviewed rule from the repository's `ubuntu_tank/host/`;
3. reload and trigger udev;
4. avoid adding human operators to `mentorpi-rrc`; any exceptional direct serial
   diagnostic requires an explicit temporary administrative procedure while the
   service is stopped;
5. verify that `/dev/rrc` resolves to exactly one character device; and
6. verify read/write access without changing the device to world-writable mode.

The ROS bridge will receive `serial_device:=/dev/rrc` and
`baud_rate:=1000000` as parameters. Missing, ambiguous, or inaccessible serial
hardware is a launch failure and must leave the guard disarmed.

## 8. Minimal code adaptations

### 8.1 `ros_robot_controller_msgs`

Copy the whole package. Its existing `CMakeLists.txt` generates the complete set
of messages and services imported by `ros_robot_controller_node.py`; pruning the
package would create import or build failures.

### 8.2 `ros_robot_controller`

Preserve the existing binary framing, CRC8 implementation, 1 Mbaud default,
motor IDs, and STM32 command encoding. Limit changes to portability and safety:

- declare and consume `machine_type`, `serial_device`, `baud_rate`,
  `servo_config_file`, and `load_servo_offsets` parameters;
- construct `Board` from the declared serial parameters;
- remove the hard dependency on `os.environ['MACHINE_TYPE']`;
- add a default-on `controller_only` mode that does not create LED, buzzer,
  OLED, RGB, PWM-servo, bus-servo, gamepad, or SBUS command endpoints;
- skip servo-offset loading in controller-only mode;
- publish repeated four-motor zero commands on orderly shutdown;
- close the serial port in a `finally` path;
- fail clearly on serial-open and serial-write errors; and
- declare all imported ROS and Python dependencies in `package.xml`.

The bridge's private `set_motor` subscription is remapped to the guarded output.
It must not subscribe directly to user or teleop motor commands.

### 8.3 `controller`

Preserve the existing `Twist` handling, tank motor polarity, message construction,
and `MecanumChassis` conversion. Limit changes to:

- consume `machine_type` from a ROS parameter;
- parameterize wheelbase, track width, and wheel diameter;
- add a controller-only mode that creates exactly one `Twist` subscription,
  `/controller/cmd_vel`, and no `/app/cmd_vel`, `/cmd_vel`, servo, pose-reset, or
  other legacy command surfaces;
- start from the values instantiated by the current vendor node: `0.1368 m`,
  `0.1446 m`, and `0.075 m`, while requiring raised-track validation;
- replace `/home/ubuntu/software/chassis_adjustment/robot_correction_factors.yaml`
  with declared left/right correction parameters whose version-controlled
  defaults ship in the package and whose host-specific values are loaded from
  `/etc/opt/ubuntu_tank/controller.yaml`;
- apply or remove every exposed correction factor so no configuration value is
  silently ineffective;
- document that `odom_raw` is command integration, not measured odometry; and
- declare all imported dependencies in `package.xml`.

Reuse the complete Python module directory because `odom_publisher_node.py`
imports other modules from it even when only Tank mode is selected. Do not carry
the vendor `controller.launch.py` or unrelated launch/config files into the
installed runtime: they import `nav2_common`, `robot_localization`, and the
excluded `peripherals` package. The adapted `setup.py` exports only
`odom_publisher`; servo-oriented `init_pose` is not an installed executable in
this phase.

### 8.4 Motor guard, authorization, and topic ownership

Implement `ubuntu_tank_safety` because the current repository has no reusable
guard that meets these requirements. Keep this new package narrow: it owns only
arming state, motor-message validation, freshness timeout, repeated zeroing,
read-only state reporting, and safe shutdown. All lease ages and watchdog
deadlines use a steady monotonic clock, never ROS time or wall time. The launch
graph will remap topics so the command path is explicit:

```text
safe teleop or bounded CLI test
          |
          v  geometry_msgs/msg/Twist
 /controller/cmd_vel
          |
          v
 controller/odom_publisher
          |
          v  ros_robot_controller_msgs/msg/MotorsState
 /ubuntu_tank_safety/motor_input
          |
          v
 ubuntu_tank_safety/motor_guard   [disarmed by default; 250 ms timeout]
          |
          v
 /ros_robot_controller/set_motor_guarded
          |
          v
 ros_robot_controller bridge -> /dev/rrc -> STM32 -> motors
```

The bringup launch must shut down the complete graph if the guard or bridge exits.
The bridge also enforces an independent monotonic 250 ms freshness deadline on
guarded motor messages from a dedicated watchdog thread; expiry sends repeated
four-motor zero commands and closes the command path. Guard and bridge emit
independent non-ROS heartbeats over separate inherited file descriptors or
credential-checked Unix sockets. Under the single-owner trust model, a runtime
PID file plus `SO_PASSCRED` UID/PID comparison is sufficient to catch accidental
or misconfigured senders; it is not expected to resist a malicious same-UID
process that can replace the PID file. Inherited pipes remain an optional
defense-in-depth mechanism. The main `ubuntu_tank_supervisor` maintains a
monotonic deadline for each and notifies systemd only while both are fresh; one
healthy child cannot mask the other's hang. Only this main process receives
systemd's notify socket. A missed deadline stops the graph. The guard's maximum
RPS will initially be conservative and raised only after bench evidence. Arming
clears any cached command, so the operator must provide a new command after
arming. Milestone 8 starts the freshness deadline at the arm transition, expires it
within the configured guard timeout (0.250 s), clears cached commands on every re-arm,
and requires a new explicit arm after expiry. A late command cannot resurrect a lease.
CLI discovery/readiness waits must occur before arming so they do not consume
the first-command window. Tests must include no first command, a command arriving
at/after the deadline, and repeated arm requests that must not silently renew an
active motion lease.

The guard will publish its armed/disarmed state as a transient-local
`std_msgs/msg/Bool` topic so `deploy.sh status` can query state without mutating
it.

These host layers mitigate a hung guard or ROS executor, but cannot prove a stop
if the bridge process, kernel, USB stack, or host hangs after the STM32 accepts a
nonzero command. The STM32's independent stale-command behavior remains a
physical acceptance gate, and on-ground operation is forbidden until it is
measured and accepted.

### 8.5 Keyboard teleop

Reuse the W/A/S/D mapping, terminal polling, and Twist publisher from the vendor
keyboard node, but do not preserve its latched linear command behavior. A raw
terminal reports characters and timeouts, not reliable key-up or focus-change
events, so the adapted node uses a short renewable command lease rather than
claiming direct release detection. It must:

- give every recognized key event a lease shorter than the 250 ms motor timeout;
- publish at a fixed rate while the lease is valid so the guard sees fresh input;
- publish zero as soon as the lease expires, including when terminal input or
  focus disappears;
- publish zero on exception, SIGINT, and SIGTERM;
- restore terminal state in `finally`; and
- contain no servo, camera, joystick, pygame, or OpenCV dependency.

Holding a key relies on terminal key-repeat behavior and may pause before repeat
begins; that is safe because an expired lease stops the tank. A future true
hold-to-run implementation would require key-up events from an input API such as
evdev and is outside this phase.

## 9. Build and deployment interface

`ubuntu_tank/deploy.sh` will be the only supported automation entrypoint. It
must be safe to rerun and must show help without modifying the host. Commands
that write system paths use `sudo` only for their exact destinations; build and
test commands run as the invoking user. `package` is the sole exception: it may
use `sudo` to manage its isolated `systemd-nspawn` root below `.work/`, while the
build itself remains unprivileged and never writes the host's system paths.

| Command | Contract |
| --- | --- |
| `./deploy.sh help` | List commands, prerequisites, effects, and safety requirements |
| `./deploy.sh check-host` | Read-only Ubuntu, ARM64, EEPROM, disk, ROS, serial, user, and process checks |
| `./deploy.sh verify-lock` | Read-only validation of pinned repository/package identities, versions, architectures, URLs, and hashes |
| `./deploy.sh prepare-host` | Update and record the explicit Ubuntu baseline, install base prerequisites, and stop if reboot is required |
| `./deploy.sh install-ros` | Verify the accepted host baseline, configure the pinned official ROS repository package, and install locked Lyrical/build dependencies |
| `./deploy.sh install-deps` | Resolve rosdep keys, require every apt result in `versions.lock`, install exact locked versions, and verify closure |
| `./deploy.sh build` | Source Lyrical and run a clean, reproducible colcon build under `ubuntu_tank/` |
| `./deploy.sh test` | Run unit, launch, package-import, topic-ownership, and signal-shutdown tests without motor hardware |
| `./deploy.sh package` | Create an ignored disposable ARM64 root below `ubuntu_tank/.work/`, build there for the final release prefix, reject leaked build paths, and emit a checksummed architecture-specific artifact in `ubuntu_tank/dist/` |
| `./deploy.sh install <artifact>` | Validate and install a new immutable release, stage its host assets, and create missing service prerequisites without changing active host assets or `current`, starting the service, or arming |
| `./deploy.sh activate <release-id>` | Acquire the deployment lock, stop/disarm, execute the write-ahead activation transaction, validate without starting, and leave the service stopped |
| `./deploy.sh rollback` | Restore the recorded release and host snapshot transactionally, validate without starting, and leave the service stopped |
| `./deploy.sh start` | Start the native systemd service in the disarmed state |
| `./deploy.sh stop` | Disarm, request repeated zero, and stop the service |
| `./deploy.sh status` | Show service, serial, ROS graph, guard state, and actuator-topic ownership |
| `./deploy.sh logs` | Show bounded recent journal output |
| `./deploy.sh arm --ack-tracks-raised` | Arm only after current raised-track acknowledgment and successful preflight |
| `./deploy.sh disarm` | Disarm and publish repeated zero immediately |
| `./deploy.sh bench --ack-tracks-raised` | Run finite low-speed forward, reverse, left, right, and stop tests |

Every mutating host or release command takes the root-owned
`/run/lock/ubuntu_tank/deploy.lock` before preflight and holds it through journal
commit or rollback. Its directory is created independently of the controller
unit and is never removed by service stop. A second deployment, activation,
rollback, start/stop, arm/disarm, or bench operation fails closed rather than
racing the first.

Installation and activation require the managed controller to be stopped before
their strict preflight. For upgrades, run `sudo ./deploy.sh stop` first; this
allows the same exclusion gate to reject every live serial owner.

The normal operator sequence after implementation will be:

```bash
cd /path/to/mentorpi/ubuntu_tank
./deploy.sh check-host
./deploy.sh prepare-host
# Reboot and rerun check-host here if prepare-host requires it.
./deploy.sh verify-lock
./deploy.sh install-ros
./deploy.sh install-deps
./deploy.sh build
./deploy.sh test
sudo -v  # The isolated production builder requires root.
./deploy.sh package
sudo ./deploy.sh install dist/ubuntu-tank-1.0.0-gabcdef-arm64.tar.zst --operator-user ubuntu
sudo ./deploy.sh activate 1.0.0-gabcdef
./deploy.sh start
./deploy.sh status
```

`activate` requires a stopped controller at preflight; `rollback` stops it
before restoring assets. Both operations leave the service stopped. The explicit `start` command performs live startup and
health checks and leaves the guard disarmed. Physical testing is a separate,
explicit operation after the non-motion gates pass.

Packaging records the release ID, project version, source revision, source and
install-tree checksums, build platform, ROS distribution, and test result. The
manifest also records the absolute install prefix used at build time. The
installer rejects malformed paths, checksum mismatches, an incompatible host or
ROS ABI, a prefix mismatch, or an existing release ID whose contents differ. It
copies configuration defaults only when no host configuration exists; upgrades
never silently replace accepted tank calibration. At least the active and
previous verified releases remain installed so rollback does not depend on the
repository checkout or network access.

## 10. Verification and acceptance gates

### 10.1 Hardware-free gates

- File-level documentation explains purpose and rationale; vendor-derived files
  identify the original source and meaningful adaptations. Review this as normal
  documentation, without a source inventory, hash gate, or parity requirement.
- `test_source_boundary.sh` checks layout, declared dependencies, imports, and
  controller-only scope without requiring a source manifest. Any mounted-image
  fallback is explained in the affected file or relevant design section.
- Repository source and generated build state remain below `ubuntu_tank/` until
  the explicit install step writes only the documented `/opt/ubuntu_tank`,
  `/etc/opt/ubuntu_tank`, `/var/opt/ubuntu_tank`, `/run/ubuntu_tank`, systemd,
  and udev targets.
- `rosdep install` resolves successfully on Ubuntu 26.04 ARM64.
- All packages build from a clean `build/`, `install/`, and `log/` state.
- Every console script imports and `--help`/startup behavior is tested on Lyrical.
- A packaged release installs with matching checksums, contains no `build/` or
  `log/` tree, and cannot be modified by the service account.
- Installed setup hooks and metadata contain the final release prefix rather than
  a checkout, temporary staging, or packaging path.
- `current` resolves to exactly one complete release, and the service sources
  only `/opt/ros/lyrical` plus that release's `install/` tree.
- Runtime tests prove the service writes nothing under `/opt`; host configuration
  remains under `/etc/opt`, persistent mutable data under `/var/opt`, temporary
  state under `/run`, and logs in journald.
- Activation and rollback atomically select the expected release, journal each
  transaction, restore schema-compatible checksummed configuration/host-asset
  snapshots, and return the service to a disarmed state.
- Motor guard tests cover disarmed startup, four-motor validation, NaN/Inf,
  duplicate/missing IDs, speed limits, timeout, re-arm, and repeated zero.
- Fault-injection tests cover wall-clock jumps, ROS-time pause, executor
  starvation, a hung guard, a missed bridge heartbeat, and a forced service kill;
  measured results distinguish host mitigations from the still-unproven STM32
  stale-command behavior.
- Launch tests prove that only the guard owns the bridge-facing motor topic and
  that the bridge exposes no non-motor command endpoints in controller-only mode.
- Launch tests prove that the controller exposes only `/controller/cmd_vel` as a
  motion input and no legacy `/app/cmd_vel`, `/cmd_vel`, servo, or pose-reset
  command surfaces.
- DDS security tests prove that an uncredentialed local participant and a
  credentialed read-only status participant cannot arm or command motion.
- Guard-state tests verify transient-local armed/disarmed reporting without
  calling the mutating arm service.
- SIGINT, SIGTERM, node crash, and service-stop tests exercise the zero path.

### 10.2 Target-Pi gates without motor power

- Ubuntu/ARM64/EEPROM preflight passes.
- Installed release files are root-owned and not writable by `ubuntu-tank`;
  service identity, supplementary groups, and systemd paths match the design.
- `/dev/rrc` is unique, stable across reconnect/reboot, and accessible only to
  the intended group.
- Serial open, close, reconnect failure, and service restart are observable and
  leave the controller disarmed.
- A logic analyzer or equivalent observation confirms the expected serial
  framing before motor power is enabled, where practical.

### 10.3 Raised-track motion gates

- Physical acknowledgment is current and the emergency power disconnect is in
  reach.
- Low-speed forward, reverse, left, and right motion matches the command signs.
- Each test is bounded and ends with repeated zero commands.
- Keyboard lease expiry, terminal loss, teleop crash, guard crash, bridge crash,
  service stop, serial disconnect, and host shutdown behavior are exercised.
- Stop latency is measured and recorded for every failure case.
- Motor RPS and geometry limits are accepted only after observing the exact tank.

No on-ground motion is authorized by completion of this design or by a successful
software-only test. It requires a separate decision after the STM32 stale-command
behavior and raised-track stop latency are proven.

### 10.4 Delivery evidence and honest acceptance reports (planned)

Separate evidence into publication, controller receipt, guard acceptance, bridge
receipt, successful SDK serial write, and observed physical movement. A matched
subscriber, armed state, healthy heartbeat, calculated RPS, or mock `Board` call
cannot stand in for a later stage. Successful host writes still do not prove
STM32 receipt, firmware response, wheel speed, or track motion.

Milestone 8 provides bounded, read-only delivery observations through protected
interfaces and narrowly scoped SROS2 grants. Each observation identifies the
process run, a monotonic sequence/time, command motor IDs/values, and the relevant
stage; bridge evidence must distinguish attempted writes from complete successful
writes and include errors/short writes. Reset or baseline evidence for each burst
and correlate the expected direction/nonzero values and terminating zeros within
the burst window. Old transient-local samples, prior runs, unrelated traffic,
mock calls, and a restarted process must not satisfy a new live test.

The bench must prepare discovery while disarmed, enforce bounded receipt waits,
stop further bursts on failed delivery, and attempt repeated zeros and disarm in
cleanup. Missing cleanup confirmation is itself a reported failure. Diagnostics
must not add an actuator bypass or delay the independent watchdog/zero path.
Reports must label simulated, native fake/PTY, live host-write, and owner-observed
physical evidence separately, with missing measurements marked pending or failed.
Keep the overall physical acceptance failed/incomplete until all mandatory
physical gates pass; never print a physical polarity/motion PASS from kinematics.
Record release/configuration identity and measurement method with each result.

## 11. Trackable implementation milestones

Checkboxes are updated only when their exit criteria and evidence are recorded.

### Milestone 1 — Repository scaffold and provenance

- [x] Create the proposed `ubuntu_tank/` directory structure.
- [x] Copy the complete messages and bridge packages plus the required complete
  controller Python module directory into `ubuntu_tank/src/`.
- [x] Use `mentorpi/src/` as the default source baseline and document every
  copied path and adaptation.
- [x] Define the narrow `/mnt/rpi-rootfs` fallback criteria; document the path,
  reason, and decision whenever a fallback is actually required.
- [x] Create the narrowly scoped `ubuntu_tank_safety` package and its tests,
  documenting why new code is required.
- [x] Create the minimal `ubuntu_tank_supervisor` and document why one trusted
  AND-gating systemd notifier is required.
- [x] Create `ubuntu_tank_teleop` from the relevant vendor keyboard node.
- [x] Record source revisions, file hashes, and local adaptations (historical
  manifest implementation; superseded by the retirement checklist in §5.1.2).
- [x] Define the version-plus-revision release ID and release-manifest schema.
- [x] Add ignores for colcon-generated `build/`, `install/`, `log/`, `dist/`, and
  disposable `.work/` output.

Exit criterion: maintained files have purpose/rationale documentation and
vendor-derived files identify their source and adaptations; the source boundary
test passes, direct package dependencies and Python imports are verified against
the controller-only allowlist, and no camera/LiDAR/AI code is in the direct dependency
declarations or AST imports (recursive transitive resolution and locked hashes
reserved for Milestones 2 and 3).
Status: Completed on 2026-09-08 (Remediated). Evidence: `tests/test_source_boundary.sh`
passed with 5-stage verification (92 payload files with provenance out of 93 Git-tracked
files with `source-manifest.txt` intentionally self-excluding, validated against commit
`ca32e0c` git object store, 0 fallbacks to `/mnt/rpi-rootfs`, complete tracked directory layout,
100% direct AST import coverage in package manifests including cross-workspace packages,
automated negative regression testing via `tests/test_negative_boundary.sh`, direct
controller-only allowlist enforcement, draft `versions.lock` scope specification for
Milestone 2, and zero perception/AI message types or tokens); unit and integration tests for
`ubuntu_tank_safety` (11/11 passed), `ubuntu_tank_supervisor` (22/22 passed, including
integration tests for credential PID mismatch rejection, missing-PID rejection, and
inherited-pipe operation), and `ubuntu_tank_teleop` (8/8 passed) all passed via
`./deploy.sh test`.



### Milestone 2 — Target-Pi Ubuntu and ROS installation workflow

Milestone 2 does not require ROS 2 or Pi-specific drivers to be installed
directly on a general-purpose development workstation. `check-host`,
`prepare-host`, and `install-ros` are target-host operations and must fail closed
unless they are running on the supported clean Ubuntu 26.04 ARM64 Raspberry Pi.
The development workstation may be used to author the workflow and run static,
mocked, and hardware-free tests without ROS. Actual apt/ROS installation,
EEPROM and device integration, and the milestone exit test must occur on a clean
target Pi. Later ROS compilation and integration tests require a compatible
Ubuntu 26.04 ARM64 ROS environment, which may be the target Pi or a separate
compatible ARM64 build host using the design's isolated disposable build root;
they do not require modifying the development workstation's base operating
system.

Any Python-only development dependency installed directly on the general-purpose
development workstation must use the single repository-level `.venv`, with its
purpose and version recorded by the project. Do not create component-level
environments, use Conda as a second environment, or install non-standard Python
packages into the global interpreter. This workstation policy does not move
target or build-root Python dependencies out of their locked apt/ROS installation
path. A Python environment is not an installation mechanism for ROS, kernel or
USB drivers, udev rules, or apt-managed native libraries. Those target
dependencies belong on the clean Pi or compatible isolated build root and must
be covered by `versions.lock` where required; Milestone 2 acceptance still
requires the clean target Pi. The target Pi does not use `.venv`, `virtualenv`,
or Conda: its Python and non-Python runtime dependencies are installed directly
through the locked apt/ROS workflow, and no Python virtual-environment activation
step is part of service startup or operator setup. The production wrapper still
sources the required ROS setup scripts as described in Section 6.4.

- [x] Implement idempotent `check-host`, `prepare-host`, `verify-lock`, `install-ros`, and `install-deps` commands.
- [x] Validate Ubuntu 26.04, ARM64, EEPROM, locale, disk, and time preconditions.
- [x] Configure the official `ros2-apt-source` package for Resolute.
- [x] Reject factory/sidecar/replacement containers, factory boot units, and conflicting ROS/device owners before install, activation, start, or arm.
- [x] Create and verify `versions.lock` for the complete Ubuntu/ROS/tooling dependency closure; never resolve a `latest` release during installation.
- [x] Install ROS 2 Lyrical ros-base, SROS2, EEPROM, packaging, archive, and only other demonstrated dependencies at locked versions.
- [x] Stop for a required reboot after base upgrades and re-run host preflight.
- [x] Add clear recovery behavior for partial apt or network failures.
- [x] Test the instructions from a clean Ubuntu 26.04 Raspberry Pi image.

Exit criterion: a clean Pi can install ROS and pass `check-host` by following only
`ubuntu_tank/README.md` and `deploy.sh`.
Status: Completed. Idempotent commands (`check-host`, `prepare-host`, `verify-lock`, `verify-closure`, `install-ros`, `install-deps`) are implemented and verified on clean ARM64 target (`tankubuntu`). The full 424-package transitive dependency closure (168 MB) was captured via the clean ARM64 APT solver and locked in `ubuntu_tank/versions.lock` (`closure_status: complete`) with exact versions, architectures, repositories, and SHA-256 hashes. Live `prepare-host`, `install-ros`, and `install-deps` ran to completion with `--no-download` from verified caches. `check-host` passed on the clean target with 0 errors and 0 warnings.

### Milestone 3 — Lyrical port and dependency closure

- [x] Complete `package.xml` metadata for every retained package.
- [x] Remove `MACHINE_TYPE` environment dependencies from both controller nodes.
- [x] Parameterize serial device, baud, geometry, and correction settings.
- [x] Remove hardcoded `/home/ubuntu/software` paths.
- [x] Make bridge serial shutdown and zeroing signal-safe.
- [x] Add an independent monotonic bridge freshness watchdog and guard/bridge
  health heartbeats for service supervision.
- [x] Make the supervisor track both child deadlines independently and ensure
  only it can send systemd watchdog notifications.
- [x] Build from a clean workspace with rosdep on Ubuntu 26.04 ARM64.
- [x] Test every installed console-script import on ROS 2 Lyrical.

Exit criterion: clean rosdep, colcon build, package import, and installed-launch
parse tests pass without legacy environment variables or paths.
Status: Completed. All 6 packages have complete `package.xml` and `setup.py` metadata, synchronized dependencies, maintainers (`dev@mentorpi.local`), versions (1.0.0), and Apache-2.0 licenses. Legacy `MACHINE_TYPE` and hardcoded `/home/ubuntu/software` paths are completely removed from sources. Native colcon build (`./deploy.sh build --clean`) succeeded on `tankubuntu` for all 6 packages (`controller`, `ros_robot_controller`, `ros_robot_controller_msgs`, `ubuntu_tank_safety`, `ubuntu_tank_supervisor`, `ubuntu_tank_teleop`). ROS 2 Jazzy/Lyrical compatibility issue with removed `geometry_msgs.msg.Pose2D` was resolved with guarded imports. All 5 console-script entry point imports were tested and verified against authentic ROS 2 Lyrical runtime libraries on `tankubuntu`. Installed launch file parsing (`ros2 launch ros_robot_controller ros_robot_controller.launch.py --print`) passed cleanly. Native `./deploy.sh test` ran on `tankubuntu` with all 53 unit/integration tests and provenance gates passing. STM32 RRC serial interface `/dev/rrc` udev symlink rule was installed and verified on `tankubuntu`, and live STM32 telemetry stream (53 Hz IMU, 1 Hz battery at 12.23V) was confirmed.

### Milestone 4 — Guarded bringup and safe teleop

- [x] Create `ubuntu_tank_bringup` with the guarded topic graph.
- [x] Configure disarmed startup, 250 ms freshness, and conservative RPS limits.
- [x] Shut down the graph when the guard or hardware bridge exits.
- [x] Disable every non-motor bridge command endpoint in controller-only mode.
- [x] Remove the controller's legacy `/app/cmd_vel`, `/cmd_vel`, servo, and
  pose-reset command surfaces in controller-only mode.
- [x] Add a read-only transient-local guard-state topic for status reporting.
- [x] Configure localhost-only DDS and distinct deny-by-default SROS2 enclaves
  for controller, guard, bridge, operator controls, and read-only status.
- [x] Implement renewable keyboard leases and periodic fresh commands.
- [x] Add invalid-command, timeout, signal, crash, and topic-ownership tests.
- [x] Add wall-clock-jump, ROS-time-pause, executor-starvation, hung-process, and
  one-child-healthy/one-child-hung heartbeat fault-injection tests.
- [x] Test that uncredentialed local/LAN participants and each credentialed
  non-owner enclave cannot arm, bypass the guard, or command forbidden topics.
- [x] Prove that no normal launch path bypasses the guard.

Exit criterion: all hardware-free safety regressions pass and every tested exit
path publishes repeated four-motor zero commands.
Status: Completed for hardware-free development and integration testing. Package `ubuntu_tank_bringup` created with guarded topic pipeline (`/controller/cmd_vel` -> `controller/odom_publisher` -> `/ubuntu_tank_safety/motor_input` -> `ubuntu_tank_safety/motor_guard` -> `/ros_robot_controller/set_motor_guarded` -> `ros_robot_controller` -> `/dev/rrc`) and fail-closed `OnProcessExit` shutdown handlers. Motor guard starts disarmed by default with 250 ms freshness timeout and <= 2.0 RPS limits. Controller-only mode verified to strip all non-motor endpoints in both controller and bridge nodes. Transient-local guard state reporting added to `/ubuntu_tank_safety/state` and `/ubuntu_tank_safety/armed`. SROS2 deny-by-default governance and permissions implemented for 5 distinct enclaves (`/ubuntu_tank/controller`, `/ubuntu_tank/guard`, `/ubuntu_tank/bridge`, `/ubuntu_tank/operator`, `/ubuntu_tank/status`), including required middleware discovery topic (`ros_discovery_info`) and node infrastructure endpoints, verified by `sros2_policy.py` and official OMG XSD schemas. Safe keyboard teleoperation implemented with renewable 150 ms leases and fail-closed zeroing. Hardware-free integration test suite (`test_milestone4_bringup.py` and `test_rmw_integration.py`) exercises authentic OpenSSL signed keystore verification, foreign CA participant rejection, tampered signature rejection, virtual PTY serial bridge communication with STM32 framing/telemetry, active real-time scheduling / clock pauses (> 250 ms), and supervisor child process faults. Physical target-Pi acceptance with real motors and tracks raised remains scheduled for Milestone 6.

### Milestone 5 — Native host deployment and operations

- [x] Implement the hardened udev rule with a dedicated `mentorpi-rrc` group and
  a persistent serial-number or physical-path identity discriminator.
- [x] Create the dedicated non-login `ubuntu-tank` service account with narrowly
  scoped serial and filesystem access.
- [x] Implement the native systemd service using
  `/opt/ubuntu_tank/current/install`, `/etc/opt/ubuntu_tank`,
  `/var/opt/ubuntu_tank`, and `/run/ubuntu_tank`.
- [x] Apply and test capability, filesystem, device, network, privilege,
  resource, watchdog, stop-timeout, restart, and restart-rate confinement.
- [x] Implement checksummed packaging and immutable installation into
  `/opt/ubuntu_tank/releases/<release-id>`.
- [x] Implement the disposable ARM64 build root below `ubuntu_tank/.work/`, use
  the final internal install prefix, and reject leaked checkout/staging paths.
- [x] Implement build, test, package, install, activate, rollback, start, stop,
  status, logs, arm, disarm, and finite bench commands.
- [x] Serialize every mutating operation with the release-independent root-owned
  `/run/lock/ubuntu_tank/deploy.lock`.
- [x] Ensure install never activates, starts, or arms the controller and never
  silently overwrites host configuration.
- [x] Make `current` activation atomic and validate root ownership and release
  integrity before switching it.
- [x] Retain and record the previous verified release for offline rollback.
- [x] Journal activation and keep root-only checksummed snapshots of compatible
  configuration, environment, security policy, systemd, and udev state.
- [x] Implement write-ahead transaction ordering, fsync points, boot-time
  interrupted-activation recovery, and failure rollback.
- [x] Install the recovery runner at the release-independent
  `/opt/ubuntu_tank/libexec/recover-activation` path and fault-inject interruption
  after every durable transaction boundary.
- [x] Implement and test explicit configuration-schema forward migrations and
  downgrade restoration.
- [x] Test upgrades and rollback of releases, configuration, udev, and systemd
  assets.
- [x] Test reboot, service restart, failed launch, and clean shutdown behavior.

Exit criterion: deployment is repeatable, the service always returns disarmed,
production runs only the selected immutable install tree, and rollback restores
the last known working release and host configuration without a checkout or
network connection.
Status: Completed for hardware-free and native deployment automation. Checksummed RFC 822 packaging and immutable installation implemented under `/opt/ubuntu_tank/releases/<release-id>`. Atomic 6-step activation transaction implemented with write-ahead journal (`PREPARED` -> `ACTIVATING` -> `COMMITTED`), root-only checksummed snapshots, fsync durability points, and automatic rollback on failure. Release-independent boot recovery runner implemented at `/opt/ubuntu_tank/libexec/recover-activation` and managed by `mentorpi-tank-recover.service`. Offline rollback restores last known working release and host configuration without repository checkout or network connection. Hardened systemd service unit (`mentorpi-tank.service`) confines runtime with dedicated `ubuntu-tank` user and `mentorpi-rrc` group, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `DevicePolicy=closed`, `IPAddressDeny=any`, `IPAddressAllow=localhost`, empty capability bounding set, 2s watchdog, and 5s stop timeout. Non-interactive launch wrapper `bin/mentorpi-tank-run` runs supervisor in main process, handles signals with safe zeroing, and notifies systemd. Full 19-test deployment regression suite (`test_milestone5_deployment.py`) passes 100%. Physical target-Pi acceptance with real motors and tracks raised remains scheduled for Milestone 6.


### Milestone 6 — Raised-track controller acceptance (reopened)

- [x] Record historical target preflight: Pi 5 Model B Rev 1.1, USB `1a86:55d4`,
  `/dev/rrc`, and live battery telemetry. Recheck before every physical run.
- [ ] Observe finite forward, reverse, left, and right motion with tracks raised.
- [ ] Confirm actual motor polarity and accept conservative velocity/RPS limits
  and geometry/correction values on the exact tank.
- [ ] Measure stop latency for keyboard lease expiry, guard timeout, process
  failures, service stop, serial loss, and host shutdown.
- [ ] Determine and record actual STM32 behavior after host-command loss.
- [ ] Publish reproducible physical evidence and operator limitations.

Exit criterion: all four requested motions are observed, every required stop
condition meets an accepted measured bound, and restart remains disarmed.
Status: Incomplete. The September 13 diagnosis supersedes earlier checked-off
motion and firmware claims. Existing kinematic/simulation results remain software
evidence only. No on-ground use is authorized. Execute the remaining physical
gates through Milestone 9 after Milestones 7 and 8 pass.

### Milestone 7 — Production loopback DDS and systemd correction

Dependencies: existing Milestones 4–5 implementation and the September 13 native
mock-board diagnosis. Status: Complete (2026-09-13).

- [x] Package the shared profile and environment contract in §6.4.1; update
  service launcher and every operator/status/verification entrypoint before ROS
  context creation. Document profile purpose, paths, overrides, and failures.
- [x] Migrate existing installations transactionally, preserve calibration and
  enclave isolation, and test immutable-prefix packaging, activation failure,
  offline rollback, and interrupted-activation recovery of matching assets.
- [x] Move both start-limit directives into `[Unit]`; validate the unit with
  `systemd-analyze verify` and inspect effective restart-throttling properties.
- [x] Add a repeatable native-Pi test using installed modules, service/operator
  identities, SROS2 Enforce, and the production sandbox with serial access denied.
  Preserve the old-discovery failure as a regression fixture and require delivery
  through controller, guard, bridge, and mock SDK with the candidate profile.
- [x] Test cold starts, all four directions, repeated CLI participant creation,
  and concurrent read-only status under domain 0. Capture loopback-only traffic
  and absence of denied discovery sends for the working profile.
- [x] Prove unauthorized local/status participants cannot arm or command motion;
  exercise forbidden non-loopback network, filesystem, and device access. Test
  missing/invalid profile rejection, restart throttling, and disarmed restart.

Exit criterion: a packaged candidate passes native delivery and negative security
checks with the IP filter intact, and installation/rollback selects consistent
assets. Mock callback success is not physical or real serial-write acceptance.
Likely files: `config/fastdds/loopback.xml`, `host/mentorpi-tank.env`,
`host/mentorpi-tank.service`, `bin/mentorpi-tank-run`, `deploy.sh`, deployment and
runtime-verification scripts, bringup clients, and their integration tests.

### Milestone 8 — Bounded arming and verified delivery acceptance

Dependencies: Milestone 7 for native integrated acceptance; guard/report unit
work can proceed independently. Status: Complete.

- [x] Implement the monotonic first-command deadline in §8.4; test expiry with
  no input, delayed input, repeated arm, re-arm, time jumps, and stale caches.
  Preserve the existing command freshness, speed limits, and stop behavior.
- [x] Implement the stage-specific observations in §10.4, including successful
  serial-write evidence, without granting operator/status actuator-topic access.
  Update message/API documentation and signed policy generation as needed.
- [x] Make bench prepare subscriptions before arm, require fresh correlated
  downstream evidence for each burst and stop, and fail closed on a broken edge,
  timeout, rejected command, process restart, write error, or short write.
- [x] Reproduce publication-plus-arming with zero downstream receipts and require
  a failed delivery result. Exercise each disconnected pipeline edge, stale
  receipts, false/mock write success, and failed disarm/cleanup confirmation.
- [x] Add native fake-board/PTY coverage that observes actual encoded frame writes
  into the test sink; distinguish that evidence from real `/dev/rrc` writes.
  Production-style tests must never open the physical serial device.
- [x] Revise console, JSON, Markdown, and operator documentation to distinguish
  software delivery, physical movement, physical stopping, and firmware behavior.
  Require owner observation for physical movement and instrumentation for timing;
  leave unmeasured gates incomplete even if all software checks pass.

Exit criterion: no disconnected or failed-write path can produce a successful
software-delivery result; an armed guard with no first command expires within
its configured bound; no software-only run can certify physical acceptance.
Likely files: safety core/node, bridge/SDK, controller observations, bringup bench
and status clients, `scripts/bench_acceptance.py`, ROS interfaces/SROS2 policies,
and safety, bench, middleware, and serial integration tests.

### Milestone 9 — Installed candidate and physical acceptance closure

Dependencies: Milestones 7 and 8 pass their software/native gates. Status: Planned.
This milestone supplies the missing evidence for Milestone 6; it does not replace
or relax any original physical gate.

- [ ] Build/package on the native ARM64 baseline, install and activate through the
  immutable-release workflow, and record release/configuration identity. Validate
  installed entrypoints and sandbox, stopped recovery/rollback, and disarmed boot.
  Preserve previous diagnosis and reports before bench overwrites output files.
- [ ] With motor power disabled where practical, verify real serial ownership,
  successful full-frame writes and zeroing; use serial instrumentation to establish
  wire delivery. Investigate power, wiring, or firmware separately if host writes
  succeed but the chassis does not respond.
- [ ] Coordinate a current owner acknowledgment, raised tracks, battery/USB and
  ownership preflight, and accessible disconnect. Begin with bounded conservative
  0.5 s bursts, record actual movement/direction and the post-burst stopped state.
  Do not increase duration or speed to compensate for missing delivery evidence.
- [ ] Measure all §10.3 failure cases, including terminal loss, independent guard
  and bridge failure, serial disconnect, host shutdown, and service stop. Record
  fault onset, last nonzero, host zero write, observed physical stop, instrument
  resolution, accepted bound, and pass/fail separately; no synthetic substitutions.
- [ ] Measure STM32 command-loss behavior with an agreed power-cut contingency.
  Treat earlier 275 ms host-zero and 1000 ms firmware numbers as unverified claims,
  not measurements or a guaranteed firmware watchdog. If safe stopping is not
  established, document the blocker and required hardware/firmware remedy.
- [ ] Update the evidence record and operator guide and close Milestone 6 only
  when every required physical gate passes. Keep incomplete gates explicit.

Exit criterion: the installed release demonstrates all four observed motions and
measured safe stops, with proven command-loss behavior and disarmed restart.
On-ground operation still requires a separate decision and is not authorized by
this milestone or by the diagnosis.

## 12. Definition of done for this phase

The controller-only phase is complete when:

- a clean Ubuntu 26.04 Pi 5 can be prepared using only files under
  `ubuntu_tank/`;
- ROS 2 Lyrical and dependencies install reproducibly;
- the reused MentorPi packages build and run natively;
- an audited artifact installs as a root-owned versioned release below
  `/opt/ubuntu_tank/releases`, and production runs only through
  `/opt/ubuntu_tank/current/install`;
- host configuration, persistent state, and runtime state remain separated under
  `/etc/opt/ubuntu_tank`, `/var/opt/ubuntu_tank`, and `/run/ubuntu_tank`;
- activation and offline rollback are atomic, preserve configuration, and return
  the service disarmed;
- localhost-only discovery and enforced SROS2 policy prevent uncredentialed LAN
  or local processes from arming or commanding motion;
- the only enabled actuator command path is motor output through the
  disarmed-by-default guard;
- forward, backward, left, right, and stop succeed in bounded raised-track tests;
- command loss and process failures stop within a measured, accepted latency;
- systemd startup is disarmed and safe across reboot; and
- `ubuntu_tank/README.md` contains the exact install, deploy, operate, recover,
  and rollback procedures with recorded validation status.

Quantitative RAM, boot-time, storage, reliability, and on-ground safety claims
must not be made until they have been measured on the target Pi and tank.

## 13. Normative installation references

- [ROS 2 Lyrical installation](https://docs.ros.org/en/lyrical/Installation.html)
  is authoritative for supported platforms and choosing Debian packages.
- [ROS 2 Lyrical Ubuntu binary installation](https://docs.ros.org/en/lyrical/Installation/Alternatives/Ubuntu-Install-Binary.html)
  is authoritative for Ubuntu 26.04 ARM64 prerequisites and repository setup.
- [Filesystem Hierarchy Standard 3.0: `/opt`](https://refspecs.linuxfoundation.org/FHS_3.0/fhs/ch03s13.html)
  defines the `/opt`, `/etc/opt`, and `/var/opt` separation used by this design.

The implementation must pin or record the exact instructions and package
versions it validates. Links labelled `latest` or content from an active ROS
release may change after this document is written.

### Production build-root bootstrap and artifact identity

The native first-package workflow bootstraps an absent disposable root from the
already prepared Ubuntu 26.04 ARM64 host, verifies the installed lockfile package
versions, and copies system tools/ROS without copying home or robot state.
`prepare_build_root.py --dry-run` explains the operation; the real bootstrap is
root-only and constrained below the workspace `.work` directory. This does not
replace target preparation or provide workstation cross-compilation.

Packaging builds independently of checkout development artifacts. The builder
records the actual production prefix, source identity, and file hashes, which
packaging and installation validate. Conflicting archives cannot reuse an
installed release ID. Host controller velocity limits are enforced before motor
publication, and the keyboard process consumes the validated host teleop settings.
