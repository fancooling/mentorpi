# MentorPi Conversation Memory

Last updated: 2026-09-12

This is the repository-local handoff between sessions. Read `GEMINI.md` for the
current architecture and safety constraints; use `README.md` for commands.

## User intent

- Preserve the vendor Raspberry Pi OS, factory `MentorPi` container as a
  rollback baseline, and the host drivers/startup contract.
- Customize camera streaming, LiDAR presentation, telemetry, perception, and
  later robot behavior without rebuilding the vendor driver stack.
- Keep the implemented sidecar image free of controller and driving programs.
- Store enough context in the repository for future sessions to resume easily.
- Keep the implemented sidecar as the low-risk path while designing a separate,
  opt-in full-stack image that can replace `MentorPi` when behavior ownership
  is desired. The two robot stacks must never run concurrently.

## Current implementation

- Custom image: `mentorpi-fan:latest`.
- Container: `MentorPiFan`.
- Compose project: `mentorpi-fan`.
- Canonical Compose file: `docker/customization/docker-compose.yml`.
- Build context: `docker/customization/` only.
- Dashboard: port 8081.
- Factory services reused: camera HTTP on 8080 and rosbridge on 9090.
- The image contains Nginx and static assets only, with no ROS, drivers,
  controllers, teleoperation, navigation, or hardware access.
- Browser code emits only rosbridge `subscribe` and `unsubscribe`; sidecar port
  8081 explicitly returns 404 for `/ros` and `/ros/`.

Runtime isolation uses a non-root user, read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, a constrained `/tmp` tmpfs, and no
bind mounts, devices, or privileged mode.

The repository is tracked in Git on branch `main`. `.gitignore` excludes the
52 GB `.img` disk image, extracted `third_party_src/`, core dumps,
transient ROS 2/Python build artifacts, PyTorch model weights (`*.pt`), and
oversized CAD meshes (`wheel_link.stl`).

## Deployment workflow

The deployment script at `docker/customization/deploy.sh` operates only on
`mentorpi-fan`:

```bash
./docker/customization/deploy.sh local
./docker/customization/deploy.sh test
./docker/customization/deploy.sh remote pi@ROBOT_IP
```

Every command rebuilds from the narrow customization context, then validates
Compose isolation, image architecture, runtime user, observer labels, Nginx
configuration, and absence of ROS/control operations. Remote deployment
preflights gzip, Docker, Compose v2, and ARM64; requires the factory `MentorPi`
container to be running; verifies the transferred immutable image ID; and
checks runtime isolation. Failed local or remote health/isolation checks
preserve an existing container if it was not replaced; otherwise they remove
the failed container and restore the previous image tag when available. The
script deliberately does not restart an old image through potentially
incompatible new configuration. It does not install host files or manage the
factory container.

The built image lives in the local Docker daemon, not the repository. The
current validated build has image ID
`sha256:66874357c1ff2c446c8d776534fce0426d5b488cf27713f06711658e83215986`.
Nginx binds to all interfaces (0.0.0.0 and [::]) on port 8081 with host
networking and CORS enabled on /video/ for remote browser streaming.

## Retired legacy deployment

On 2026-09-05, the reconstructed privileged full-stack deployment was retired:

- Removed the old full-stack `docker/Dockerfile`.
- Replaced the privileged Compose definition with the sidecar-only definition,
  now at `docker/customization/docker-compose.yml`.
- Removed the duplicate `docker/docker-compose.fan.yml`.
- Removed legacy `host_setup/` and `container_env/` files.
- Rewrote the deployment script, now at `docker/customization/deploy.sh`; it no
  longer builds `mentorpi:latest`, installs udev/systemd files, mounts `/dev`, or
  starts a replacement `MentorPi`.
- Removed `SETUP_GUIDE.md`; root `README.md` is now canonical.

Vendor code in `mentorpi/src`, extracted code in `third_party_src`, and
`tools_and_models` were deliberately retained as reference material for future
customization. They are not included in the sidecar build context.

## Full-stack replacement design

Current-state correction (2026-09-07): `docker/original/` is absent from both
the worktree and Git index. The entries below record prior work, not an available
workflow. Do not run, reference, or reuse its guard/deployment files unless that
directory is deliberately restored and revalidated.

On 2026-09-05, `/mnt/rpi-rootfs` was re-audited as the authoritative source for
host setup, Docker metadata, and active container code. The resulting
`docs/MENTORPI_REPLACEMENT_CONTAINER_DESIGN.md` defines the full-stack
replacement requirements; it does not change the sidecar Compose/deployment.

On 2026-09-07, the independent `docker/original` workflow implemented an
opt-in `runtime-core` candidate: sanitized private source capture, digest-pinned
ARM64 multi-stage build, separate core/operator Compose files, non-root and
non-privileged runtime policy, compatibility helpers, a disarmed-by-default
motor freshness guard, runtime SBOM/provenance, immutable staging, factory
backup, acceptance-gated cutover, automatic failure restore, and explicit
rollback. The capture records both workspace revisions and dirty tracked-file
counts and excludes credentials, generated outputs, mutable robot data, and
unsupported optional tools from the build context.

The factory boot contract hard-codes container name `MentorPi` and launches ROS
with `docker exec`. Active configuration is MentorPi Tank V2.0.1 (2026-08-22),
ROS 2 Humble on Ubuntu 22.04 ARM64, MS200 lidar, Aurora depth camera, and ROS
domain 0. The active primary and third-party workspaces are opaque writable-
layer trees, so a fresh allowlisted extraction must preserve their working-tree
changes rather than use the imported base image or older repository copies.

Production gates include an exact live ROS/QoS baseline, resolution of
conflicting recorded `.typerc` bind paths, non-privileged device access, a
graceful zero-motor shutdown path, and a proven stale-command/STM32 watchdog.
No replacement cutover or on-ground motion is authorized until those safety
tests and a factory rollback drill pass.

## Validation history

- On 2026-09-07, `./docker/original/deploy.sh capture` completed against
  `/mnt/rpi-rootfs`, including source hashes, secret-pattern checks, package
  manifests, workspace revision metadata, and the disk-image checksum.
- The replacement's hardware-free `deploy.sh test` builds and inspects the real
  ARM64 image, validates Compose isolation and runtime provenance, loads ROS
  packages and launch files, runs motor-guard regressions, and verifies bounded
  signal shutdown without starting or replacing a `MentorPi` container. Record
  the final result and immutable image ID here only after the current test run
  completes.

- After `deploy.sh` and `docker-compose.yml` moved into
  `docker/customization/` on 2026-09-06, the deployment script's complete test
  command passed from the new path with the Compose context resolved to that
  directory.
- The ARM64 `mentorpi-fan` image built successfully and ran locally through
  QEMU on 2026-09-05.
- `MentorPiFan` reported healthy on port 8081.
- Runtime inspection showed `privileged=false`, `readonly=true`, user `nginx`,
  no binds/devices, `cap_drop=["ALL"]`, and `no-new-privileges=true`.
- Nginx configuration, JavaScript syntax, Compose rendering, and the health
  endpoint passed local checks.
- Camera and live LiDAR integration still require validation on a powered
  factory robot with hardware-backed ports 8080/9090.

## Native Ubuntu 26.04 motion controller design

On 2026-09-07, `docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md` was revised into a
design-only plan for a clean Ubuntu 26.04 Pi 5 with native ROS 2 Lyrical. The
first phase is controller-only: guarded forward, reverse, left, and right motion.
All authored source, configuration defaults, tests, deployment automation,
systemd/udev templates, and operator instructions live under a self-contained
repository `ubuntu_tank/` workspace. Production does not run from that checkout:
immutable root-owned releases install under
`/opt/ubuntu_tank/releases/<release-id>`, with
`/opt/ubuntu_tank/current` atomically selecting the active release. Host-specific
configuration lives under `/etc/opt/ubuntu_tank`, persistent mutable state under
`/var/opt/ubuntu_tank`, and volatile state under `/run/ubuntu_tank`. A dedicated
non-root `ubuntu-tank` account runs the systemd service. Install, activation,
rollback, start, and arming are separate operations; activation/rollback use a
write-ahead recovery journal and leave the service stopped and disarmed. A
hardware-free Milestone 1 scaffold exists; native installation, full graph
bringup, and physical deployment are not implemented yet.

