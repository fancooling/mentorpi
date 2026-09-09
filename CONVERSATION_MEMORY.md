# MentorPi Conversation Memory

Last updated: 2026-09-09

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

## Commit and review conventions

- When addressing code modifications based on review comments, do not create new commits; amend/append changes into the existing git commit for that milestone.
- Create new git commits only when making progress on milestones or adding new features.

## Codex continuity

- Codex local memories are enabled on this workstation with
  `[features] memories = true` in `~/.codex/config.toml`.
- `AGENTS.md`, `GEMINI.md`, and this file provide repository-specific continuity
  independent of workstation-local memory.
