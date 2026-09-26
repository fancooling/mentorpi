# C4 file changes

Inventory updated on 2026-09-26 against the C3 baseline
(`91fc82fca1ab71ebe3970a86df5da1bc4540b0c7`). Includes all current tracked and
untracked source changes: **32 files — 26 modified, 6 added, none deleted**,
including this document. Paths are repository-relative. This replaces the C3
inventory; unchanged files from C3 are not repeated.

C4 adds host ownership and paired Docker deployment. Incomplete deployments
block Start/Arm; successful deployment leaves the controller stopped and disarmed.
Implementation includes a containerd image-identity correction after the first
Pi staging attempt. C4 deployment, redeployment, reboot and power-on checks passed
on the Pi; other C4 fault tests were explicitly waived by the owner, not passed.

| Status | File | What changed and why |
| --- | --- | --- |
| Modified | `CHANGES.md` | Replaces the C3 inventory with every current C4 file change, its rationale and validation boundary. |
| Modified | `CONVERSATION_MEMORY.md` | Records C4 behavior, local validation, the final ARM64 release identity and pending Pi/physical checks for the next session. |
| Modified | `GEMINI.md` | Identifies paired containers as the current Ubuntu deployment path and distinguishes historical native physical acceptance from pending Docker acceptance. |
| Modified | `README.md` | Replaces the outdated native-scaffold status with the container guide and current C4/C5 validation status. Preserves factory-stack separation. |
| Modified | `docker/ubuntu_tank/README.md` | Documents host prerequisites, retained configuration/TLS/SROS2, preparation, staging, deployment, stopped-pair verification, admission variables, serial recovery, log retention and manual Git fallback. Explains paused-container shutdown and the first manual deploy's additional service-driven redeployment. |
| Modified | `docker/ubuntu_tank/build.py` | Uses BuildKit configuration digests independently of the local Docker image store. Explicitly includes the new protocol deployment module in the allowlisted build context before its first commit, so both images contain the C4 gate. |
| Modified | `docker/ubuntu_tank/compose.yaml` | Supplies release ID, build hash and deployment token to both services; labels the release and mounts the host admission directory read-only. Keeps existing isolation and hardware mappings. |
| Added | `docker/ubuntu_tank/tls_setup.py` | Prepares TLS and browser origins with installed web dependencies; validates and preserves existing identities. Mounted only for explicit provisioning, so current images remain usable. |
| Modified | `ubuntu_tank/tests/test_container_web.py` | Exercises real TLS creation, idempotence, generated origins and rejection without identity replacement. Does not simulate Pi installation. |
| Added | `docker/ubuntu_tank/image_identity.py` | Verifies portable configuration digests across classic and containerd image stores. Tags discover candidates; pinned local IDs are used only after content verification. Caches immutable verified pairs to avoid repeated exports at boot. |
| Added | `docker/ubuntu_tank/deploy.py` | Resolves verified local image IDs, installs the shared resolver and records local IDs in target evidence. Implements staging, host preparation, paired deployment, boot, stop, status, bounded logs and target verification. Validates immutable image identities/manifests and mounted configuration through installed consumers; rejects conflicting owners, masks native services, records undo information and provisions account/device permissions and log retention. Serializes replacement, closes admission before shutdown, kills paused containers without explicitly unpausing them, and permits control only after verifying the stopped replacement pair. `target-test --redeploy` checks actual container replacement and configuration preservation. |
| Modified | `docker/ubuntu_tank/runtime_start.py` | Requires production deployment configuration and validates the selected generation/build before existing mount, configuration, device and owner-lock checks. Prevents obsolete runtime startup. |
| Modified | `docker/ubuntu_tank/smoke.py` | Resolves portable release identities before running images. Creates disposable admission records, passes the release identity to test containers, verifies approval and rejects a web startup with an obsolete generation. Retains HTTPS, ROS-free imports, shared IPC and server-replacement checks; bounds Docker calls to 90 seconds. |
| Added | `docker/ubuntu_tank/ubuntu-tank-container.service` | Orders host boot and shutdown around Docker and required storage. Invokes the installed host CLI; application process supervision remains inside runtime. |
| Modified | `docs/MENTORPI_CONTAINER_REFACTOR_DESIGN.md` | Records owner-scoped C4 Pi acceptance and the explicitly waived tests, summarizes its admission/ownership tooling and links the host guide. Records remaining historical-helper cleanup before C6 release. |
| Modified | `ubuntu_tank/README.md` | Directs current operations to Docker and identifies older native instructions as historical/fallback documentation. Keeps real-Pi and physical acceptance pending. |
| Modified | `ubuntu_tank/bin/mentorpi-tank-run` | Checks host admission before startup, immediately before launching ROS and throughout the controller loop. Revocation stops the graph through existing shutdown handling. Adds the protocol package to source-checkout imports and tidies imports. |
| Modified | `ubuntu_tank/deploy.sh` | Retains development testing and help; removes native build/deployment/control dispatch and directs production operations to the Docker CLI. Uses the repository `.venv`, including inherited shell-helper Python commands, and adds admission tests to the suite. |
| Modified | `ubuntu_tank/scripts/deployment_manager.py` | Rejects direct CLI execution with the new deployment path. Historical helper APIs remain for existing consumers/tests; this is not wholesale removal of native helper code. |
| Modified | `ubuntu_tank/src/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py` | Requests exclusive serial open so cooperating bridges cannot share the chassis port. Updates adaptation documentation; host checks remain necessary for noncooperating holders. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/state_machine.py` | Reports the deployment release ID, rejects Arm without approval, cancels late Arm confirmation after revocation, and releases ownership/stops through the safety loop when approval disappears. |
| Added | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/deployment.py` | Provides ROS-free admission and startup identity checks. Approval must match release, generation and host boot ID; missing or malformed approval fails closed. Startup rejects obsolete selections/builds. Standalone product use without configured deployment admission remains supported. |
| Modified | `ubuntu_tank/src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/lifecycle_service.py` | Rejects Start while admission is closed and rechecks it during serialized startup and readiness waiting. Stop remains available independently of host deployment locking. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/entrypoint.py` | Validates deployment identity before normal web startup, rejecting an obsolete generation or mismatched build. OpenAPI export remains independent of deployment state. |
| Modified | `ubuntu_tank/tests/fixtures/container_ipc.py` | Validates the selected deployment identity before starting the installed, motor-free IPC fixture used by image smoke tests. |
| Added | `ubuntu_tank/tests/test_container_admission.py` | Adds three behavioral tests covering stale/missing approvals, cancellation of pending Arm and late success, and ownership revocation requiring fresh acquisition. Uses operator state transitions without hardware. |
| Modified | `ubuntu_tank/tests/test_container_supervision.py` | Runs process fixtures with valid host approval and adds a real-process test that removes approval, observes graph shutdown, rejects Start, permits Stop and requires explicit Start after approval returns. |
| Modified | `ubuntu_tank/tests/test_milestone4_bringup.py` | Removes six tests for retired native shell arm/disarm/status dispatch. Renames the remaining client test class; retains runtime client, timeout, DDS and security checks. |
| Modified | `ubuntu_tank/tests/test_milestone5_deployment.py` | Removes the obsolete disposable native-build cleanup test and shell teleop-dispatch assertions. Retains configuration-to-teleop argument and lease behavior checks. |
| Modified | `ubuntu_tank/tests/test_milestone6_acceptance.py` | Removes the retired `deploy.sh bench` dispatch test while retaining acceptance orchestration and motion-safety behavior tests. |
| Modified | `ubuntu_tank/tests/test_rmw_integration.py` | Removes a native shell-status test based on synthetic ROS output. Adds a second-open rejection check to the real PTY serial test; retains frame exchange and disconnect behavior. |
| Modified | `ubuntu_tank/tests/test_target_test.py` | Removes the old shell forwarding test for native `target-test`. The Docker host CLI now exposes the production target command; historical orchestrator tests remain. |

The uncommitted `docker/ubuntu_tank/deploy.sh` wrapper was removed; it was not
present in `HEAD`, so it is not counted as a Git deletion. Development commands
use the repository `.venv`; Pi host commands use system Python directly.
No tracked files were deleted. The existing Dockerfile, TLS implementation and
web shell entrypoint are unchanged. Historical native modules and unit templates
remain in the repository; removing shell dispatch does not remove those files.
Git-ignored build contexts, images, manifests, logs and review artifacts are
outside the source inventory.

Validation completed during C4 implementation:

- Development and browser suites passed, including 13 real-process supervision
  tests and three admission tests. Native DDS and live RRC/udev checks skipped
  on the development computer.
- Both final ARM64 images built under emulation. Installed-image smoke checks
  passed HTTPS/static assets, ROS-free web, shared IPC permissions, server
  replacement and obsolete-generation rejection.
- The release reader accepted the C4 manifest and rejected a C3 manifest without
  the admission gate. A disposable local Docker container confirmed direct
  SIGKILL works while paused; no robot hardware was attached.
- Frontend type checking, new-code Python lint/formatting, shell lint, Compose
  parsing and whitespace checks passed. Existing modified Python files gained
  no lint diagnostics. Independent critical-only reviews reported `PASS`.

Build evidence: `ubuntu_tank/.work/c4-build-final/release.json`, release ID
`5e0dea917ebe31451dd918c855b403c72c0f3c91bef72f62a25aea77869c618e`.
The final staged application inputs matched the working tree.

Pi evidence: `ubuntu_tank/.work/c4-pi-evidence/evidence/` contains initial,
redeployment, reboot and power-on reports for release
`3ac501e52612b5b2a5b0352e5f5285e5fa6481dd304d6a4add7a75ae57c64b05`.
All four report PASS_STOPPED_INTEGRATION; image identities match the release,
all 40 configuration hashes are unchanged, both containers changed on redeploy,
and boot IDs match the recovery reports. The owner observed the power cycle.
Evidence/build artifacts remain Git-ignored. USB reconnect, competing-owner and
interrupted-update tests were waived by the owner; DDS and C5 physical acceptance
remain unverified. No motor operation or push was performed in this work.

C4 image-store compatibility follow-up (2026-09-26): the Pi reported manifest
IDs from Docker's containerd store, while the release retained configuration
digests. The actual transport archive binds both sets of IDs to the same content.
The host CLI and smoke tool now resolve this distinction without modifying the
release or accepting tags as execution identities. Subsequent Pi staging and
deployment passed with the correction.
Validation for the correction: actual archive hashes link the Pi-reported IDs to
the release config digests; real Docker export verification and wrong-digest
rejection passed. The installed-image ARM64 smoke test, Python lint/format,
whitespace checks and independent review passed. The correction reused the
existing images; no image rebuild was required.

Explicit TLS provisioning: `setup-tls` uses the verified web image to prepare a
missing certificate/key and browser origins while applications are stopped. It
rejects partial or invalid identities and preserves existing valid pairs. Host
installation now copies `tls_setup.py` and preflights the full bundle before
cutover. Existing images and release manifests need no rebuild or modification.
TLS setup validation: five TLS/HTTPS product tests passed, and the existing ARM64
web image exercised creation, name coverage, repeat-run preservation and rejection
of partial, mismatched, corrupt and expired identities. Lint/format, whitespace
and independent review passed. Subsequent Pi TLS setup and staging succeeded.
