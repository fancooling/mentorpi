# MentorPi Pi 5 Web Control Design

Status: Milestones 10–11 completed; Milestones 11.1–16 pending implementation.

Date: 2026-09-16

Target: Native Ubuntu 26.04 ARM64 / ROS 2 Lyrical on the MentorPi Tank Pi 5.

## 1. Purpose and relationship to the native controller

Provide a page served by the Pi that the owner can open from a laptop, desktop,
or phone on the local network to start and stop the controller, arm and disarm,
drive forward/reverse, spin left/right, and inspect status and recent logs.
Driving supports both on-screen buttons and W/S/A/D; Space stops motion.

Selected stack: Vue 3 + TypeScript + Vite PWA frontend, Python FastAPI served
by Uvicorn, and a separate Python operator agent connected over a Unix socket.
The PWA provides the future phone controller using the same API and control
rules as the desktop page. Python permits reuse of configuration, validation,
and supporting libraries without combining web serving with motor supervision.

This extends [the native controller design](MENTORPI_FRESH_CONTROLLER_DESIGN.md).
Reuse its controller, motor guard, serial bridge, kinematics, speed enforcement,
loopback DDS configuration, SROS2 enforcement, and immutable release workflow.
The September 14 repository handoff records native raised-track acceptance;
this document does not independently reverify that evidence. Browser, network,
and new process failure paths require their own acceptance before web driving.

This is a native application under `ubuntu_tank/`, not an extension of the
factory observer in `docker/customization/`. Native/factory mutual exclusion
and sole ownership of `/dev/rrc` by the motion service remain mandatory.
Initial operation is raised-track only. This design does not authorize
on-ground motion, remote unattended driving, or operation beyond visual range.

Version 1 includes everyday controller operations, not a browser shell, package
installation, OS reboot, firmware updates, autonomous navigation, or camera and
LiDAR integration. Deployment and instrumented bench acceptance remain CLI tasks.

## 2. Page and operator interaction

Use a responsive single page with large labeled controls, visible keyboard
hints, and status expressed in text as well as color. Keep Stop motion visible
without scrolling. A conceptual layout is:

```text
MentorPi Tank             Connected | Controller running | Disarmed
Battery: 12.2 V           Control owner: this tab | Raised-track mode

[Start controller] [Stop controller] [Take control] [Release control]
[ ] I confirm the tracks are raised and the power disconnect is accessible
[Arm] [Disarm]

                          [Forward W]
                [Left A] [STOP Space] [Right D]
                          [Reverse S]

Hold a direction to move. Release to stop. Space stops and disarms.
Command: zero | Delivery: idle | Last update: 0.1 s ago
[Recent logs] [Diagnostics]
```

Values above are illustrative, not live readings.

| Control | Required behavior |
| --- | --- |
| Start controller | Start `mentorpi-tank.service`; wait for readiness and fresh disarmed state. Starting does not arm or move. |
| Stop controller | Invalidate driving, request zero/disarm, then stop the systemd service even if ROS is unavailable. Keep the web page available. |
| Take control | Acquire the single operator slot while disarmed; report who holds it if busy. Opening a page never takes control automatically. |
| Arm | Require explicit current tracks-raised acknowledgment, ownership, healthy preflight, neutral input, and a ready command path. |
| Forward / W | Hold for positive linear velocity, zero angular velocity. |
| Reverse / S | Hold for negative linear velocity, zero angular velocity. |
| Left / A | Hold for positive angular velocity (spin left), zero linear velocity. |
| Right / D | Hold for negative angular velocity (spin right), zero linear velocity. |
| Direction release | Immediately request zero. Remain armed only while the active control session is healthy and within its idle limit. |
| Stop motion / Space / Disarm | Request zero and disarm; clear all held input and require a new explicit Arm before moving again. No confirmation dialog. |
| Release control | Stop, disarm, invalidate the control lease, and relinquish ownership. |

On-screen movement uses press-and-hold: a quick click produces only a brief
request between press and release, possibly no visible movement. It never
latches motion after a click. Do not add a minimum movement duration to make a
quick click visible. Default web speeds are 0.20 m/s linear and 0.50 rad/s angular;
version 1 has no speed slider. Server configuration may reduce them, never
exceed the installed controller limits or accepted motor RPS caps.

Keyboard driving activates only after taking control and explicitly focusing
the drive panel. Use `KeyboardEvent.code` (`KeyW`, `KeyS`, `KeyA`, `KeyD`) and
keydown/keyup state; OS key-repeat is not required for continuous holding.
Ignore direction shortcuts in text inputs, editable content, dialogs, or with
Ctrl/Alt/Meta modifiers. Space has stop priority whenever this page has focus,
including its text fields; it cannot be a global shortcut in another window.
Prevent browser scrolling only for handled controls. No key held before Arm may
start motion: require release and a new press.

Exactly one direction/input source is valid at once. Multiple direction keys,
multiple touches, or mixed pointer/keyboard driving request zero and require
all inputs to be released before another direction can start. No diagonal or
combined commands in version 1. Direction reversal passes through zero.

Use pointer capture, and handle `pointerup`, `pointercancel`, and
`lostpointercapture`; cancel when the pointer leaves the drive button bounds.
Suppress compatibility click events that could duplicate a press. Use semantic
buttons and accessible focus styling; keyboard-only users drive with W/S/A/D.
[MDN documents the pointer event and capture lifecycle](https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events).

Window blur, hidden document, navigation, screen lock, or socket loss clears
input and requests stop/disarm. Returning to the page never resumes movement.
Lifecycle notifications are best effort; the Pi enforces expiry independently.
Hidden-page timers may be throttled, so they cannot supply safety timing.
[Page Visibility API behavior](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API).

## 3. Process architecture and ownership

```mermaid
flowchart TD
    B[Vue and TypeScript PWA: desktop or phone] -->|HTTPS and WSS| W[FastAPI and Uvicorn: API and static UI]
    W -->|Unix socket: bounded requests| O[Python operator agent: arbitration and leases]
    C[CLI teleop, arm and bench clients] -->|Same operator protocol| O
    O -->|Loopback DDS with SROS2| R[Controller: Twist to motor commands]
    R --> G[Motor guard]
    G --> S[Serial bridge: sole device owner]
    S --> H[STM32 and motors]
    O -->|Restricted Unix socket| L[Lifecycle helper: fixed service operations]
    W -->|Stop fallback and bounded status| L
    L --> M[systemd: mentorpi-tank.service]
```

### 3.1 Web service