The design copies the complete `ros_robot_controller_msgs`,
`ros_robot_controller`, and the complete controller Python module directory from
`mentorpi/src`, which is the primary code reference for this native target;
legacy controller launch surfaces that require Nav2 or peripherals are excluded.
`/mnt/rpi-rootfs` is a documented fallback only for required missing,
hardware-specific, or contradictory facts, and every use must record its reason,
path, and hash. No reusable guard exists in the current worktree, so the design
calls for one narrowly scoped new `ubuntu_tank_safety` package plus a minimal
AND-gating heartbeat supervisor. It adapts only the relevant vendor keyboard
logic into renewable, timeout-bounded motion leases.
Narrow porting changes remove environment and
absolute-path assumptions, complete dependency metadata, parameterize the serial
and kinematic contract, disable unused bridge and controller command endpoints,
expose read-only guard state, and add monotonic guard/bridge watchdogs. Production
discovery is localhost-only and distinct deny-by-default SROS2 enclaves protect
each command boundary. Camera, LiDAR, navigation, AI, joystick, Docker runtime,
and on-ground motion are outside this phase. Six trackable milestones
cover repository provenance, clean-host ROS installation, the Lyrical port,
guarded bringup, versioned native deployment, and raised-track acceptance.

On 2026-09-08, Milestone 1 (Repository scaffold and provenance) was implemented and validated:
- Created the `ubuntu_tank/` directory layout with tracked scaffold files: `config/controller.yaml`,
  `config/sros2/README.md`, `host/99-mentorpi-rrc.rules`, `host/mentorpi-tank.service`,
  `host/mentorpi-tank.env`, `scripts/install_ros2.sh`, `scripts/build_workspace.sh`,
  `scripts/check_host.sh`, `scripts/recover_activation.sh`, and `scripts/verify_runtime.sh`.
- Reused vendor packages `ros_robot_controller_msgs`, `ros_robot_controller`, and `controller` from
  `mentorpi/src` (Git baseline `ca32e0c`) with 0 fallbacks to `/mnt/rpi-rootfs`, completing direct package
  metadata declarations in `package.xml`.
- Created safety and control packages:
  - `ubuntu_tank_safety`: Disarmed-by-default motor guard enforcing monotonic freshness (250 ms timeout),
    valid 4-motor commands, and finite/bounded speed limits.
  - `ubuntu_tank_supervisor`: Trusted AND-gating systemd watchdog supervisor validating separate guard
    and bridge monotonic heartbeats via inherited kernel pipes or socket credentials (`SO_PASSCRED`).
    Under the documented single-owner threat model, owner-approved same-UID processes are trusted, and
    credential checks catch accidental senders or configuration mistakes.
  - `ubuntu_tank_teleop`: Keyboard teleoperation emitting renewable 150 ms velocity leases, halting
    motion within the configured lease after key repeat ceases or terminal focus is lost.
- Added dependency specifications and verification tools:
  - `versions.lock`: Pinned direct-package specification for Milestone 2, explicitly marked `closure_status: direct-only` until a clean ARM64 target transaction is captured and reviewed.
  - `docs/DEPENDENCY_CLOSURE.md`: Direct runtime dependency declarations, AST verification, allowlist,
    and target transitive closure specification.
  - `docs/RELEASE_MANIFEST_SPEC.md`: Version-plus-revision release ID and manifest schema.
  - `source-manifest.txt`: 100% provenance coverage for all 92 delivered payload files out of 93 Git-tracked
    files (`source-manifest.txt` intentionally self-excluding).
- Implemented comprehensive automated boundary and regression gates:
  - `tests/test_source_boundary.sh`: 5-stage verification (manifest provenance against `git ls-files`,
    Git-tracked layout assertion, 0 rootfs fallbacks, 100% direct AST import coverage including cross-workspace
    dependencies, controller-only allowlist enforcement, and zero perception/camera/LiDAR/AI code or tokens).
  - `tests/test_negative_boundary.sh`: Automated negative regression suite verifying that omitting
    cross-workspace or external dependencies triggers immediate gate failure with exact error reporting.
- Validation: `./ubuntu_tank/deploy.sh test` passed all 5 boundary stages, 2 negative regression tests,
  and 41 modular unit/integration tests (11 safety, 22 supervisor, 8 teleop).
- On 2026-09-09, Milestone 2 (Target-Pi Ubuntu and ROS installation workflow) was remediated and validated across all 11 review findings from `review.md`:
  - Verified upstream inputs & 4 pinned rosdep sources: Pinned official `ros2-apt-source` GitHub release 1.2.0 deb package and all four upstream rosdep snapshot sources (`index_v4`, `base`, `python`, `ruby`) at commit `a9f673b32f2469b5b3655f62d53f176ef69b233a` with exact SHA-256 hashes in `versions.lock`. Pure-Python parser and authentic `rosdep keys` / `rosdep resolve` pipeline verify all keys without error suppression.
  - Mandatory closure verification (`verify-closure`): Required `--candidates <manifest>` in `verify-closure`, enforcing exact package set equality and sha256 checksum match against `versions.lock`. Integrated mandatory closure verification into `cmd_install_ros` via `generate_candidate_manifest_from_archives`. Live `install-ros` and `install-deps` now fail before host mutation while the committed lock is `direct-only`; target installation requires a reviewed `closure_status: complete` lock.
  - Host baseline validation & reboot acceptance: Structured `record_host_baseline` captures Deb822 sources, `.list` files, ownership, architecture, and kernel; `validate_host_baseline` rejects permission errors or environment tampering. `cmd_prepare_host` explicitly signals pending reboot with exit code 2; reboot rerun refreshes accepted baseline.
  - Fail-closed host preflight: `check_host.sh` strictly validates EEPROM date format (rejecting malformed strings or 'garbage'), enforces character-device type for `/dev/rrc`, requires `fuser` when serial device is present, fails closed if Docker daemon is unreadable, and eliminates literal NUL bytes (`tr -d '\000'`). Process scanning filters process ancestors and checks explicit ROS node executables rather than broad name substrings.
  - Hardened lock and override security: `assert_no_mutation_overrides` strictly blocks all `UBUNTU_TANK_MOCK_*`, `UBUNTU_TANK_LOCK_DIR`, and custom `LOCK_FILE` variables unless `dry_run=true`. Elevated privileges ensure canonical root-owned deployment lock at `/run/lock/ubuntu_tank/deploy.lock`.
  - Apt failure boundary: `recover_apt_state` is limited to base-host preparation before the locked ROS transaction. Once `install-ros` begins verified source/package handling, dpkg, download, closure, or no-download installation failures abort without network-enabled recovery and preserve verified artifacts for inspection.
  - Complete test gates:
    - `tests/test_source_boundary.sh`: 100% manifest and provenance gate (95 payload files out of 96 Git-tracked files in `source-manifest.txt`).
    - `tests/test_negative_boundary.sh`: 2/2 negative boundary regression tests pass.
    - `tests/test_dependency_closure.sh`: 8/8 negative regression tests pass (including unpinned versions, perception exclusion, hash tampering, omitted dependencies, latest URL, candidate hash mismatch, transitive unlocks, and missing candidate manifest).
    - `tests/test_install_workflow.py`: 35/35 unit tests pass (preflight, authentic EEPROM format, JSON serialization, mutual exclusion, closure-state gates, reboot sequences, lock contention, apt recovery, mock override rejection, and upstream inputs).
    - Full test suite `./ubuntu_tank/deploy.sh test`: 100% pass across all stages (Milestone 1 safety/supervisor/teleop and Milestone 2 workflows).
  - Status: Milestone 2 workflow hardening is hardware-free complete, but the authoritative transitive package lock is not. Clean-Pi closure capture, installation, and acceptance remain pending.
