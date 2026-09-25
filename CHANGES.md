# C2 file changes

Inventory checked on 2026-09-25 against `HEAD` (`f7fb68b538e03def719d177397fed9aa9acf88de`).
The C2 worktree contains 33 changed files: 23 modified, 9 added, and 1 deleted.
This inventory adds `CHANGES.md` as the 34th file. Paths are repository-relative.

C2 replaces native controller lifecycle operations with Supervisor, adds an
independent safety monitor, and makes controller shutdown terminate the complete
ROS process group. Native runner/lifecycle startup is no longer supported by
these entrypoints. Images, host ownership/deployment tooling, and real-Pi
validation remain pending in C3–C6.

| Status | File | What changed and why |
| --- | --- | --- |
| Added | `CHANGES.md` | Records every current C2 file change and its rationale, as requested. |
| Modified | `CONVERSATION_MEMORY.md` | Records C2 behavior, deadlines, validation results, and remaining Pi work so later sessions have an accurate handoff. |
| Added | `docker/ubuntu_tank/supervisord.conf` | Defines controller, operator, lifecycle, and safety-monitor processes; private Unix control API; group shutdown; and bounded log rotation. Disables controller autostart and all automatic process restarts so recovery cannot resume motion automatically. |
| Modified | `docs/MENTORPI_CONTAINER_REFACTOR_DESIGN.md` | Marks C2 complete on the development computer, records numeric supervision deadlines, and links the implementation guide. Keeps C3–C6 and physical validation pending. |
| Modified | `ubuntu_tank/README.md` | Explains that current runner/lifecycle entrypoints require Supervisor and that the native commands describe the previous delivery. Links C2 setup and test instructions. |
| Modified | `ubuntu_tank/bin/mentorpi-tank-lifecycle` | Imports the lifecycle server from the supervisor package instead of web and supports `UBUNTU_TANK_LIFECYCLE_SOCKET`. Documents execution as the container application UID so the launcher matches the new server location and socket configuration. |
| Modified | `ubuntu_tank/bin/mentorpi-tank-operator` | Adds the supervisor source package to the checkout import path so the operator can load its new progress-reporting dependency without an installed workspace. |
| Modified | `ubuntu_tank/bin/mentorpi-tank-run` | Replaces systemd notifications, native deployment-lock/journal checks, and host hardware-exclusion calls with Start admission and reciprocal safety-monitor checks. Moves heartbeat/PID paths into the private runtime directory, launches ROS in its own process group, stops directly on stale guard/bridge progress, and escalates group shutdown after 500 ms. This lets container runtime supervision enforce stopping without host systemd; host exclusion is deferred to C4. |
| Modified | `ubuntu_tank/deploy.sh` | Runs the new C2 process suite and adds the supervisor source directory to Python paths used by tests and existing command wrappers. Keeps source-checkout execution working after the dependency change; does not implement Docker deployment. |
| Added | `ubuntu_tank/docs/CONTAINER_SUPERVISION.md` | Documents processes, IPC permissions, admission/epoch behavior, deadlines, C3 environment requirements, test commands, and validation limits so the runtime can be integrated and verified reproducibly. |
| Modified | `ubuntu_tank/docs/WEB_DEPENDENCY_CLOSURE.md` | Records that the lifecycle server now belongs to the runtime supervisor package and uses Supervisor. Clarifies that web remains a ROS-free socket client. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/package.xml` | Declares the supervisor runtime dependency for ROS package dependency resolution. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/setup.py` | Declares the same supervisor dependency for Python package installation. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/agent_node.py` | Reports progress from the safety timer and restricts default container IPC access to the application UID. On runtime epoch change, releases ownership or stops the state machine before zero/disarm, invalidating pending Arm and old motion authority so a later timer publication cannot revive motion. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/entrypoint.py` | Runs the safety timer in simulation mode so motor-free process tests exercise real operator progress and epoch handling without ROS. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/ipc_server.py` | Reports IPC-loop progress separately from the ROS timer, uses mode `0600` for container sockets, and handles closed-descriptor `ValueError` during shutdown. Allows the monitor to detect a stalled IPC loop and avoids a shutdown-thread exception. |
| Modified | `ubuntu_tank/src/ubuntu_tank_supervisor/package.xml` | Declares the shared protocol dependency because the moved lifecycle server consumes protocol constants. |
| Modified | `ubuntu_tank/src/ubuntu_tank_supervisor/setup.py` | Declares the protocol dependency for Python installation and updates the package description to include container lifecycle responsibilities. |
| Added | `ubuntu_tank/src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/lifecycle_service.py` | Reuses the former server's socket framing in the runtime package and replaces systemd/journal operations with fixed Supervisor operations. Adds same-UID access, byte-bounded frames/log responses, serialized Start admission, token-bound readiness, priority Stop, and direct group signalling so Stop wins against pending startup or stalled control RPC. |
| Added | `ubuntu_tank/src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/progress.py` | Provides atomic private progress records, process/time freshness checks, shared numeric deadlines, Start revocation, epoch changes, and bounded process-group signalling. Gives the runner, agent, lifecycle service, and monitor one shared supervision contract. |
| Added | `ubuntu_tank/src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/runtime_monitor.py` | Adds a separate safety process and Supervisor event listener. Checks operator, lifecycle, and runner progress and the local Supervisor API; stops controller groups directly and terminates failed infrastructure. Remains able to act when the runner or lifecycle adapter hangs. |
| Added | `ubuntu_tank/src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/supervisor_api.py` | Implements XML-RPC over the private Supervisor Unix socket with a 200 ms timeout and separate connections. Exposes fixed controller operations and bounded log retrieval, removing dependence on host `systemctl` and `journalctl`. |
| Deleted | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/lifecycle_service.py` | Removes the native lifecycle server from web after moving/reworking it in the supervisor package. Keeps runtime process management outside the web package. |
| Added | `ubuntu_tank/tests/fixtures/runtime_graph.py` | Supplies real guard/bridge child processes that emit credentialed heartbeats, plus a controllable startup gate. Makes hangs, interrupted startup, and descendant cleanup testable without ROS or motors. |
| Added | `ubuntu_tank/tests/runtime-requirements.txt` | Pins Supervisor 4.3.0 for reproducible development process tests. Production image dependency pinning remains C3 work. |
| Modified | `ubuntu_tank/tests/test_container_protocol.py` | Removes the special import prohibition for the deleted web lifecycle module. The existing prohibition on the entire supervisor package continues to enforce the ROS-free web boundary. |
| Added | `ubuntu_tank/tests/test_container_supervision.py` | Adds 12 tests using real Supervisor/runtime processes and the operator state machine. Covers stopped boot, logs, Start/Stop races, malformed frames/socket permissions, required-process exits, each monitored hang, guard/bridge heartbeat loss, group cleanup, explicit restart, and authority revocation during driving and pending Arm. |
| Modified | `ubuntu_tank/tests/test_milestone11_operator_agent.py` | Adds the supervisor source package to test imports so existing operator behavior tests resolve the new dependency. |
| Modified | `ubuntu_tank/tests/test_milestone12_web_api.py` | Imports the moved lifecycle service, updates injected process/log operations and import-isolation checks, and removes three tests for the retired native deployment-lock/recovery-journal lifecycle checks. Preserves tests for web behavior, priority Stop, and failure confirmation against the new adapter. |
| Modified | `ubuntu_tank/tests/test_milestone13_browser_pwa.py` | Updates the lifecycle import, source path, and injected operation names so existing real-browser/PWA tests run with the runtime server. |
| Modified | `ubuntu_tank/tests/test_milestone14_installed_integration.py` | Adds the supervisor test import path and includes that package in the copied-launcher fixture. Ensures clean-environment lifecycle launcher probes still resolve all required packages and preserve source hashes. |
| Modified | `ubuntu_tank/tests/test_milestone5_deployment.py` | Gives child-exit tests valid runtime admission and monitor progress, and removes two tests for native runner lock/journal gates that no longer exist. The diff also removes the `TestReviewFindingsRound4` class declaration/docstring, placing its retained methods in the preceding class; this is an incidental grouping change, not required by C2. |
| Modified | `ubuntu_tank/tests/test_source_boundary.sh` | Recognizes `http` and `xmlrpc` as Python standard-library modules so the dependency audit accepts the new Supervisor client without inventing external dependencies. |
| Modified | `ubuntu_tank/tests/test_target_test.py` | Adds the supervisor source package to test imports so existing target-orchestrator unit tests can load operator code. No real-Pi test was executed by this change. |

`review.md` was also refreshed during independent review and currently contains
`PASS`. It is Git-ignored and is not included in the 33-file implementation count.
Supervisor 4.3.0 was installed into the existing repository `.venv` for testing;
that local environment is also outside the source inventory.

The recorded implementation validation includes all 12 C2 tests, existing
development/browser suites, frontend type checking, shell lint, and no newly
introduced lint diagnostics in modified existing Python files. The full test
command stopped at a copied-launcher fixture missing the moved package; that
fixture was corrected, its suite passed on rerun, and the remaining suites passed
separately. Native DDS/live udev checks skipped on the development computer.
No deployment, motor operation, commit, or push was performed.
