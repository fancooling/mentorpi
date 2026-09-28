# M14.3 change inventory

Updated 2026-09-27 against baseline `b45dfdd`. Covers the M14.3 changes, including the preceding design edits and this inventory:
**42 files — 38 modified, 4 added, none deleted**.
Paths are repository-relative. The ignored review output is listed separately.

M14.3 makes input expiry stop motion without disarming a healthy controller,
adds a configurable 1-second default and fresh-input recovery, and combines
startup/acquisition into Take control. Explicit Stop and hard faults still disarm.
Protocol 2 blocks the existing browser until M14.4 implements the new UI flow.

| Status | File | What changed and why |
| --- | --- | --- |
| Modified | `CHANGES.md` | Replaces the previous C4 inventory with the complete current change list, rationale and validation boundaries. |
| Modified | `CONVERSATION_MEMORY.md` | Records the approved timeout redesign, M14.3 implementation and validation, deployed-version boundary and remaining browser/robot work. |
| Modified | `docker/ubuntu_tank/README.md` | Documents combined Take control and explains that the protocol-2 backend requires M14.4 before browser driving is available. |
| Modified | `docker/ubuntu_tank/compose.yaml` | Mounts shared web.yaml read-only into the runtime so the operator enforces the configured input timeout. |
| Modified | `docker/ubuntu_tank/smoke.py` | Generates the installed IPC probe using the shared protocol version; the probe is exercised against a real operator socket so packaging checks accept protocol 2. |
| Modified | `docs/BUG_WEB_CONTROL_LEASE_EXPIRY.md` | Records the deployed binding fix, unresolved network stalls and the locally implemented pause/recovery redesign without claiming robot acceptance. |
| Modified | `docs/MENTORPI_WEB_CONTROL_DESIGN.md` | Defines pause versus disarm, neutral recovery, input generations, configurable timeout and combined acquisition; updates M14.3–M16 scope and completion status. |
| Modified | `ubuntu_tank/config/web/web.yaml` | Changes the shipped input lease default from 150 ms to 1 second; existing deployment overrides remain explicit. |
| Modified | `ubuntu_tank/docs/WEB_DEPENDENCY_CLOSURE.md` | Documents protocol 2, acquisition operations, recovery messages, configuration loading/restart rules and browser compatibility limits. |
| Modified | `ubuntu_tank/docs/openapi_v1.json` | Regenerates the public API contract for protocol 2, asynchronous acquisition and pause status; removes public controller/start and Arm affirmation. |
| Modified | `ubuntu_tank/src/ubuntu_tank_bringup/ubuntu_tank_bringup/bench_client.py` | Updates Arm calls and stops a burst on input pause rather than automatically recovering buffered motion. |
| Modified | `ubuntu_tank/src/ubuntu_tank_bringup/ubuntu_tank_bringup/operator_client.py` | Uses the revised Arm interface while retaining hardware-test acknowledgments at the tool boundary. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/package.xml` | Declares python3-yaml for runtime loading of the shared configuration. |
| Added | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/acquisition.py` | Adds runtime-owned asynchronous startup/acquisition, private deduplicated operation results, readiness and binding deadlines, cancellation and cleanup. Motion Stop preserves bound ownership. Controller Stop monotonically upgrades cleanup for cancelled workers, preventing delayed startup from leaving the service active. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/agent_node.py` | Wires configured timing and lifecycle coordination into the operator; publishes zero on pause and accepts downstream zero-write evidence for recovery. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/entrypoint.py` | Loads and validates shared web.yaml before startup, passes timing/speed settings and the lifecycle client, and preserves socket environment overrides. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/ipc_server.py` | Gates mutating commands by protocol version, exposes runtime acquisition operations and routes cancellation, binding and recovery status through IPC. |
| Modified | `ubuntu_tank/src/ubuntu_tank_operator/ubuntu_tank_operator/state_machine.py` | Separates input expiry from disarm using INPUT_PAUSED and input generations. Requires fresh neutral plus downstream zero for recovery; preserves hard-fault, idle and hold limits. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/config.py` | Rejects booleans, nonnumeric and nonfinite timing values before validating timeout bounds and challenge interval consistency. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/constants.py` | Advances protocol/schema versions to 2 and sets the default input lease to 1 second. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/enums.py` | Adds INPUT_PAUSED to the shared operator states. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/ipc_client.py` | Sends protocol version and input generations, exposes intent results, removes the Arm affirmation argument and stops bursts on recovery-required challenges. |
| Added | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/lifecycle_client.py` | Moves the bounded Unix-socket lifecycle client into the shared protocol package so runtime startup coordination does not import the web service. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/openapi_generator.py` | Updates generated OpenAPI and TypeScript contracts for acquisition operations, compatibility, pause/recovery fields and the revised Arm request. |
| Modified | `ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/schemas.py` | Adds input generation, pause reason and recovery readiness; validates response generations and removes the shared Arm affirmation field. |
| Modified | `ubuntu_tank/src/ubuntu_tank_teleop/ubuntu_tank_teleop/teleop_key_node.py` | Adapts terminal control to protocol 2; input pause triggers Stop, clears pending keyboard input and requires explicit Arm before further motion. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/lifecycle_client.py` | Re-exports the shared lifecycle client to preserve the existing web import path without duplicating implementation. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/models.py` | Updates HTTP models for protocol-2 acquisition, private operation results/cancellation, pause status and Arm without affirmation. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/operator_relay.py` | Retains the acquisition IPC connection, manages private operation/binding tokens, delegates deduplication to runtime and clears failed setup/socket state so acquisition, polling and cancellation recover after an operator restart. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/routes_api.py` | Replaces public Start with combined asynchronous Take control, exposes private operation polling/cancellation, updates Arm/status and preserves distinct Stop/release behavior. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/routes_ws.py` | Relays recovery challenges, input generations and acknowledgment/error state; uses the configured challenge interval. |
| Modified | `ubuntu_tank/tests/test_container_admission.py` | Adapts admission and late-Arm regressions to the revised Arm interface. |
| Modified | `ubuntu_tank/tests/test_container_supervision.py` | Supplies shared configuration to real Supervisor process fixtures and updates Arm calls while retaining authority-revocation coverage. |
| Modified | `ubuntu_tank/tests/test_milestone10_protocol.py` | Updates state-machine expectations and challenge generations for pause recovery; supplies explicit legacy timing where boundary tests need it. |
| Modified | `ubuntu_tank/tests/test_milestone11_operator_agent.py` | Migrates IPC/agent/terminal tests to the new Arm and expiry contracts, preserving explicit Stop/Arm recovery and hardware-tool acknowledgment coverage. |
| Modified | `ubuntu_tank/tests/test_milestone12_web_api.py` | Migrates HTTP/WS tests to asynchronous combined acquisition, lifecycle fixtures, protocol 2 and generation-bearing intent. |
| Modified | `ubuntu_tank/tests/test_milestone13_browser_pwa.py` | Keeps build/PWA checks and runs the protocol incompatibility browser test. Full driving scenarios await migration and re-enabling in M14.4. |
| Added | `ubuntu_tank/tests/test_milestone143_input_recovery.py` | Adds 28 behavioral tests for configuration, expiry, zero-confirmed recovery, stale input, terminal/burst safety, acquisition races/timeouts ownership retained after Stop, delayed lifecycle cancellation, socket-loss recovery and the executable packaging probe. |
| Modified | `ubuntu_tank/tests/test_milestone14_installed_integration.py` | Updates local API expectations for removed public Start, revised Arm and protocol 2; does not constitute installed-Pi validation. |
| Modified | `ubuntu_tank/web/src/composables/useControlSession.ts` | Makes minimal request-shape changes for generated types: protocol version on acquisition and no fabricated Arm affirmation. Full browser adaptation remains M14.4. |
| Modified | `ubuntu_tank/web/src/types/api.ts` | Regenerates TypeScript types for the revised API and recovery state. |
| Added | `ubuntu_tank/web/tests/protocol_fence.spec.ts` | Adds a real-browser check that the existing protocol-1 UI cannot acquire or arm the protocol-2 runtime. |

The implementation also updated the ignored local artifact `review.md` to
`PASS` after independent review and correction of the Stop/ownership regression.
It is not included in the Git change count above.

Validation: `./ubuntu_tank/deploy.sh test` passed all 555 tests across
23 groups plus source, negative-boundary and dependency-closure gates.
The focused API/recovery run passed 71 tests; the final 28-test recovery run
also verifies ongoing zero observations through the agent. These counts overlap.
Compose parsing, generated-contract comparison and whitespace checks passed.
All 29 changed authored Python files pass format checking; no new lint violations
remain relative to HEAD. Container smoke images were not built or run; the updated
IPC probe passed as a subprocess against a real local operator socket.

M14.3 is recorded as one milestone commit and is not deployed. Full browser driving/recovery is pending
M14.4; Pi deployment, physical zero delivery and stopping acceptance remain M15.
The browser checks above do not certify the old full driving suite or robot motion.

Review triage: the zero-delivery and hold-cap findings were not adopted. The
agent continuously refreshes zero-write evidence while paused; losing that
stream must still disarm. Expiry does not prove input was released, so its hold
budget remains until fresh neutral recovery. Tests cover two seconds of fresh
agent observations, stale delivery and an unreleased hold reaching its limit.