- On 2026-09-09, Milestone 3 (Lyrical port and dependency closure) was implemented, audited against `review.md`, and validated across all 7 findings:
  - Fatal serial fault propagation: Updated `set_motor_state` to advance the motor-command freshness timestamp only after a successful serial write. Serial write failures (including write timeouts) mark `_fatal_fault = True`, cancel the heartbeat timer to immediately expire supervisor deadlines, trigger safe zeroing, and shut down the node/graph.
  - Constrained workspace cleanup (`--clean`): Added `validate_clean_target` to `build_workspace.sh`, canonicalizing paths and strictly rejecting any path outside `ubuntu_tank`, root/system directories (`/`, `/home`, etc.), and protected workspace directories (`src`, `config`, `host`, `scripts`, `docs`, `tests`).
  - Solver closure manifest extraction: Updated `generate_candidate_manifest_from_archives` in `install_ros2.sh` to extract the complete isolated solver/download artifact set (including downloaded transitive debs) and determine repository origins independently via exact-version `apt-cache madison`/`policy` results, rather than filtering against or copying from `versions.lock`. Unknown origins and unlocked transitive packages are exposed to and rejected by `verify-closure`.
  - Dependency-install ownership: `install-deps` no longer invokes network-enabled apt installation. It verifies that the closure-checked `install-ros` transaction already installed each rosdep-resolved package at its exact locked version, then runs dpkg and rosdep consistency checks.
  - Bounded serial I/O: Parameterized `Board` with 50 ms read polling, a 100 ms write timeout, and a fatal 500 ms receive-silence deadline, all declared in `ros_robot_controller.launch.py` and mapped into node parameters. Read exceptions, write failures, and persistent empty reads propagate fatal state to the bridge heartbeat path.
  - Dedicated controller-only battery telemetry: Added dedicated 1 Hz battery polling timer in `ros_robot_controller_node.py` when `controller_only=True`, publishing battery telemetry to `~/battery` without activating unused peripheral interfaces.
  - Package metadata synchronization: Synchronized `setup.py` with `package.xml` in both `controller` and `ros_robot_controller` (version `1.0.0`, Apache-2.0, maintainer `Ubuntu Tank Maintainers <dev@mentorpi.local>`).
  - Re-review remediation (2026-09-09):
    - Baseline permissions: Replaced string digit check with bitwise octal test (`0002` and `0020`), accepting `0644`/`0600` root-owned baselines while rejecting world/group-writable modes in live mode.
    - Dpkg-deb artifact parsing: Robustly parsed labeled output fields (`Package:`, `Version:`, `Architecture:`), verified against authentic `.deb` build artifacts.
    - Receive failure fault coordination: Kept serial port open after RX errors/silence timeout so bounded stop packets reach the STM32 during safe shutdown before closure; blocked nonzero motor commands in fatal state; caught short writes in `buf_write`.
    - Watchdog freshness latch: Watchdog expiration triggers fatal shutdown, cancels supervisor heartbeats, zeroes motors, closes the command path, and rejects subsequent commands.
    - Fault shutdown logging safety: Replaced unsupported `get_logger().critical()` with `get_logger().fatal()` and protected all shutdown logging in `try...except`, ensuring logger failures cannot abort motor zeroing, port closure, or ROS shutdown.
    - Stop-write attempt resilience: Updated `zero_motors` to preserve bounded attempt execution across all counts despite intermediate timeouts or short writes, preventing transient write faults on early frames from cancelling remaining stop attempts before port closure.
    - In-flight write and RX-fault shutdown serialization: Unified command admission, fatal error checks, motor writes, freshness updates, and signal-safe shutdown zeroing/close under reentrant locks (`RosRobotController._motor_lock` and `Board._write_lock`). Nonzero motor writes cannot interleave after stop zeros or port closure, and command timestamps cannot advance once fatal fault or shutdown is latched. Removed trailing whitespace at `ros_robot_controller_sdk.py:403`.
  - Status accuracy: Accurately reflected in `MENTORPI_FRESH_CONTROLLER_DESIGN.md` and `README.md` that Milestone 3 porting, watchdogs, fatal error handling, and hardware-free test suites are complete (53/53 unit tests in `test_milestone3_port.py`), while the complete target apt lock, clean Ubuntu 26.04 ARM64 rosdep/colcon compilation, real STM32 telemetry-cadence check, and installed-script target tests remain in progress pending physical target work.
  - Validation: Automated test suite `./ubuntu_tank/deploy.sh test` passed 100% (130 Python tests: 11 safety, 22 supervisor, 8 teleop, 36 install workflow, 53 porting/remediation), plus the source, negative-boundary, and dependency-closure shell gates.
- On 2026-09-09, Milestone 2 and Milestone 3 were deployed and verified natively on clean Ubuntu 26.04 ARM64 Raspberry Pi 5 (`tankubuntu`):
  - Milestone 2 acceptance:
    - Target host preflight: Passed initial `check-host` on clean Pi 5 with 0 errors and 0 warnings.
    - Host preparation & baseline: Ran `sudo ./deploy.sh prepare-host` to record baseline, upgrade system packages, enable universe, and produce accepted post-reboot baseline (`/var/opt/ubuntu_tank/deployment/host-baseline.txt`).
    - Transitive dependency closure capture: Captured authentic 424-package transitive dependency closure (168 MB) via clean ARM64 APT solver into `/var/cache/apt/ubuntu_tank_capture`.
    - Complete dependency lock: Updated `ubuntu_tank/versions.lock` with `closure_status: complete`, locking all 424 packages with exact versions, architectures (`arm64`/`all`), repositories (`ros2`/`ubuntu-resolute`), and SHA-256 hashes.
    - Pinned rosdep resolution: Pinned `rosdep update --rosdistro lyrical` to local snapshot sources.
    - Live ROS & dependencies installation: Executed `sudo ./deploy.sh install-ros` and `sudo ./deploy.sh install-deps` with `--no-download` from verified local cache; verified dpkg package presence and workspace closure with authentic rosdep (exit code 0).
    - Host verification: Reran `./deploy.sh check-host` on `tankubuntu`: PASSED with 0 errors and 0 warnings.
  - Milestone 3 acceptance:
    - Native Colcon compilation: Executed `./deploy.sh build --clean` on `tankubuntu`; all 6 workspace packages (`controller`, `ros_robot_controller`, `ros_robot_controller_msgs`, `ubuntu_tank_safety`, `ubuntu_tank_supervisor`, `ubuntu_tank_teleop`) built successfully.
    - ROS 2 Lyrical API remediation: Fixed removed `geometry_msgs.msg.Pose2D` in `odom_publisher_node.py` and `cp.py` with guarded imports.
    - Installed console-script imports: Tested all 5 console-script entry point callables (`ubuntu_tank_safety.motor_guard_node:main`, `ubuntu_tank_supervisor.supervisor_node:main`, `ubuntu_tank_teleop.teleop_key_node:main`, `controller.odom_publisher_node:main`, `ros_robot_controller.ros_robot_controller_node:main`) on authentic ROS 2 Lyrical libraries on `tankubuntu`; all loaded and verified successfully.
    - Installed launch parse test: Verified `ros2 launch ros_robot_controller ros_robot_controller.launch.py --print` parses and executes cleanly on `tankubuntu`.
    - Native test suite: Ran `./deploy.sh test` directly on `tankubuntu`; passed all 53 unit/integration tests and source/dependency shell gates.
    - STM32 RRC hardware verification: Installed udev rule template `host/99-mentorpi-rrc.rules` and created dedicated group `mentorpi-rrc`; verified symlink `/dev/rrc -> ttyACM0` with restricted permissions. Verified live serial communication with STM32 controller: confirmed streaming telemetry cadence (53 Hz IMU, 1 Hz battery at 12.23V).
  - Status: Milestone 2 and Milestone 3 are 100% completed and accepted on physical hardware. Milestone 4 (guarded bringup and safe teleop) is next.