New `mentorpi-tank-web.service`, non-root user `ubuntu-tank-web`, serves bundled
compiled Vue assets and an HTTPS JSON/WebSocket API. Use Python FastAPI with
Pydantic request/response models and one Uvicorn worker managed by systemd.
Disable development reload in production. Uvicorn terminates HTTPS using the
configured certificate; a separate proxy is not required for this deployment.
Lock and verify the native dependency closure before implementation is accepted.
[FastAPI features](https://fastapi.tiangolo.com/features/) and
[server deployment](https://fastapi.tiangolo.com/deployment/manually/).

Use strict validation for safety fields, including booleans, bounded numbers,
enums, and rejection of unexpected fields. Publish versioned OpenAPI for HTTP
routes and generate the frontend HTTP client/types from it. WebSocket messages
need explicit runtime validation and separate versioned JSON schemas and
TypeScript types; they are not automatically described by HTTP OpenAPI.
Serve API documentation assets locally, or disable the
interactive docs in production; no runtime CDN dependency.

The web process has no ROS credentials, serial access, general sudo permission,
or writable release files. It forwards explicit input; it must never regenerate
motion renewals from its last received direction. A stalled web event loop must
cause the independent operator agent to expire motion.

### 3.2 Operator agent

New `mentorpi-tank-operator.service`, a separate Python process under a non-root
dedicated user, owns the sole
authorized operator publisher to `/controller/cmd_vel` and guard arm client.
It uses the existing packaged loopback discovery profile and enforced security.
It has no serial access and retains localhost-only network confinement.
The controller/guard/bridge path and its independent watchdogs remain intact.

Share side-effect-free Python schemas and configuration utilities where useful.
The web process must not import modules that initialize ROS, open hardware, or
start publishers. All control requests cross the authenticated Unix socket;
using the same language does not remove this process boundary.

Use a bounded, versioned Unix-socket protocol with peer credential checks for
the web service and authorized local operator accounts. The agent owns all
monotonic deadlines, command mapping, fresh preflight, control arbitration,
periodic ROS publishing, and state reporting. Socket handlers and ROS service
calls must not block its deadline checks. No nonzero publisher may continue
running in another thread from a cached command without checking its deadline.

Migrate CLI teleop, arm/disarm, and bench command submission to this same agent;
read-only clients may retain their status enclave. This is required integration
work, not an assumption that the existing CLI already serializes publishers.
Provision operator keys for the agent only and retire legacy direct-publisher
credentials when web control is enabled. SROS2 policy and actual key filesystem
access must agree. Preserve bench correlation and terminating-zero observations.
Root/owner intervention remains trusted, as in the native controller design.

One connection holds control, including across browsers, tabs, CLI teleop, and
bench. No silent takeover. Handoff requires stop/disarm confirmation or service
stop before granting another owner. Any connected client may request
Stop/Disarm even if another tab owns driving. Stop invalidates the old motion
generation before replying so subsequent queued commands cannot restart it.

### 3.3 Lifecycle helper

A small root-owned helper exposes only fixed `start`, `stop`, `status`, and
bounded recent-log operations for `mentorpi-tank.service` over a protected Unix
socket. Use fixed argument arrays and sanitized environment, never shell text,
caller-selected unit names, file paths, or arbitrary arguments. Check peer
credentials and request sizes. The web peer may request stop/status/logs directly
when the operator agent is unavailable; start requires agent coordination.

Extract and reuse installed native preflight/lifecycle logic instead of invoking
a mutable checkout's `deploy.sh` or blindly calling `systemctl start` without
preflight. Serialize starts and release changes under the deployment lock.
Activation must revoke control and stop the graph before switching releases;
reject starts while activation/recovery is pending. Stop must not wait behind
a long build/deployment lock or an unavailable ROS response. Define lock ordering
and bounded cancellation explicitly in implementation tests.

The web and operator services remain available with the motion service stopped;
they must not have a dependency that starts the motor service just to display
the page. Starting any new service leaves ownership empty and motion disabled.
Lifecycle operation completion means observed systemd state, not subprocess
launch success. Report stop failure as unconfirmed, never as physical rest.

Administrative boot and maintenance may group `mentorpi-tank.service` and
`mentorpi-tank-operator.service` under `mentorpi-tank-stack.target` for a single
start/stop command. This grouping does not merge process identities or replace
independent service control: web lifecycle operations still start and stop only
the motion service, allowing the operator and web status paths to remain up while
the controller is unavailable.

### 3.4 Vue frontend and PWA lifecycle

Use Vue 3 single-file components, TypeScript, Vite, and `vite-plugin-pwa`.
Start with one responsive page and simple component/composable state; add routing
or a state-management library only when screens require it. Separate input
handling, API transport, telemetry, and rendering. Keep one connection/input
controller per active page and clean it up on component unmount.
[Vue introduction](https://vuejs.org/guide/introduction.html) and
[Vite PWA guide](https://vite-pwa-org.netlify.app/guide/).

Build on the workstation/build host, or on the Pi 5 with Node.js and npm installed
as build-time dependencies, and package the static output with the backend.
The Pi serves those files without a Node.js server or frontend build tools at
runtime. A web app manifest defines the name, icons, start URL, scope,
and standalone display. Use the same stable HTTPS origin for installation,
API calls and WSS; document certificate trust on phones.

The service worker caches only versioned interface assets. Exclude `/api/`,
telemetry and control requests from caching and background
sync; never persist or replay commands. The interface can open while the Pi is
unreachable, but must show disconnected status with driving disabled and no
cached battery/armed state presented as live. Internet access is unnecessary
when the phone can reach the Pi over the local network.

Use an explicit update prompt, not automatic page reload or forced service-worker
activation during control. Stop/disarm and relinquish ownership before accepting
an update. Every freshly loaded or resumed client checks backend protocol and
release compatibility before acquiring control; an incompatible cached client
remains unable to arm. Backend checks must reject incompatible clients too.
Rollback must work even when a phone retains newer cached assets: incompatible
clients can reload the installed UI, but cannot regain control through stale
sessions. Network-only recovery/version endpoints remain reachable outside the
cached app shell.

The service worker is not a motion publisher or a timer for renewing leases.
Phone lock, app switching, suspension, and loss of focus follow the existing
stop/expiry rules. Resume requires fresh state and explicit control acquisition
and arm. Validate installation and lifecycle behavior on actual Android/iOS
devices; desktop emulation alone does not establish mobile acceptance.
[Service worker lifecycle and HTTPS requirements](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API).

## 4. Motion lease and state contract

Application states are `NO_OWNER`, `OWNED_DISARMED`, `ARMING`, `ARMED_IDLE`,
`DRIVING`, and `FAULT`. Service state and telemetry freshness are separate fields.
Only `ARMED_IDLE`/`DRIVING` accept nonzero intent. Any fault, control-lease expiry,
service restart, agent restart, or changed release invalidates the control epoch
and requires explicit acquisition/arming as appropriate.

1. Acquire ownership while disarmed. Resolve one installed release and prepare
   ROS discovery/subscriptions before enabling Arm.
2. Arm only with fresh guard/bridge health, valid battery and hardware preflight,
   no conflicting stack or operator, and neutral browser input. Reuse native
   preflight thresholds; missing or stale information blocks arming.
3. After successful arm, submit fresh zero immediately within the existing
   250 ms first-command deadline. Confirm guard state and downstream zero
   delivery before enabling direction controls. Timeout or a late arm response
   triggers compensating disarm; never trust a timed-out arm request to have
   had no effect. Repeated Arm cannot renew a deadline.
4. Publish at 20 Hz only from currently valid agent state. Active sessions send
   explicit neutral intent while idle; zero publishing ends with disarm after
   30 seconds without directional input. Status polling does not count as input.
5. A motion/session lease lasts at most 150 ms, checked at least every 20 ms.
   Expiry immediately clears nonzero state, requests repeated zero/disarm, and
   invalidates that epoch. The existing 250 ms guard/bridge freshness behavior
   remains an independent fallback if the agent stalls or crashes.

To bound delayed commands, the agent issues single-use challenges every 50 ms
with an unpredictable token, current epoch, and a deadline 150 ms after issuance
on the Pi monotonic clock. The focused browser returns its current neutral or
held direction with the challenge and increasing sequence number. A response
cannot extend the lease past that challenge's original deadline. Reject expired,
reused, out-of-order, wrong-owner, or old-epoch responses; browser wall clocks
are never authoritative. Once expired, an epoch cannot be revived by late input.
The web layer only relays these challenges and responses.

Use latest-intent handling with bounded queues, not queued movement playback.
Stop has priority over nonzero intent and invalidates outstanding challenges.
Allow at most one unsent intent per connection; close a congested control socket
and expire ownership instead of draining buffered motion later. The browser
WebSocket API lacks automatic backpressure, so queue limits must be explicit.
[WebSocket API constraints](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket).

As a version-1 additional limit, cap a continuous hold at 5 seconds, then stop
and disarm. A new explicit arm and fresh press are required. Browser code cannot
prove that a human is still physically holding a key when input events are lost;
this cap limits that failure without treating a network heartbeat as proof of
human intent. Do not extend any safety deadline to hide poor Wi-Fi performance.

An explicit stop requests zero without waiting for the next publication tick.
Healthy-agent zero submission target is within 20 ms of receipt or lease expiry;
150 ms plus a 20 ms scheduling check is a software target, not a measured physical
stop guarantee. Browser-to-Pi delay and actual track deceleration are separately
measured. Space is a software stop, not a replacement for the physical disconnect.

## 5. Network access and API

This is a personal, single-owner tank on a trusted local network. The web UI
and API require no user authentication: no login/logout, passwords, accounts,
API keys, bearer tokens, authentication cookies, or login-session store.
Any client that can reach the configured interface can view status and request
control. Take control selects one active connection; it does not identify a user.

Default deployment is HTTPS on a configured LAN address, port 8443; advertise
the actual configured URL in CLI status. Provision an owner-trusted server
certificate for browser/PWA support; no client certificate is required. Serve
all assets locally for operation without Internet access.

Validate the exact Host/Origin on HTTP and WebSocket browser requests; reject
cross-origin mutations and missing-Origin browser controls. Use a restrictive
same-origin CSP, deny framing, and render logs as text. Bound connections,
input sizes, and log responses; control expiry runs independently of request
load. These browser safeguards do not authenticate users. Control ownership,
challenge tokens, epochs, explicit arming, and stop deadlines remain motion
safety mechanisms, with immediate invalidation on connection loss or lease expiry.
Existing native SROS2 and local process-identity checks remain internal controller
safeguards; they add no web login or user-authentication workflow.

| Proposed API | Input, result, and side effect |
| --- | --- |
| `GET /api/v1/status` | Service/agent/guard status, freshness, battery, owner, limits, release ID, and last fault; never arms. |
| `GET /api/v1/version` | Network-only protocol compatibility and release identity; no robot state or credentials. Used before control and to recover incompatible cached clients. |
| `GET /api/v1/logs?limit=N` | Recent controller/web/agent logs, maximum 200 lines and 64 KiB; no arbitrary journal filters. |
| `POST /api/v1/controller/start`, `/stop` | Request ID; return operation ID and pending/completed/error state. Stop invalidates driving first. |
| `POST /api/v1/control/acquire`, `/release` | Connection-bound ownership; acquisition returns epoch or busy, release stops/disarms. |
| `POST /api/v1/control/arm` | Epoch, request ID, exact boolean `tracks_raised: true`; returns pending then confirmed or failed. |
| `POST /api/v1/control/stop` | Stop independent of ownership; immediate invalidation and asynchronous zero/disarm confirmation. |
| `GET /api/v1/operations/{id}` | Status of a bounded retained operation; timeouts/errors explicit. |
| `WSS /api/v1/control` | Control connection binding, agent challenges, enumerated direction/neutral intent, stop, acknowledgments, and live state. |

All mutation requests are bounded JSON with schema version and request ID.
Directions are enums, not arbitrary velocities, ROS topics, or service names.
Reject unexpected fields, invalid booleans/numbers, and oversized frames before
changing state. Use stable errors such as `NOT_OWNER`, `LEASE_EXPIRED`,
`PREFLIGHT_FAILED`, `CONTROLLER_UNAVAILABLE`, and `DEPLOYMENT_BUSY`.
Retries of the same operation ID must not repeat an arm or revive motion.
Never automatically retry nonzero intent or arm after reconnection.

Display service running, guard armed, requested velocity, and observed delivery
as distinct facts. Timestamp telemetry on the Pi and expose its age; historical
transient-local ROS state alone cannot establish current health. Stale telemetry
disables arming/driving and triggers disarm. Define per-topic freshness thresholds
from actual publisher rates during M10. Do not label a successful serial write
as physical movement or show a stopped icon solely because zero was requested.

## 6. Files, installation, and rollback

Proposed source layout:

```text
ubuntu_tank/
  web/src/                        Vue components, TypeScript, generated API types
  web/public/                     PWA icons and other public static assets
  web/package.json                frontend build/test dependencies
  web/package-lock.json           locked frontend dependency graph
  web/vite.config.ts              Vite and PWA cache/update configuration
  web/dist/                       generated static output; ignored, packaged
  src/ubuntu_tank_web/             FastAPI API, static serving and socket client
  src/ubuntu_tank_operator/        ROS agent, leases, arbitration, schemas
  host/mentorpi-tank-web.service
  host/mentorpi-tank-operator.service
  host/mentorpi-tank-lifecycle.*    restricted helper/socket assets
  config/web/                     documented non-secret defaults
  tests/                          protocol, browser, deployment and fault tests
```

Install code with the same immutable release under `/opt/ubuntu_tank`;
configuration and TLS material below `/etc/opt/ubuntu_tank/web`, ephemeral sockets
and sessions below `/run/ubuntu_tank-web`, and bounded persistent diagnostics
below `/var/opt/ubuntu_tank/web`. Split file ownership so only the web service
reads its TLS material and only the agent reads operator ROS keys.
Neither new non-root account joins the serial-access group.

Document `listen_address`, `port`, certificate/key paths, allowed origin,
web speed caps, lease/publication periods,
idle timeout, and maximum hold duration. Reject inconsistent configurations at
startup; browser requests cannot override timing or speed limits. Bind safely
to loopback until LAN address and TLS are configured.

Extend existing package/install/activate/rollback transactions to include new
units, schemas, credentials permissions, and SROS2 migration. Activation stops
all command producers and invalidates sessions; mixed release/protocol versions
fail closed. Never hot-reload a driving process. Rollback to a native-only release
disables the web/agent units and restores matching native CLI/security assets,
leaving the controller stopped and disarmed. Preserve credentials separately
from shipped artifacts and omit secrets from reports.

Workstation Python tooling stays in the repository `.venv`. Target/build-root
Python, FastAPI, Uvicorn, Pydantic, and their transitive dependencies must be
apt/ROS-managed and added to the locked closure. Verify compatible ARM64 packages
in M10; unresolved packaging is a gate, not permission for target pip installs
or nested virtual environments. Lock build-host Node.js/npm and frontend package
versions; use `npm ci`, TypeScript checking, tests, and a production Vite build.
Record frontend and backend identity in the release manifest and include only
required static output in the installed frontend payload, not `node_modules`.
Browser-test tooling is development-only and version-locked. All new commands
and deployment flags are proposed until implemented and documented in README.

## 7. Trackable implementation milestones

Continue numbering after native Milestone 9. Unchecked boxes identify pending
work. Complete M11.1 before continuing with M12; M12 and M13 can then proceed
independently, with M14 integrating both.

### Milestone 10 — Protocol, state machine, and dependency closure

- [x] Define versioned schemas, state transitions, monotonic challenge deadlines,
  error codes, telemetry freshness, configuration validation, and lock ordering.
- [x] Lock native web dependencies and document exact release/API compatibility.
- [x] Verify FastAPI/Uvicorn/Pydantic ARM64 apt closure; lock Vue/TypeScript/Vite
  and PWA build dependencies and define HTTP/OpenAPI and WebSocket schemas.
- [x] Add deterministic tests for expiry, late/replayed intent, stop priority,
  arm cancellation, stale state, idle timeout, and the continuous-hold cap.

Exit: hardware-free tests prove no stale input can create or revive motion.
Status: Completed. Hardware-free deterministic test suite passes 100% across all
monotonic challenge lease, 5.0 s continuous hold, 30.0 s idle timeout, stop priority,
arm cancellation, and telemetry freshness gates.

### Milestone 11 — Shared operator agent and CLI integration

- [x] Implement the loopback-only ROS operator agent and credential-checked IPC.
- [x] Route teleop, arm/disarm, and bench through exclusive ownership; migrate
  SROS2 permissions/key access and preserve read-only diagnostics.
- [x] Verify first-command zero, delivery correlation, fault recovery, agent
  crash behavior, and rejection of competing direct publishers.

Exit: native middleware tests show exactly one command authority and working CLI
regressions. Hardware-free evidence remains distinct from target-Pi evidence.
Status: Completed. Verified loopback Fast DDS operator agent with Unix domain
socket IPC and SO_PEERCRED UID/PID verification, newline-delimited JSON framing
capped at 64 KiB, exclusive single-operator arbitration returning DEPLOYMENT_BUSY
to competing callers, fail-closed disconnect handling (disconnect of owner
automatically stops and disarms), stop priority (any connected client can stop
and disarm), 250 ms first-command zero deadline, single-use monotonic challenge-intent
leases, and rolling 5-stage delivery observation buffer. CLI integrations
(operator_client, teleop_key_node, bench_client) routed through agent IPC with
fail-closed defaults; explicit direct ROS mode holds the same provisioned
cross-user authority lock for the publisher lifetime. Arm success requires a
fresh complete downstream zero write within the original deadline, and deadline
validation samples monotonic time after lock acquisition and again before motion
publication. Hardened systemd service mentorpi-tank-operator.service configured.
Comprehensive test suite passes 100% (66/66 tests in test_milestone11_operator_agent.py).

### Milestone 11.1 — Behavior-based test cleanup

Scope: change tests and test-only fixtures or harnesses only. Do not modify
production source, handwritten runtime configuration, packaging, service units,
deployment commands, or installed behavior. If authentic behavioral validation
would require a production change, record that validation as pending instead.

- [x] Inventory Python and shell tests that read handwritten source, systemd,
  udev, tmpfiles, YAML, XML, JSON, TOML, or other static configuration merely to
  assert literal strings, syntax fragments, or repeated parsed values.
- [x] Replace source-code introspection with tests through the affected public
  API, executable, or process boundary, asserting observable behavior and failure
  handling rather than implementation text.
- [x] Exercise handwritten configuration through its real consumer or an official
  parser, including systemd, tmpfiles, ROS launch/security, and application
  configuration surfaces. Assert the resulting behavior, permissions, lifecycle,
  or rejection instead of restating file contents.
- [x] Retain content-level assertions only when code under test generates the
  artifact and its generated content is the behavioral contract. Remove obsolete
  or duplicate assertions without weakening the safety invariant they represented.
- [x] Record target-only validation as pending when the authentic consumer is not
  available hardware-free; do not replace unavailable runtime evidence with a
  source-text assertion. Run the full hardware-free suite and boundary gates after
  the cleanup, with no intended production behavior change.

Exit: no test treats literal text in handwritten source or configuration as a
substitute for behavior. Every retained safety claim is exercised through a real
consumer or explicitly recorded as pending, and all hardware-free gates pass.

### Milestone 12 — Web API and service lifecycle

- [x] Remove M10 login/logout schemas and exports, authentication error codes,
  credential-file/login-session configuration, and matching OpenAPI generator,
  generated specification, and TypeScript types. Retain connection identifiers,
  motion challenges, and leases for control arbitration; they are not login sessions.
- [x] Implement TLS provisioning, same-origin controls, bounded HTTP/WSS,
  agent challenge relay, status, logs, and operation results.
- [x] Implement FastAPI strict models, OpenAPI export, WebSocket validation,
  compatibility/version endpoint, and the single-worker Uvicorn systemd service.
- [x] Implement the narrow lifecycle helper and start/stop deployment interlocks.
- [x] Test cross-origin requests, oversized messages, request
  floods, frozen web process, and Stop when the agent or ROS is unavailable.

Exit: the page manages a mocked service without login or root web privileges;
only the active control owner can arm/drive, any client can stop, and queue
congestion cannot prolong an agent lease.

### Milestone 13 — Vue browser and PWA driving interface

- [x] Implement the page, hold buttons, W/S/A/D, Space, acknowledgments, status,
  ownership display, responsive layout, and accessible focus behavior.
- [x] Automate browser tests for key release, focus loss, hidden tabs, pointer
  cancellation, mixed inputs, held keys across Arm, delayed messages, multiple
  tabs, connection loss, and reconnection with no automatic resume.
- [x] Verify fresh neutral intent while idle and immediate local input clearing.
- [x] Build Vue/TypeScript components and the generated HTTP API client; add
  manifest/icons, asset-only caching, explicit updates, and offline status.
- [x] Test that offline requests cannot queue/replay movement, updates require
  disarm, and stale or incompatible cached clients cannot arm.

Exit: real browser tests against a mocked agent satisfy every interaction in §2.

### Milestone 14 — Installed Pi integration and rollback

- [x] Package all services/assets; verify ARM64 imports, dependency closure,
  ownership, TLS trust setup, and production systemd confinement on the Pi.
- [x] Verify installed native DDS delivery without motor power, CLI/browser
  exclusion, web availability while the controller is stopped, and safe restart.
- [x] Test activation, interrupted activation, incompatible protocols, and
  rollback to native-only releases without retained sessions or motion.
- [x] Verify packaged static frontend delivery without Node.js at runtime,
  phone-trusted HTTPS, PWA installation, and cached-client recovery on rollback.

Exit: reproducible target-Pi installation and recovery pass; motion gates remain
blocked until raised-track testing is explicitly undertaken.

### Milestone 14.1 — Real Pi 5 installation and deployment integration tests

Implement and run a dedicated integration suite on a real ARM64 Pi 5 running
the supported native Ubuntu image. **Always build and install before running
integration tests.** The owner or deployment pipeline runs the complete production
build, packaging, installation, and activation workflow first; the integration
test then verifies the resulting installed system. It must not perform an implicit
initial build or installation to satisfy missing prerequisites. Organize tests
around complete workflows and their final state, not individual scripts. Expose
verification through a separate, explicit target-test command, outside the default
development-machine test run. Lifecycle/recovery scenarios can change installed
files and services, but their verification likewise follows the corresponding
completed deployment or recovery operation. Keep motor power off
and motion disarmed throughout; movement testing remains Milestone 15.

Required sequence:

1. Prepare the host and dependencies using the production setup commands, including
   any required reboot.
2. Build the frontend from the checked-out source:

   ```bash
   cd ubuntu_tank/web
   npm ci
   npm run build
   ```

   This generates `ubuntu_tank/web/dist/`; keep it ignored by Git. Run the build
   on the Pi 5 or transfer the generated output from the build computer to the
   packaging workspace. Node.js/npm are build-time requirements only.
3. Build the native ROS workspace and package the complete release, including
   the generated frontend assets. Install and activate that release on the Pi 5
   using the production deployment commands. Stop if any step fails.
4. Run integration verification against that installed, active release. Check
   release identity, manifests, and installed frontend assets before service
   tests. Missing or stale build/install prerequisites must produce an actionable
   failure, not trigger a build, installation, or a misleading pass. Merely building
   `web/dist/` in the checkout does not update an already installed release.

After installation, run from the repository root:

```bash
sudo ./ubuntu_tank/deploy.sh target-test --expected-release-id YOUR_INSTALLED_RELEASE_ID
```

The command never builds or performs an initial installation. Normal runs verify
the installed system and service lifecycle, leaving motion stopped/disarmed.
`--expected-release-id` rejects an unintended or stale active release; omit it
only when intentionally verifying whichever release is currently active.

Repeat-install, upgrade, recovery and offline rollback scenarios are opt-in:
add `--deployment-scenarios --release-archive /absolute/path/current.tar.zst
--upgrade-release-archive /absolute/path/upgrade.tar.zst
--native-release-archive /absolute/path/native-release.tar.zst` to that command.
All archives must be built beforehand. The repeat-install archive must match the
active release; the upgrade archive must be web-enabled with a different release
ID. The native archive must be a genuinely production-built ARM64 release (including
the ROS workspace and SROS2 policy tooling, without web/lifecycle units). Build
that archive from the native-only milestone using its production packaging
workflow; do not construct a synthetic install tree. The suite installs and
activates that baseline, activates the web release, then uses production rollback
to verify the native result. The archive must use the target's release prefix.
For these optional scenarios, the operator login defaults to `SUDO_USER`; direct
root invocation requires `--operator-user LOGIN`. Archive paths and the login
are checked before host mutation, except
that archive integrity and production build provenance are validated by the
production installer. `--inspect-only` does not require the archive or login.
Cleanup confirms services are stopped before restoring baseline files and treats
shutdown or systemd reload failure as a failed run. Real target execution remains
required; passing development tests does not verify Pi installation.

- [x] Separate production build/installation from integration verification using the documented
  production entrypoints: host preflight and preparation, dependency setup,
  build, packaging, installation, and activation, including required reboots.
  Require successful completion before starting integration verification; remove
  automatic initial setup/build/install from the integration-test entrypoint.
  Let the production entrypoints invoke their helpers normally; do not create a separate
  test for each script or installation step. Preserve production safety checks
  and stop the workflow if a required step fails.
- [x] Implement target checks and a reproducible setup procedure for a clean
  Pi image with a recoverable baseline. Use real apt/dpkg, ROS, systemd, udev,
  users/groups, and production paths; do not replace them with mocked commands,
  synthetic install trees, or a development-machine imitation of the Pi.
- [x] After the whole installation/deployment workflow completes, run a
  comprehensive verification phase covering every installed configuration artifact:
  package sources and locked versions, users/groups, device rules, runtime
  directories, service units and enablement, environment files, controller and
  web settings, DDS/SROS2 configuration and credentials, TLS files, release
  manifests, frontend assets, and the active-release link.
- [x] Check installed file locations, ownership, permissions, preserved user
  settings, and generated values against the installation inputs and deployment
  contract. Load configuration through its actual consumer and verify effective
  service settings, ARM64 imports, protected IPC, native DDS communication,
  HTTPS/static delivery, and status/log access while the controller is stopped.
  Reading checked-in configuration text alone is not installation verification.
- [x] Exercise fresh installation, repeat installation, upgrade, activation,
  controlled interruption and recovery, and offline rollback to web-enabled and
  native-only baselines. Verify configuration preservation, legacy snapshots,
  removal of obsolete installed files, and stopped/disarmed state after recovery.
  Run each as a complete workflow on the recoverable target, followed by checks
  of the resulting system state, with documented reset steps. For an interrupted
  workflow, verify the recovered state after recovery completes.
- [x] Record the exact release and starting image, invoked commands, exit codes,
  installed-state checks, logs, and recovery outcome. Missing target services or
  unexecuted cases remain pending; a mock or development-machine pass cannot
  complete this milestone.

Exit: the complete installation/deployment workflow and recovery scenarios have
reproducible real-Pi execution evidence, and comprehensive post-run verification
confirms that all required files and configuration are in place and work through their
real consumers. The Pi can be restored to the recorded baseline without enabling
motion. These results supply the target evidence required by Milestone 14.

#### Installed configuration and generated-file verification checklist

Verify this checklist after the complete workflow, not through separate tests of
each producing script. Paths below use the default production layout;
`<release>` is `/opt/ubuntu_tank/releases/<release-id>` and `<keystore>` is
`/etc/opt/ubuntu_tank/security/keystore`. Resolve configured overrides and symlink
targets from the actual installation. Copied configuration is included because
its installed destination must be verified even when its contents were not
generated. Do not log private keys or credential contents.

| Installed configuration / output | Producer | Required post-run verification |
| --- | --- | --- |
| `/etc/opt/ubuntu_tank/controller.yaml` | `deployment_manager.py` installation; copied from release defaults only when missing | Real controller configuration loader accepts it; calibration and safety limits match installation inputs, and existing owner settings survive reinstall/upgrade. |
| `/etc/opt/ubuntu_tank/mentorpi-tank.env` | `deployment_manager.py` installation and `migrate_host_env()` during activation | Systemd and launchers load the intended ROS/DDS/security environment; profile, keystore, log and controller-config paths resolve; unrelated owner settings survive migration. |
| `/etc/opt/ubuntu_tank/web/web.yaml` | `deployment_manager.py` installation; copied only when missing | Web configuration loader accepts listen address/port, origins, TLS paths, IPC paths and motion limits; actual HTTPS access works at the configured address and existing settings are preserved. |
| `/etc/systemd/system/mentorpi-tank.service`, `mentorpi-tank-operator.service`, `mentorpi-tank-web.service`, `mentorpi-tank-lifecycle.service`, `mentorpi-tank-recover.service`, `mentorpi-tank-stack.target` (all in that directory) | `deployment_manager.py` activation copies release units | Systemd loads the installed units without errors, resolves executable/environment paths, and applies the intended users, groups, dependencies and restrictions. Check actual start/stop and recovery behavior with motor power off. Check enablement against the documented setup procedure; a unit's `[Install]` section alone does not enable it. |
| `/etc/udev/rules.d/99-mentorpi-rrc.rules` | `deployment_manager.py` merges candidate rule with the selected device identity | Rule retains the selected serial or USB-port discriminator; real udev binds the intended device as `/dev/rrc` with the required group/access. Verify no ambiguous device selection. |
| `/etc/tmpfiles.d/ubuntu-tank.conf` | `deployment_manager.py` copies release tmpfiles configuration and invokes `systemd-tmpfiles` | Real tmpfiles processing creates the required runtime/state paths with effective owner/group/mode, including after reboot. Verify the resulting filesystem, not just directives. |
| `<release>/config/controller.yaml`, `config/web/web.yaml`, `config/fastdds/loopback.xml` | `deployment_manager.py` packaging copies configuration; installation extracts it | Manifest integrity passes; defaults are available; active Fast DDS profile is accepted and real native DDS traffic stays on the configured loopback transport. Distinguish release defaults from mutable host configuration above. |
| `<release>/config/sros2/governance.xml`, `policies.xml`, `permissions/{controller,guard,bridge,operator,status}_permissions.xml`, `schemas/omg_shared_ca_governance.xsd`, `schemas/omg_shared_ca_permissions.xsd` | Packaging copies SROS2 policy inputs and schemas | Installed policy validation succeeds; generated signed grants correspond to the selected release and real secured participants communicate as intended. |
| `<keystore>/identity_ca.key.pem`, `identity_ca.cert.pem`, `permissions_ca.key.pem`, `permissions_ca.cert.pem`, `governance.xml`, `governance.p7s` | `deployment_manager.py` `_provision_sros2_keystore()` generates/preserves CA identities and signs the policy | Validate certificate/key pairs and signed governance, owner/mode and service access; preserve existing identities across upgrades. Resolve the live keystore link to a complete published generation. |
| `<keystore>/enclaves/ubuntu_tank/<role>/{key.pem,cert.pem,identity_ca.cert.pem,permissions_ca.cert.pem,governance.p7s,permissions.p7s}` for each of `controller`, `guard`, `bridge`, `operator`, `status` | Same SROS2 provisioning function | Every enclave has its full file set; certificates and signatures validate; intended service roles can read their credentials and start with security enforcement. Temporary `request.csr` files are removed after successful generation. |
| `/var/opt/ubuntu_tank/web/certs/server.crt`, `server.key` (or the paths configured in `web.yaml`) | Web entrypoint calls `ubuntu_tank_web/tls.py` on startup; not created by package installation alone | After the explicit web-service startup phase, verify readable certificate, matching protected private key, validity and configured hostname/IP coverage, and browser HTTPS trust. Preserve valid owner-provided credentials. |
| `/etc/ros/rosdep/sources.list.d/10-ubuntu-tank.list` | `install_ros2.sh install-deps` generates it | Rosdep consumes the local verified source files; obsolete unpinned `20-default.list` is absent after this workflow. |
| `/var/opt/ubuntu_tank/rosdep_sources/index-v4.yaml`, `base.yaml`, `python.yaml`, `ruby.yaml` | `install_ros2.sh install-deps` downloads and verifies pinned inputs | Files match the lock's hashes and dependency resolution succeeds. Verify the effective `ROSDISTRO_INDEX_URL` used by the command separately; downloading `index-v4.yaml` does not prove the resolver used that local file. |
| System locale configuration affected by `locale-gen` / `update-locale` (`/etc/locale.gen` where used; `/etc/default/locale` or the target package's actual locale-config destination) | `install_ros2.sh prepare-host` invokes distribution locale tools | Record the files actually modified by the installed tools; a fresh process has the requested UTF-8 locale and the generated locale is available. Do not assume every listed distribution-dependent path exists. |
| Ubuntu APT source files actually changed under `/etc/apt/sources.list` and `/etc/apt/sources.list.d/`; ROS source definitions and signing key files installed by `ros2-apt-source` | `prepare-host` invokes `add-apt-repository`; `install-ros` installs the pinned repository package | Enumerate actual source/key paths through the target package inventory (`dpkg-query -L ros2-apt-source`) and resolved links, including package-owned files outside `/etc`. Verify enabled repositories, signature-key references, and locked package versions through real APT/dpkg. Do not invent a fixed ROS source filename that the script does not specify. |
| Account databases affected by provisioning: `/etc/passwd`, `/etc/group`, and associated `/etc/shadow` / `/etc/gshadow` entries managed by the OS tools | `deployment_manager.py` invokes account/group management tools | Verify accounts and membership through NSS (`getent`/`id`), including controller, operator, web, hardware-access, operator-access and status-access roles and the selected human operator. Do not compare entire host databases or dump password hashes. |

Also verify the following generated state and supporting artifacts. These are
not all configuration files, but a correct installation depends on them:

| Output | Producer | Required post-run verification |
| --- | --- | --- |
| `/var/opt/ubuntu_tank/deployment/host-baseline-pre.txt`, `host-baseline-post.txt`, `host-baseline.txt` | `install_ros2.sh prepare-host` | Accepted baseline matches the running target after required reboot; a pending-reboot snapshot must not be mistaken for an accepted baseline. |
| `/var/opt/ubuntu_tank/deployment/activation-journal` | `deployment_manager.py` | Selected release and transaction state agree with the installed system; successful completion has no unresolved transaction. |
| `/var/opt/ubuntu_tank/deployment/snapshots/<tx-id>/{symlink_target,metadata.json,checksums.sha256,security.json}`, backed-up configuration/unit/rule files, and `keystore/` when present | `SnapshotManager` during activation | Verify checksums, tracked/present-file metadata, recorded release, credential ownership, and actual restoration. Conditional backups include `controller.yaml`, `mentorpi-tank.env`, `web.yaml`, the six systemd units/target above, and `99-mentorpi-rrc.rules`; absence is valid only when the saved baseline lacked that file. |
| `<release>/release-manifest.txt`, `<release>/install/production-build.json`, generated `install/setup.*`, `install/local_setup.*`, package environment hooks and installed launch/configuration files | Colcon/build scripts generate install tree and provenance; packaging generates manifest | Enumerate the full generated install tree from the manifest/provenance rather than hard-coding every colcon filename. Verify hashes, ARM64 imports, correct production prefix, non-synthetic build provenance, and no dependency on the checkout/build root. |
| `<release>/web/dist/index.html`, `manifest.webmanifest`, `sw.js`, and all emitted asset/service-worker files | Frontend build produces assets; deployment packaging copies `web/dist` | Every emitted file is included in the manifest and served correctly by the installed web service; target serving requires no Node.js runtime. |
| `/opt/ubuntu_tank/current` and `/etc/opt/ubuntu_tank/security/keystore` symlinks | `deployment_manager.py` publishes release/security generations | Links resolve to the intended complete release and credential generation; activation, rollback and recovery leave them consistent. |
| `/opt/ubuntu_tank/libexec/recover-activation`, `deployment_manager.py`, `config_migration.py` | Installation generates the recovery launcher and copies its helper modules | Installed recovery runs independently of the checkout and selected release, with correct executable/read permissions. |
| `/run/ubuntu_tank/`, `/run/ubuntu_tank-web/`, `/run/lock/ubuntu_tank/`, deployment/operator lock files, configured IPC sockets, and `/var/opt/ubuntu_tank/{deployment,ros-log,operator-log,web}` | Provisioning/tmpfiles create paths; running services create sockets and other runtime files | Verify live ownership and access, lock coordination and socket connectivity in the appropriate service phase; verify reboot recreation. Do not require service-owned sockets while their service is stopped. |

For the build phase, additionally record the actual disposable-root destination
and verify `.ubuntu-tank-build-root.json`, its generated `etc/passwd`, `etc/group`,
`etc/machine-id`, copied `var/lib/dpkg/status`, generated `var/lib/dpkg/arch`, and
the host configuration copied by `prepare_build_root.py`: `etc/os-release`,
`alternatives/`, `ld.so.cache`, `ld.so.conf`, `ld.so.conf.d/`, `nsswitch.conf`,
`localtime`, `timezone`, `locale.alias`, `default/locale`, `python3/`, and
`python3.14/`, where present on the source host. These are build-root artifacts,
not additional production-host configuration. Use the provenance and resulting
real build to establish correctness; do not create a simulated root for this test.

Record package-tool outputs whose filenames depend on the installed package
version from the actual target's package inventory. Temporary downloads, candidate
closure manifests under `/tmp`, APT transaction caches, retired credential
generations, and ROS/rosdep caches are not mandatory persistent configuration;
check their documented cleanup or failure-preservation behavior when applicable.
Extend this inventory if the scripts gain new outputs. Every applicable checklist
entry must have a verification result or an explicit pending/failure reason;
mere file existence is insufficient.

### Milestone 14.2 — Remove development-machine installation simulations

Remove tests that imitate a Pi installation on the development machine.
Milestone 14.1's end-to-end integration testing is the way to verify installation
and deployment; do not port or replace individual simulation tests with target
test cases. Preserve existing milestone numbers and retain tests of runtime
product behavior.

- [ ] Verify each candidate in the inventory below against its current test body,
  setup/teardown, helper calls, and assertions before deleting it. Record a
  remove/retain/split decision and a short reason for each test method. Remove
  a case when it tests installation/deployment by substituting a Pi host, package
  manager, installed filesystem, service provisioning, or release workflow on
  the development machine. A mock, temporary directory, or deployment-related
  filename alone does not qualify a test for removal. Do not delete whole mixed
  files or classes without checking every method.
- [ ] Remove those simulation cases and fixtures used only by them. Split mixed
  test files as needed: retain robot control, motion safety, operator, web/API,
  and state-transition tests, along with reusable pure-function tests that do
  not pretend to validate an installed Pi environment.
- [ ] Update test runners and documentation so development tests exercise
  product behavior and the separate target suite verifies installation and
  deployment. Remove stale imports, test counts, and claims that development
  fixtures establish real-Pi installation correctness.
- [ ] Run the retained development tests to verify the cleanup preserves product
  and motion-safety coverage. Keep installation/deployment verification in the
  Milestone 14.1 integration suite, without requiring one-to-one replacements
  for removed tests.

Exit: development-machine installation/deployment simulations are removed;
Milestone 14.1 provides installation verification, and product and motion-safety
regression coverage remains intact.

#### Removal candidate inventory (code scan, 2026-09-19)

All paths below are relative to `ubuntu_tank/tests/`. These are concrete candidates
for developer verification, not permission to delete every listed class wholesale.
Class names identify all methods to inspect unless specific methods are listed.

| File | Candidate classes or methods | What currently substitutes for the installed Pi |
| --- | --- | --- |
| `test_install_workflow.py` | `TestHostPreflight`, `TestMutualExclusion` | Execute `check_host.sh` with `UBUNTU_TANK_MOCK_TARGET` and overridden architecture, OS, EEPROM, containers, or serial-device state. Inspect exceptions such as `test_non_mock_process_table_scan_clean` separately. |
| `test_install_workflow.py` | `TestDryRunCommands`, `TestAptRecoveryWorkflow`, `TestRebootSequence` | Installer dry runs, synthetic candidate manifests/reboot baselines, and fake `dpkg`/`apt-get` executables exercise host setup and recovery without installing on the Pi. |
| `test_install_workflow.py` | `TestDeploymentLock`; `TestSecurityAndCliGuards.test_reject_all_mock_and_override_variables_without_dry_run` | A temporary deployment lock and injected installer overrides exercise simulated setup interlocks. Verify the installation purpose of each assertion before removal. |
| `test_milestone3_port.py` | `TestInstallRos2ClosureManifest.test_generate_candidate_manifest_rejects_unreadable_artifact`, `test_generate_candidate_manifest_rejects_duplicate_package`, `test_generate_candidate_manifest_includes_unlocked_archives` | Source `install_ros2.sh` functions with fake `dpkg-deb`/`apt-cache` and dummy archives standing in for package installation inputs. Distinguish these from actual artifact-parser tests in the same class. |
| `test_milestone5_deployment.py` | `TestPackagingAndReleaseManifest`, `TestInstallationAndImmutability`, `TestAtomicActivationAndRollback`, `TestFaultInjectionAndBootRecovery` | `BaseDeploymentTestCase` builds temporary `/opt`, `/etc`, `/var`, `/run`, systemd and udev layouts; packaging uses `allow_staged_install=True`, and installation/activation use synthetic releases with target checks disabled. |
| `test_milestone5_deployment.py` | `TestReviewRemediations.test_p1_finding1_self_contained_recovery_outside_checkout`, `test_p1_finding2_service_identity_and_ownership_normalization`, `test_p1_finding4_recovery_and_rollback_reject_unverified_baseline` | Package/install/provision/recover releases inside the temporary host hierarchy. Other methods in this class cover runtime behavior and require separate decisions. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound2.test_finding1_interrupted_rollback_is_journaled_and_recoverable`, `test_finding2_retried_activation_reconciles_pending_transaction` | Fabricated releases, snapshots, and journal state simulate interrupted deployment and retry. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound3.test_finding1_packaged_tree_production_prefix_no_checkout_leak`, `test_finding2_interrupted_install_resumes_host_provisioning` | Synthetic build/install trees and interrupted provisioning simulate installed releases outside the checkout. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound4.test_finding3_remove_executable_trailing_eof_and_subprocess_package`, `test_finding4_build_inside_disposable_root_at_actual_prefix` | Builder/packager tests use development fixtures and simulated build roots rather than a native installed Pi. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound5.test_finding2_explicit_copy_rootfs_to_target_and_failed_build_rejection` | Constructs fake Ubuntu 26.04/ARM64 rootfs metadata, ROS setup, and build payloads to exercise packaging and failed-build recovery. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound6.test_finding1_mutual_exclusion_fails_closed_across_entrypoints`, `test_finding2_fresh_installation_provisions_valid_sros2_keystore`, `test_finding4_nounset_disabled_during_ros_sourcing` | Simulated host inventory, temporary installed credentials, and fixture ROS setup exercise installation/build behavior. Split any runtime safety checks from installation simulation. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound7.test_legacy_pem_store_repaired_without_rotating_identity`, `test_policy_activation_rollback_and_interruption`, `test_preflight_rejects_before_install_and_activation_mutations`, `test_live_preflight_checks_host_and_exclusion_and_rejects_overrides`, `test_role_permissions_and_operator_membership`, `test_incomplete_security_snapshot_rejected_before_restore` | Temporary credential stores, release activation/snapshots, mocked host preflight, and mocked account provisioning stand in for installed-host operations. Check whether any method is solely a reusable integrity validator before deleting it. |
| `test_milestone5_deployment.py` | `TestReviewFindingsRound8.test_wrong_prefix_and_changed_payload_are_rejected`, `test_conflicting_archive_rejected_identical_retry_preserved`, `test_development_tree_does_not_bypass_production_builder`, `test_empty_production_root_bootstraps_before_build`, `test_bootstrap_dry_run_and_destination_guards` | Fixture releases/build provenance, intercepted builder subprocesses, fake rootfs/bootstrap helpers, and dry-run bootstrap exercise deployment on the development machine. Separate pure integrity and destructive-path validation from installation simulation. |
| `test_milestone7_dds_correction.py` | `TestHostEnvironmentMigrationAndRollback` | Temporary host directories and constructed legacy/candidate releases exercise installation, migration, activation, rollback, and recovery. Inspect the two direct `migrate_host_env` methods separately as possible pure transformation tests. |
| `test_milestone11_operator_agent.py` | `TestDeploymentProvisioningAndLifecycle.test_service_identities_provisioning`, `test_deployment_manager_backup_and_restore_mappings` | Mocked root UID, passwd/group lookups and account-management subprocesses; temporary service files and snapshots simulate provisioning and rollback. |
| `test_milestone14_installed_integration.py` | `TestPackagingAndAssetClosure.test_package_includes_web_dist_and_manifest_checksums`, `TestTransactionalActivationAndRollbackClosure`, `TestSnapshotWebConfigPreservation` | `BaseMilestone14TestCase` supplies a temporary Pi directory hierarchy; synthetic native/web releases and snapshot mutations simulate installed packaging, activation, rollback, and configuration restoration. |

Inspect these neighboring tests too, but do not label them Pi installation
simulations solely because they live beside the candidates:

- `test_milestone5_deployment.py`: `TestSystemdUnitAndConfinementDirectives`
  mixes isolated systemd/tmpfiles roots with actual consumer checks and
  `test_target_udev_rule_creates_restricted_rrc_device`, which checks a real
  connected target device. Remove only methods verified to imitate installation;
  retain or place genuine target checks with the target suite. Likewise inspect
  `TestDeploymentLockContention` and `TestNonInteractiveRunner` by purpose.
- `test_milestone11_operator_agent.py`:
  `TestDeploymentProvisioningAndLifecycle.test_tmpfiles_recreation_preserves_agent_access`
  mixes static assertions with an optional real `systemd-tmpfiles --root` call.
  Determine which parts simulate provisioning; do not describe static assertions
  as a successful installation test.
- `test_milestone14_installed_integration.py`: `TestProductionSystemdConfinement`
  checks source units, while
  `TestPackagingAndAssetClosure.test_launchers_import_release_packages_without_pythonpath`
  actually runs checkout launchers. Neither performs a Pi installation despite
  the file/class wording; classify their actual purpose before removal.
- `test_milestone3_port.py`: `TestBuildWorkspaceScript` includes help/dry-run
  checks and destructive-path rejection; `TestInstallRos2ClosureManifest` also
  includes real `.deb` parsing. `test_install_workflow.py`: `TestVerifyLock`,
  `TestUpstreamInputsVerification`, `TestExternalWorkingDirectory`, and the
  remaining `TestSecurityAndCliGuards` methods need individual classification.
  `test_dependency_closure.sh` verifies locks/manifests and tamper rejection;
  it does not install packages or simulate a Pi host.

Retain runtime tests even when they use mocks: bridge zeroing/watchdogs, supervisor
heartbeats, launch command routing, operator ownership and leases, web lifecycle
APIs, and browser input behavior. In particular, do not sweep away the runtime
methods mixed into Milestone 5 review classes, the Milestone 3/4 controller tests,
or Milestone 14's `TestLifecycleAndWebAvailability`,
`TestCliAndBrowserMutualExclusion`, and
`TestCachedClientRecoveryAndIncompatibleProtocol`. Pure schema, path-safety,
checksum, and configuration-transformation tests are not automatically installation
simulations. Check shared fixture users and `deploy.sh` test-runner references
before removing helpers or entire modules. The inventory is a starting list;
repeat the scan at implementation time and apply the same per-method decision
to additional candidates.

### Milestone 15 — Raised-track web movement and failure acceptance

- [ ] Observe all four directions via buttons and keyboard at conservative speed;
  verify release, Space, Disarm, Stop controller, idle timeout, and hold cap.
- [ ] Measure loss-of-focus, tab close, browser crash, Wi-Fi loss, delayed/buffered
  packets, web crash/hang, operator crash/hang, and reconnect behavior.
- [ ] Repeat relevant controls and failure cases in installed Android/iOS PWAs,
  including touch cancellation, app switching, screen lock, resume, and updates.
- [ ] Re-run affected native guard/bridge/serial/host-stop acceptance cases; a
  previous native acceptance report does not certify the new producer path.
- [ ] Record exact installed release, configuration, client/browser versions,
  network conditions, physical observer, instruments, and raw evidence.

Target: physical rest within 300 ms for browser/network/web loss with a healthy
agent, measured from the injected fault; record event-to-agent, zero-write, and
physical-stop timing separately. Agent and downstream failures must also satisfy
the applicable native measured bounds. These are acceptance targets, not current
claims. If measurements fail, keep the gate open; do not silently raise limits.

Exit: actual track observations and instrumented timings pass. Mocks, request
acknowledgments, and serial writes alone cannot mark physical acceptance passed.

### Milestone 16 — Operator handoff and release

- [ ] Document certificate setup and direct page access, normal start/arm/drive/stop flow,
  input limitations, loss-of-connection recovery, CLI handoff, and rollback.
- [ ] Document phone installation, tested browser/OS versions, offline behavior,
  update/recovery workflow, API schemas, and frontend build commands.
- [ ] Publish a web-specific acceptance report with separate software, installed
  Pi, and physical results, and links to reproducible evidence.
- [ ] Obtain an independent committed-code review and resolve critical findings;
  record the accepted release and remaining operational restrictions.

Exit: the owner can perform everyday controls entirely from the page, with
bounded motion, verified stopping, and a documented local recovery path.

## 8. Definition of done

An installed Pi serves the Vue/TypeScript PWA through FastAPI/Uvicorn without an
Internet dependency or a Node.js runtime. Mobile installation, offline status,
disarmed updates, and cached-client compatibility/recovery pass their gates;
start/stop, arm/disarm, mouse/touch buttons, W/S/A/D, and Space work as specified.
Only one operator controls motion, loss of input or connectivity cannot latch
movement, reconnection never resumes it, and production DDS/security confinement
remains effective. M10–M16 evidence, including M11.1, M14.1, and M14.2, is complete and
distinguishable from native M1–M9 evidence. Raised-track completion still does
not authorize on-ground use.