- On 2026-09-10, Milestone 4 ("Guarded bringup and safe teleop") was implemented, verified, and integrated into `./ubuntu_tank/deploy.sh test`:
  - Package `ubuntu_tank_bringup` implemented with guarded bringup launch file (`launch/tank.launch.py`):
    - Explicit topic pipeline: `/controller/cmd_vel` -> `controller/odom_publisher` -> `/ubuntu_tank_safety/motor_input` -> `ubuntu_tank_safety/motor_guard` -> `/ros_robot_controller/set_motor_guarded` -> `ros_robot_controller` -> `/dev/rrc`.
    - Fail-closed graph shutdown: `OnProcessExit` handlers for `motor_guard_node`, `bridge_node`, and `controller_node` emit `Shutdown` event.
    - Parameter declarations with safe defaults: `controller_only=true`, `max_rps=2.0`, `guard_timeout_sec=0.250`, `serial_device=/dev/rrc`.
  - Safe teleoperation launch file (`launch/teleop.launch.py`):
    - Configurable renewable 150 ms leases (`lease_duration_sec=0.150`), `linear_vel=0.2`, `angular_vel=0.5`.
  - Guard state reporting:
    - Updated `motor_guard_node.py` to publish transient-local `std_msgs/msg/Bool` on both `/ubuntu_tank_safety/state` and `/ubuntu_tank_safety/armed` with `QoSProfile(depth=1, durability=TRANSIENT_LOCAL, reliability=RELIABLE)`.
  - Command surface stripping:
    - Verified `controller_only` mode strips `/app/cmd_vel`, `cmd_vel`, `set_pose`, `set_odom`, and servo state publisher in controller node.
    - Verified `controller_only` mode disables all non-motor endpoints (buzzer, oled, rgb, bus/pwm servos, reception) in bridge node.
  - SROS2 Security Policies (`ubuntu_tank/config/sros2/`):
    - `governance.xml`: Enforces deny-by-default access control, rejects unauthenticated participants (`allow_unauthenticated_participants=FALSE`), and enforces payload/metadata encryption (`ENCRYPT`).
    - `policies.xml`: Configures 5 distinct least-privilege enclaves (`/ubuntu_tank/controller`, `/ubuntu_tank/guard`, `/ubuntu_tank/bridge`, `/ubuntu_tank/operator`, `/ubuntu_tank/status`).
    - DDS permissions XML files (`permissions/*.xml`): Per-enclave OMG DDS-Security permissions with `<default>DENY</default>`.
    - Verification script (`scripts/sros2_policy.py`): Programmatic audit asserting strict topic ownership, unauthenticated rejection, and least-privilege isolation.
  - CLI operations:
    - `deploy.sh arm`: Fails closed without `--ack-tracks-raised`, checks factory container mutual exclusion, calls `/ubuntu_tank_safety/set_arm` with `data=True`.
    - `deploy.sh disarm`: Calls `/ubuntu_tank_safety/set_arm` with `data=False`.
    - `deploy.sh status`: Queries transient-local guard state topic.
    - `scripts/verify_runtime.sh`: Checks `ROS_LOCALHOST_ONLY=1`, verifies topic graph, and reads guard state.
  - Test Suite (`tests/test_milestone4_bringup.py`):
    - 24 regression tests covering launch graph structure, bypass prevention, fail-closed handlers, controller-only stripping, transient-local state, repeated zero emission (destroy_node, disarm, timeout, invalid command), renewable teleop leases, fault injection (wall-clock jump, ROS time pause, one-child-hung supervisor), SROS2 access control, and CLI arm acknowledgment.
  - Source boundary and provenance:
    - Updated `test_source_boundary.sh` allowlist with `ubuntu_tank_bringup`.
    - Updated `source-manifest.txt` with SHA-256 hashes for all 16 new/modified files (111 payload files tracked).
    - Code review remediations (addressing all 7 findings from review.md):
      - Finding 1 (P1 - ROS launch enclaves): Replaced unsupported `enclave='...'` keyword with `ros_arguments=['--enclave', ...]` on all `launch_ros.actions.Node` definitions in `tank.launch.py` and `teleop.launch.py`, eliminating unexpected keyword `TypeError`.
      - Finding 2 (P1 - Bringup security preflight): Added `validate_security_preflight` in `tank.launch.py` to fail closed before graph execution if `ROS_LOCALHOST_ONLY != '1'`, `ROS_SECURITY_ENABLE != 'true'`, `ROS_SECURITY_STRATEGY != 'Enforce'`, or if the SROS2 keystore/enclaves are missing; configured `SetEnvironmentVariable` for discovery and SROS2 enforcement in both launch files.
      - Finding 3 (P1 - Official OMG DDS-Security XSD compliance): Stored official OMG 20170901 governance and permissions schemas in `ubuntu_tank/config/sros2/schemas/`. Rewrote `governance.xml` and all 5 permission documents (`guard`, `controller`, `bridge`, `operator`, `status`) to strictly validate against official OMG schemas with exit code 0 via `libxml2`. Updated `sros2_policy.py` to validate official XSD schemas and simulate allowed/denied participant traffic.
      - Finding 4 (P1 - Signal shutdown stop delivery): In `motor_guard_node.py`, updated `handle_sig` to call `node.destroy_node()` (publishing 5 stop messages) while the ROS context is still valid before calling `rclpy.shutdown()`.
      - Finding 5 (P1 - Fail closed on container enumeration error): In `deploy.sh cmd_arm`, captured `docker ps -a` error output and fail closed with code 1 if container inventory cannot be enumerated.
      - Finding 6 (P2 - Teleop keyboard input path): In `teleop_key_node.py`, added `/dev/tty` fallback when `sys.stdin` is not an interactive tty; exposed and documented `./deploy.sh teleop` (`ros2 run ubuntu_tank_teleop teleop_key`); verified key delivery via virtual PTY integration test.
      - Finding 7 (P2 - Runtime verification error propagation): Rewrote `verify_runtime.sh` to track check failures, enforce `ROS_LOCALHOST_ONLY=1` and SROS2 security variables, verify guard begins disarmed (`data: false`), confirm exclusive publisher ownership (`motor_guard`), and exit 1 on check failures.
      - Manifest & whitespace hygiene: Fixed extra blank line at EOF in `source-manifest.txt` (passing `git diff --check`), added schemas, and refreshed all SHA-256 hashes (113 payload files tracked).
    - Full hardware-free test suite (`./deploy.sh test`) passes 100% (168 tests total: 11 safety, 22 supervisor, 10 teleop, 36 install workflow, 53 porting, 36 Milestone 4 bringup, and SROS2 security gate).
  - Status: Milestone 4 implementation is completed, reviewed, remediated across all findings, and validated hardware-free. Native target-Pi installation, systemd service packaging, and raised-track bench tests follow in Milestones 5 and 6.
  - Re-review remediation (2026-09-11 - Milestone 4):
    - Finding 1 (P1 - Middleware internal discovery endpoints): Permitted `ros_discovery_info` under both `<publish>` and `<subscribe>` across all 5 DDS permission documents (`controller`, `guard`, `bridge`, `operator`, `status`) and added matching `topic_rule` with metadata/data encryption in `governance.xml`. Checked and enforced by `sros2_policy.py`.
    - Finding 2 (P1 - Node infrastructure endpoints): In `operator_permissions.xml`, granted `rr/teleop_key/*Reply` and `rq/teleop_key/*Request`. In `teleop_key_node.py`, passed `start_parameter_services=False` to `super().__init__`. In `status_permissions.xml`, granted `rt/parameter_events` under `<publish>` and `<subscribe>`, preserving strict denial of motion and arming requests. Aligned `policies.xml`.
    - Finding 3 (P2 - Exact endpoint records in runtime verification): Replaced loose regexes in `verify_runtime.sh` with exact numeric counts and section-associated endpoint parsing via Python validator. Requires exact 1 publisher (`motor_guard`) and exact 1 subscriber (`ros_robot_controller`) on `/ros_robot_controller/set_motor_guarded`, and exact 1 subscriber (`motor_guard`) on `/ubuntu_tank_safety/motor_input`. Fails closed on 10 publishers, wrong node names, or missing subscribers.
    - Finding 4 (P2 - Wildcard pattern evaluation in policy isolation): Updated `sros2_policy.py` to evaluate effective DDS grants using glob matching (`fnmatch.fnmatchcase`) against parsed DDS permissions documents rather than literal string set membership against `policies.xml`. Disallowed broad wildcard patterns (`*`, `rt/*`, `rq/*`, `rr/*`) in permissions.
    - Finding 5 (P2 - Fail closed when schema validator unavailable): In `sros2_policy.py`, updated `validate_xml_against_xsd` to raise `PolicyValidationError` immediately if `LoadLibrary('libxml2.so.2')` fails rather than skipping validation.
    - Finding 6 (P2 - Hardware-free RMW integration harness & status accuracy): Created `ubuntu_tank/tests/test_rmw_integration.py` exercising OpenSSL-based authentic signed keystore creation, CMS verification of `governance.p7s` and `permissions.p7s`, untrusted CA participant rejection, tampered signature rejection, virtual PTY serial bridge communication with STM32 framing/telemetry, active real-time scheduling / clock pauses (> 250 ms), and supervisor child process faults. Updated `docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md` to keep physical target-Pi acceptance explicitly marked as pending target environment validation (Milestone 6).
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (179 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 40 Milestone 4 bringup, 6 RMW integration, and SROS2 security gate). All 6 XML documents validate against official OMG XSD schemas. Manifest and provenance gates pass 100% across all 114 payload files.
  - Review remediation (2026-09-11 - P1 CLI service endpoints for arm/disarm/status):
    - Dedicated operator & status clients: Created `operator_client.py` and `status_client.py` in `ubuntu_tank_bringup` with `start_parameter_services=False` and `start_type_description_service=False`, registered as console scripts in `setup.py`. Updated `deploy.sh` `arm`, `disarm`, and `status` to invoke dedicated clients under `/ubuntu_tank/operator` and `/ubuntu_tank/status`.
    - Pinned CLI & dedicated endpoint grants: In `operator_permissions.xml`, granted `rr/operator_client/*Reply`, `rr/_ros2cli_requester_*/*Reply`, `rq/operator_client/*Request`, and `rq/_ros2cli_requester_*/*Request`. In `status_permissions.xml`, granted `rr/status_client/*Reply`, `rr/_ros2cli_direct*/*Reply`, `rq/status_client/*Request`, and `rq/_ros2cli_direct*/*Request`, while strictly keeping status denied from publishing any service requests (`rq/*`) or motion commands. Aligned `policies.xml` and `sros2_policy.py`.
    - Test harness reboot isolation: Parameterized reboot-pending file resolution (`resolve_reboot_check_file`) in `check_host.sh` and `install_ros2.sh` to respect test mock targets and prevent workstation-local reboot flags from failing dry-run tests.
    - Integration & regression tests: Added `TestSecuredCliAndDedicatedClientStartup` in `test_rmw_integration.py` exercising OpenSSL-signed CMS policies against a fake guard (verifying arm/disarm execution, status telemetry read, and hard rejection of status arming/motion), and expanded `test_milestone4_bringup.py` with deploy client invocation tests and option verifications.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (187 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 46 Milestone 4 bringup, 8 RMW integration, and SROS2 security gate). Manifest and provenance gates pass 100% across all 116 payload files.
  - Review remediation (2026-09-11 - Controller executable and battery message type):
    - Controller launch executable resolution (Finding 1 - P1): Corrected controller executable from `odom_publisher_node` to `odom_publisher` in `ubuntu_tank/src/ubuntu_tank_bringup/launch/tank.launch.py`, matching the console-script entry point exported in `ubuntu_tank/src/controller/setup.py`. Added regression tests in `test_milestone4_bringup.py` asserting all bringup and teleop launch executables match package entry points and resolve in a clean install tree.
    - Battery message type in status client (Finding 2 - P1): Replaced non-existent `ros_robot_controller_msgs.msg.BatteryState` with `std_msgs.msg.UInt16` in `status_client.py` and read millivolt telemetry from `msg.data`. Added unit and regression tests in `test_milestone4_bringup.py` with message stubs and in `test_rmw_integration.py` confirming `deploy.sh status` reports armed and disarmed states with known voltage readings (12.23 V and 12.15 V) under enforced SROS2 policies.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (192 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 50 Milestone 4 bringup, 9 RMW integration, and SROS2 security gate). Manifest and provenance gates pass 100% across all 116 payload files.
  - Review remediation (2026-09-11 - Target-Pi libxml2 ABI compatibility and xmllint fallback):
    - Ubuntu 26.04 libxml2 ABI resolution (Finding 1 - P1): Updated `ubuntu_tank/scripts/sros2_policy.py` with multi-candidate dynamic loader `_load_libxml2()` supporting `ctypes.util.find_library('xml2')`, official Ubuntu 26.04 ABI `libxml2.so.16` (SONAME change in upstream libxml2 >= 2.14 / Ubuntu `libxml2-16`), `libxml2.so.2`, and `libxml2.so`. Added CLI fallback to locked `xmllint` executable (`libxml2-utils`) while preserving fail-closed rejection when no validator is available.
    - Added regression tests in `test_milestone4_bringup.py` verifying schema validation passes for valid policies and catches malformed XML under the simulated Ubuntu 26.04 `.16` ABI, validates and rejects malformed XML via `xmllint` fallback, and fails closed with `PolicyValidationError` when no validator is present.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (194 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 52 Milestone 4 bringup, 9 RMW integration, and SROS2 security gate). Manifest and provenance gates pass 100% across all 116 payload files.
- On 2026-09-11, Milestone 5 ("Native host deployment and operations") was implemented, verified, and integrated into `./ubuntu_tank/deploy.sh test`:
  - Checksummed packaging & immutable install tree (`scripts/deployment_manager.py`):
    - `package_release`: Emits compressed archive `dist/ubuntu-tank-<release-id>-<arch>.tar.zst` containing `release-manifest.txt` with RFC 822 key-value headers and SHA-256 checksums, sizes, and octal permissions for all packaged payload files. Scans install tree and rejects leaked checkout or build paths.
    - `install_release`: Extracts into `/opt/ubuntu_tank/releases/<release-id>`, verifies file integrity, and enforces root-owned, non-writable permissions. Fails closed on tampered files, architecture mismatch, or differing existing release bytes. Initializes `/etc/opt/ubuntu_tank/controller.yaml` and `mentorpi-tank.env` only when missing, strictly preserving existing user calibration. Does not update `current`, start the service, or arm motors.
  - Crash-consistent atomic activation & offline rollback:
    - 6-step transactional activation: acquires `/run/lock/ubuntu_tank/deploy.lock`, creates checksummed snapshot under `/var/opt/ubuntu_tank/deployment/snapshots/<tx-id>/`, logs `PREPARED` to write-ahead journal (`activation-journal`), stops/disarms service, stages/replaces host files and reloads systemd/udev (`ACTIVATING`), atomically updates `/opt/ubuntu_tank/current` symlink with fsync, and writes `COMMITTED`. Automatic rollback on failure.
    - Offline rollback: Restores last verified release symlink and restores host configuration and units from snapshot without repository checkout or network access.
    - Boot recovery runner: `scripts/recover_activation.sh` and `/opt/ubuntu_tank/libexec/recover-activation` run by `mentorpi-tank-recover.service` before the controller at boot; reconciles interrupted `PREPARED` or `ACTIVATING` transactions, restoring previous safe baseline and leaving service stopped and disarmed.
  - Systemd confinement & non-interactive supervisor runner:
    - `host/mentorpi-tank.service`: Confinement directives enforced: dedicated non-login `User=ubuntu-tank`, `Group=mentorpi-rrc`, `Type=notify`, `NotifyAccess=main`, `WatchdogSec=2s`, `TimeoutStopSec=5s`, `Restart=on-failure`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `RestrictSUIDSGID=yes`, `CapabilityBoundingSet=`, `AmbientCapabilities=`, `DevicePolicy=closed`, `DeviceAllow=/dev/rrc rw`, `IPAddressDeny=any`, `IPAddressAllow=localhost`, `ReadWritePaths=/var/opt/ubuntu_tank /run/ubuntu_tank`, `ReadOnlyPaths=/opt/ubuntu_tank /etc/opt/ubuntu_tank`.
    - `host/mentorpi-tank-recover.service`: Oneshot boot recovery unit executing `recover-activation` before `mentorpi-tank.service`.
    - `host/ubuntu-tank.conf`: tmpfiles.d configuration creating runtime `/run/ubuntu_tank` (0750) and state `/var/opt/ubuntu_tank` directories.
    - `bin/mentorpi-tank-run`: Non-interactive launcher executing supervisor in main process (PID = MAINPID), supervising ROS 2 bringup child process, sending `READY=1` and `WATCHDOG=1`, and coordinating graceful stop zeroing on SIGTERM/SIGINT.
  - Review remediation (2026-09-11 - Milestone 5 host deployment, recovery, identity, and supervisor):
    - Finding 1 (P1 - Self-contained recovery implementation and hard startup gate): Bundled `deployment_manager.py` and `config_migration.py` directly into `/opt/ubuntu_tank/libexec/` so `recover-activation` executes without `PYTHONPATH` or checkout dependencies. Added `Requires=mentorpi-tank-recover.service` to `mentorpi-tank.service` to hard-gate controller startup on recovery success.
    - Finding 2 (P1 - Service identity provisioning and filesystem ownership normalization): Implemented `_provision_service_identities()` to create `mentorpi-rrc` group and `ubuntu-tank` system user if missing. Normalized tar extraction with `--no-same-owner` and reset release tree to root ownership with strict `0555`/`0444` permissions. Set `/var/opt/ubuntu_tank/ros-log` and `/run/ubuntu_tank` ownership to `ubuntu-tank:mentorpi-rrc` (mode `0750`) and executed `systemd-tmpfiles --create`.
    - Finding 3 (P1 - Host safety configuration applied to production graph): In `bin/mentorpi-tank-run`, loaded and validated `/etc/opt/ubuntu_tank/controller.yaml` via `validate_config` (failing closed on invalid configuration) and mapped all kinematics, guard parameters (`max_rps`, `guard_timeout_sec`), calibration factors, and serial bridge settings into ROS launch arguments.
    - Finding 4 (P1 - Reject recovery and rollback on unverified baseline): In `recover_activation()` and `rollback_release()`, required verified previous release integrity (`validate_release`) and valid snapshot checksums (`verify_snapshot`) before mutating symlink or restoring assets. Preserved pending journal transaction and failed closed without switching symlink when baseline is corrupt or missing.
    - Finding 5 (P1 - Confirm controller stopped and inactive before changing assets): In `_stop_and_disarm_service()`, strictly verified inactivity via `systemctl is-active`, escalated to `SIGKILL` on timeout, and raised `RuntimeError` if service remains active or stop command fails.
    - Finding 6 (P1 - Propagate unexpected child termination as supervisor failure): In `bin/mentorpi-tank-run`, distinguished signal-driven shutdown from unexpected child termination; propagated unexpected child exit codes (e.g. 17, 23, and unexpected 0) as non-zero supervisor exits to trigger systemd `Restart=on-failure`. Added `_UBUNTU_TANK_TEST_CHILD_CMD` hook for supervisor process testing.
    - Added dedicated regression test class `TestReviewRemediations` in `ubuntu_tank/tests/test_milestone5_deployment.py` asserting all 6 remediations under isolated conditions.
  - Review remediation (2026-09-11 - Milestone 5 host deployment, rollback recovery, supervisor credentials, and serial identity):
    - Finding 1 (P1 - Rollback transaction journaling and recovery): In `rollback_release()`, journaled rollback with `record_rollback_prepared()` (`ROLLING_BACK` state) before any file, configuration, unit, or symlink mutation. Extended `recover_activation()` to handle interrupted rollback (`ROLLING_BACK`) by verifying the rollback target snapshot and release, completing restoration, and updating the symlink and journal without leaving mixed code/assets.
    - Finding 2 (P1 - Retried activation reconciles pending recovery record): Hardened `ActivationJournal.record_prepared()` to reject new transactions if an uncommitted transaction already exists. Updated `activate_release()` under `DeploymentLock` to detect interrupted pending transactions and reconcile the prior baseline via `_recover_activation_unlocked()` before taking a new snapshot.
    - Finding 3 (P1 - Startup serialized with deployment lock and transaction gate): Updated `deploy.sh cmd_start` and `mentorpi-tank-run` preflight to probe `deploy.lock` and inspect `activation-journal.json`, failing closed (exit code 1) if a deployment lock is held or an uncommitted transaction is pending. Set `RemainAfterExit=no` in `mentorpi-tank-recover.service` so systemd re-executes recovery before the controller on every start.
    - Finding 4 (P1 - Deactivating waited on and query errors rejected): In `_stop_and_disarm_service()`, waited through `deactivating` until confirmed `inactive` or `failed`. Rejected empty status queries and ambiguous states with `RuntimeError`.
    - Finding 5 (P1 - Supervisor credential and monotonic freshness validation): In `mentorpi-tank-run`, added `validate_heartbeat()` enforcing expected UID (`os.geteuid()`) and expected PID (`expected_guard_pid` / `expected_bridge_pid`), and rejecting wrong-PID, wrong-UID, missing `SCM_CREDENTIALS`, negative (`-100.0`), non-finite (`NaN`, `inf`), future, stale (`age > deadline`), and replayed datagrams.
    - Finding 6 (P1 - Fail closed on unreadable or corrupt journals): In `ActivationJournal.get_state()`, distinguished legitimate first installation (`not os.path.exists`) from corrupt/unreadable files or invalid JSON/schema, raising `RuntimeError`. Updated `recover_activation()` and `mentorpi-tank-run` to fail closed and block startup without altering deployment assets.
    - Finding 7 (P1 - Preserve udev serial identity and reject ambiguous rules): Added `extract_udev_discriminator()`, `merge_udev_rule()`, and `is_ambiguous_udev_rule()` to `ReleaseManager`. Preserved existing host `ATTRS{serial}` in `/etc/udev/rules.d/99-mentorpi-rrc.rules` when staging candidate rules, and rejected ambiguous rules matching multiple connected adapters or wildcard serials.
    - Added dedicated regression test class `TestReviewFindingsRound2` in `test_milestone5_deployment.py` covering all 7 remediations.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (226 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 52 Milestone 4 bringup, 9 RMW integration, 32 Milestone 5 deployment, and SROS2 security gate). Manifest and provenance gates pass 100% across all 122 payload files.
  - Review remediation (2026-09-11 - Milestone 5 disposable build root packaging, resumable host install, and authoritative startup serialization):
    - Finding 1 (P1 - Build the packaged install tree at its production prefix): Created `scripts/build_disposable_root.sh` implementing the isolated ARM64 disposable build root workflow targeting `/opt/ubuntu_tank/releases/<release-id>/install` without leaked checkout paths. Added `--production`, `--symlink-install`, and `--no-symlink-install` to `scripts/build_workspace.sh`. Updated `package_release()` in `deployment_manager.py` with `install_tree` and `build_root` support, resolving production install trees from `.work/build_root` or synthesizing self-contained trees at the release prefix.
    - Finding 2 (P1 - Complete interrupted host installation before returning success): Extracted `_provision_host_assets()` and updated `install_release()` in `deployment_manager.py` so retrying an existing verified release executes and completes all host provisioning steps (configuration, runtime dirs, tmpfiles, libexec recovery runner) without starting/activating the service. Preserved existing user calibration and configuration during repeated/resumed installation.
    - Finding 3 (P1 - Serialize startup against the actual deployment transaction): Standardized authoritative lock and journal paths across `bin/mentorpi-tank-run`, `deploy.sh`, and `host/ubuntu-tank.conf`: `/run/lock/ubuntu_tank/deploy.lock` and `/var/opt/ubuntu_tank/deployment/activation-journal`. Configured `/var/opt/ubuntu_tank/deployment` directory permissions to mode `0755 root root` so the service UID (`ubuntu-tank`) can read the journal, while preserving restricted `0700` permissions on `snapshots/`. Acquired non-blocking shared lock (`LOCK_SH | LOCK_NB`) on `deploy.lock` in `bin/mentorpi-tank-run` and held it through preflight, environment sourcing, config validation, socket binding, and child process launch, releasing it only once steady-state supervision begins. Controller startup and `deploy.sh start` fail closed on unreadable/corrupt journals or uncommitted transactions.
    - Added dedicated regression test class `TestReviewFindingsRound3` in `test_milestone5_deployment.py` covering all 3 findings.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (229 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 52 Milestone 4 bringup, 9 RMW integration, 35 Milestone 5 deployment, and SROS2 security gate). Manifest and provenance gates pass 100% across all 123 payload files.
  - Review remediation (2026-09-12 - Milestone 5 release ID path traversal, disposable build cleanup, EOF fix, and container prefix build):
    - Finding 1 (P1 - Reject unsafe release IDs before deleting staging directories): In `scripts/deployment_manager.py`, implemented `ReleaseManager.validate_release_id()` requiring path-safe single-component format (`^[0-9a-zA-Z][0-9a-zA-Z._-]*$`, strictly rejecting `/`, `\`, `\0`, `.`, `..`, and absolute paths). In `package_release()`, validated `release_id` prior to staging path resolution, verified canonical staging directory containment strictly inside `.work/staging` before calling `shutil.rmtree()`, rejected symlinks, and applied `validate_release_id()` across `install_release()`, `activate_release()`, and CLI handlers `cmd_package` and `cmd_activate`.
    - Finding 2 (P1 - Constrain disposable-build cleanup to disposable directories): In `scripts/build_disposable_root.sh`, added `validate_disposable_target()` enforcing that target directories passed to `--clean` or `--build-root` are strictly contained within disposable `.work` trees. Explicitly rejected the workspace root, workspace ancestors, critical host/system roots (`/`, `/home`, `/etc`, `/usr`, `/var`, `/tmp`, `/opt`, `/root`, etc.), protected workspace subtrees (`src`, `config`, `host`, `scripts`, `tests`, `docs`, `bin`), and symlink escapes, preserving sentinel files. Also validated `RELEASE_ID` argument against path separators and traversal.
    - Finding 3 (P1 - Remove the executable trailing `EOF`): In `scripts/build_disposable_root.sh`, removed the stray executable `EOF` command at line 206 that caused exit code 127 (`EOF: command not found`). Verified direct script invocation exits with code 0 and confirmed the automatic subprocess packaging path in `package_release()` completes packaging, producing `.tar.zst` and valid manifest.
    - Finding 4 (P1 - Build inside the disposable root at the actual production prefix): In `scripts/build_disposable_root.sh`, added `verify_arm64_ubuntu_rootfs()` asserting Ubuntu 26.04, `arm64` architecture, and ROS 2 Lyrical setup in target rootfs. Configured isolated compilation via `systemd-nspawn` or `chroot` mounting sources read-only at `/build_ws` and compiling directly into `/opt/ubuntu_tank/releases/<release-id>/install` internally without leaked host paths or host-prefixed colcon arguments. Added `--opt-dir` parameter and populated both target build root and rootfs install trees under `--allow-staged-install`. In `test_milestone5_deployment.py`, verified dry-run outputs container isolation plan with internal production prefix `/opt/ubuntu_tank/releases/<release-id>/install` and target architecture `arm64`, and validated hardware-free artifact packaging, standalone execution, and python module imports after deleting checkout and build roots.
    - Added dedicated regression test class `TestReviewFindingsRound4` in `test_milestone5_deployment.py` covering all 4 findings.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (233 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 52 Milestone 4 bringup, 9 RMW integration, 39 Milestone 5 deployment, and SROS2 security gate). Manifest and provenance gates pass 100% across all 123 payload files.
  - Review remediation (2026-09-12 - Milestone 5 colcon build options, explicit output copy, failed-build remnants, and install tree completeness):
    - Finding 1 (P1 - Production builds pass an unsupported colcon option): Removed unsupported `--no-symlink-install` from all colcon invocations and dry-run messages in `scripts/build_disposable_root.sh` (as colcon defines `--symlink-install` as `store_true` where disabling requires omission). In `test_milestone5_deployment.py`, exercised all generated commands against the locked upstream colcon build argument parser, verifying clean parsing and confirming rejection when `--no-symlink-install` is supplied.
    - Finding 2 (P1 - Packaging can select an empty install tree left by the builder): Removed pre-creation of empty install directories before compilation in `scripts/build_disposable_root.sh`. Explicitly copied completed build output from `ROOTFS_INSTALL_DIR` to `TARGET_INSTALL_DIR` via `cp -a`, replacing any existing target directory. Cleaned up any partial or empty build remnants on failure, and verified target install completeness before exit. In `scripts/deployment_manager.py`, implemented `ReleaseManager.is_complete_install_tree()` and filtered candidate install trees to reject empty directories or directories missing `setup.bash`. Updated `validate_release()` to reject empty install trees, missing `setup.bash`, or packages containing only `setup.bash` without installed artifacts. In `test_milestone5_deployment.py`, simulated compilation producing recognizable files only inside rootfs, asserting they are copied and reach the packaged archive; repeated after failed build and verified packaging fails closed.
  - Review remediation (2026-09-12 - Milestone 5 hardware mutual exclusion, fresh install SROS2 keystore provisioning, bridge freshness timeout forwarding, and ROS setup nounset wrapping):
    - Finding 1 (P1 - Hardware-owner checks fail closed across all startup entrypoints): Added `check_hardware_mutual_exclusion()` in `deployment_manager.py` verifying Docker container inventory (failing closed if Docker command fails or daemon is unreadable), conflicting container presence (`MentorPi`, `MentorPiFan`, `mentorpi`, `runtime-core`), conflicting active host services (`mentorpi.service`, `mentorpi-start.service`, `mentorpi-fan.service`, `hiwonder-chassis.service`), and character device exclusivity (`fuser /dev/rrc`). Integrated checks into `deploy.sh cmd_start`, runtime service wrapper `bin/mentorpi-tank-run`, and boot recovery runner `recover_activation()`.
    - Finding 2 (P1 - Fresh installation provisions signed SROS2 security keystore): Added `_provision_sros2_keystore()` and `_is_valid_sros2_keystore()` to `ReleaseManager` in `deployment_manager.py`. Generated authentic SROS2 credentials (`identity_ca`, `permissions_ca`, signed `governance.p7s`, participant keys, certificates, and signed `permissions.p7s`) for all 5 required enclaves (`controller`, `guard`, `bridge`, `operator`, `status`) using `openssl`. Enforced directory permissions `0750` and file permissions `0640` with `ubuntu-tank:mentorpi-rrc` ownership. Updated `_provision_host_assets()` to fail closed if keystore credentials are missing or incomplete, satisfying `tank.launch.py` security preflight.
    - Finding 3 (P1 - Forward configured bridge stop deadline and enforce timeout invariant): Forwarded `serial_bridge.freshness_timeout_sec` from `controller.yaml` through `mentorpi-tank-run` into `tank.launch.py` launch arguments and into `RosRobotController` bridge node parameters. In both `config_migration.py` and `mentorpi-tank-run`, validated that `serial_bridge.write_timeout_sec < serial_bridge.freshness_timeout_sec`. Verified that a simulated command gap triggers the bridge watchdog at the configured deadline (e.g. 150 ms) and commands 4-motor zero.
    - Finding 4 (P1 - Disable nounset while sourcing upstream ROS setup): Wrapped `source /opt/ros/lyrical/setup.bash` with `set +u` / `set -u` in both `systemd-nspawn` and `chroot` compilation branches of `scripts/build_disposable_root.sh`, preventing failures when optional upstream variables like `AMENT_TRACE_SETUP_FILES` are unset.
    - Added dedicated regression test class `TestReviewFindingsRound6` in `test_milestone5_deployment.py` covering all 4 findings.
    - Full hardware-free test suite (`./ubuntu_tank/deploy.sh test`) passes 100% (239 tests total: 11 safety, 22 supervisor, 11 teleop, 36 install workflow, 53 porting, 52 Milestone 4 bringup, 9 RMW integration, 45 Milestone 5 deployment, and SROS2 security gate). Manifest and provenance gates pass 100% across all 123 payload files.


## Latest Milestone 5 review remediation (2026-09-12)

- Signed policy generations now use the permissions CA and S/MIME; activation
  repairs legacy PEM signatures while retaining CA and participant identities.
- Activation snapshots include security contents and ownership. Policy changes,
  interrupted activation, and rollback select matching signed grants. UUID
  transaction IDs prevent same-second snapshot collisions.
- Installation/activation run strict target and hardware exclusion checks under
  the deployment lock. Stop the managed service before these commands.
- `install --operator-user <login>` (default sudo caller) provisions operator and
  status group membership without hardware-group access. A fresh login is needed.
- Initial udev binding records one observed serial or stable USB port; subsequent
  releases preserve it and reject wildcard/unbound rules.
- Full `./ubuntu_tank/deploy.sh test` passed. The final expanded deployment
  suite passed 53 tests with one native-DDS skip; provenance verified all 123
  payload files and `git diff --check` passed.
- Native DDS verification is a hardware-free test that skips without ROS 2.
  The target SSH connection timed out during this remediation; no hardware was
  operated and target middleware/physical acceptance has not been established.

## Latest functional Milestone 5 remediation (2026-09-12)

- Controller speed caps now apply before motor publication; host teleop speeds
  and lease reach the keyboard process, with missing config rejected.
- Production packaging ignores checkout install output, verifies build-prefix,
  source and payload provenance, and rejects conflicting archives using one ID.
- An absent build root is bootstrapped offline from the prepared native Ubuntu
  26.04 ARM64 host with locked dependencies. Run `sudo -v` then
  `./ubuntu_tank/deploy.sh package`; no synthetic-install option is needed.
- Full hardware-free suite passed: 253 tests passed, one native-DDS test skipped;
  source provenance verified 124 payload files, and whitespace checks passed.
- Real ARM64 bootstrap/build acceptance remains to be performed on the native
  host. Workstation fixtures validate control flow and artifact rejection only.

## Milestone 6 raised-track acceptance and latency validation (2026-09-12)

- Orchestration CLI: Implemented `./deploy.sh bench --ack-tracks-raised` replacing
  prior stub, backed by `scripts/bench_acceptance.py` and `ubuntu_tank_bringup/bench_client.py`.
  Provides `--mock` for hardware-free simulation / regression verification and live mode
  for target-Pi execution with active controller services.
- Mandatory physical safety acknowledgment: Enforced `--ack-tracks-raised` for all
  bench commands; execution fails closed with exit code 1 if omitted. On-ground motion
  remains strictly forbidden.
- Preflight & hardware verification: Verified STM32 RRC USB identity `1a86:55d4`,
  battery cutoff threshold >= 9.60 V (nominal 11.1 V 3S LiPo), emergency power
  disconnect access within 0s, deployment lock exclusivity, and container/service
  mutual exclusion.
- Review remediations (Findings 1 & 2):
  - Finding 1 (P1 - Do not certify live motion and stopping from simulated results):
    Separated software simulation from physical target-Pi acceptance. In live mode
    (`mock=False`), motion acceptance requires live BenchClientNode execution and verified
    arming/disarming state against active controller services; unobserved runs fail closed
    without certifying bursts. Stop latencies reject synthetic timing/mock boards in live
    mode, marking physical bounds as pending physical instrumentation. STM32 command-loss
    characterization records pending physical bench test instead of claiming proven/characterized.
    Updated design doc, README, and acceptance reports to explicitly distinguish simulation
    from physical acceptance.
  - Finding 2 (P1 - Fail live battery preflight when telemetry is unavailable):
    In live mode, battery preflight requires fresh, authenticated telemetry via
    StatusClientNode under the `/ubuntu_tank/status` SROS2 enclave. Missing ROS, subscription
    failures, absent readings, or voltage < 9600 mV fail preflight; synthetic voltage is
    strictly forbidden outside simulation.
- Kinematic polarity & conservative limits: Verified forward (left < 0, right > 0),
  reverse (left > 0, right < 0), spin left (all > 0), spin right (all < 0), and stop
  (all 0.0) against accepted geometry (wheelbase 0.1368m, track width 0.1446m,
  sprocket 0.075m, left/right correction 1.0) and conservative limits (max linear <= 0.5 m/s,
  max angular <= 2.0 rad/s, max RPS <= 2.0 RPS).
- Simulated stop latencies across all 6 failure conditions:
  - Keyboard lease expiry: ~155 ms (bound <= 200 ms)
  - Guard freshness timeout: ~260 ms (bound <= 300 ms)
  - Teleop crash / command loss: ~260 ms (bound <= 300 ms)
  - Supervisor child crash: ~120 ms (bound <= 250 ms)
  - Service stop (SIGTERM): < 1 ms (bound <= 100 ms)
  - Serial loss / disconnect: ~510 ms (bound <= 600 ms)
  Physical target-Pi measurements remain pending physical instrumentation.
- STM32 chassis controller command-loss characterization:
  - Host zero delivery: Design specification <= 275 ms delivery of 4-motor zero packets.
  - STM32 firmware timeout: Vendor specification <= 1000 ms timeout for motor PWM cutoff.
  - Emergency disconnect: Manual power cut accessible within 0s reach.
- Reports and test suite: Emits structured JSON and Markdown acceptance reports.
  Added 28-test regression suite `tests/test_milestone6_acceptance.py` integrated
  into `./deploy.sh test`. Physical target-Pi bench execution remains scheduled for
  actual target-Pi hardware.

## Milestone 6 bench safety remediation (2026-09-12)

- Failed prerequisites now skip actuation. Every burst explicitly rearms and
  verifies guard state; pauses occur disarmed and failed bursts are not accepted.
- Bench holds a read-only shared deployment lock through cleanup, recognizes
  the managed bridge through systemd cgroup/executable metadata, and continues
  to reject unrelated serial owners. No extra operator filesystem rights needed.
- Status preflight and operator motion phases use separate, explicitly initialized
  ROS contexts. RMW security credentials bound at context initialization cannot
  leak across enclave boundaries. In fallback mode, preflight shuts down only its
  internally owned context and preserves caller-owned contexts. Both
  `StatusClientNode` and `BenchClientNode` accept explicit contexts.
- Avoid assigning the inherited read-only `Node.context` property in
  `StatusClientNode` and `BenchClientNode` (which raised `AttributeError: property
  'context' ... has no setter` under upstream rclpy). Pass context through
  `super().__init__(..., context=context)`, read the inherited property afterward,
  and store fallback context on private attribute `_fallback_context`. Fallback
  `_FallbackNode` also faithfully mirrors the read-only property descriptor.
- Bench preserves the ROS Python environment. Physical motion and latency
  acceptance remain pending; validation of this revision is hardware-free.
- Full suite passed: 294 tests passed, one native-DDS skip. All 41 bench tests
  passed; source provenance, negative boundary, and whitespace checks passed.

## Commit and review conventions

- Create a new Git version (commit) only for a new feature or milestone.
- Fold code review revisions into the existing feature or milestone commit by
  amending or squashing; do not leave separate review-fix commits or create a
  new version for those revisions. The rule is recorded in root `AGENTS.md` and
  `.agents/rules/git_versioning.md`.
- Workspace skill `.agents/skills/ack-review/SKILL.md` documents the step-by-step
  operational procedure to parse `review.md`, remediate findings, execute full
  test gates and manifests, and amend into the existing commit.

## Codex continuity

- Codex local memories are enabled on this workstation with
  `[features] memories = true` in `~/.codex/config.toml`.
- `AGENTS.md`, `GEMINI.md`, and this file provide repository-specific continuity
  independent of workstation-local memory.
