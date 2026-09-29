# Native Ubuntu Tank Web Control Dependency Closure & Protocol Specification

This document defines the Milestone 10 runtime dependency closure, locked package specifications, API/OpenAPI models, and state machine invariants for the MentorPi Pi 5 Web Control system according to `docs/MENTORPI_WEB_CONTROL_DESIGN.md`.

---

## 1. Native Target System & APT Runtime Dependencies

The web control and operator subsystems run natively on Ubuntu 26.04 ARM64 without containerization. Their dependencies are strictly managed via Ubuntu APT packages:

| Package | Upstream Version (Noble / Resolute) | Purpose | Service Owner |
|---|---|---|---|
| `python3-fastapi` | `0.101.0-3` / `0.118.0-1` | REST & WebSocket web framework | `mentorpi-tank-web.service` |
| `python3-uvicorn` | `0.27.1-1` / `0.38.0-1` | Single-worker ASGI production server | `mentorpi-tank-web.service` |
| `python3-pydantic` | `1.10.14-1` / `2.12.5-2` | Data parsing & input validation models | Shared schemas |
| `python3-websockets` | `12.0-1build1` / `15.0.1-1build2` | High-throughput async WebSocket streaming | `mentorpi-tank-web.service` |
| `python3-cryptography` | `41.0.7-4ubuntu0.1` / `46.0.5-1ubuntu2.2` | Local TLS certificate and key provisioning | `mentorpi-tank-web.service` |
| `python3-yaml` | `6.0.3-1build1` | Controller & web configuration parsing | `mentorpi-tank-operator.service` |

### Service Isolation & Confinement

1. **`mentorpi-tank-web.service`**:
   - Runs as non-root user `ubuntu-tank-web`.
   - Has **NO** access to `/dev/rrc` or serial dialout groups.
   - Has **NO** ROS 2 environment, keystores, or participant discovery permissions.
   - Communicates with the operator agent exclusively via local Unix domain socket `/run/ubuntu_tank/operator.sock`.
   - Communicates with the lifecycle helper exclusively via `/run/ubuntu_tank/lifecycle.sock`.
   - Terminates TLS at Uvicorn; serves pre-compiled static PWA assets from `web/dist/`.

2. **`mentorpi-tank-operator.service`**:
   - Runs as dedicated non-root user `ubuntu-tank-operator`.
   - Owns the single authorized ROS 2 publisher to `/controller/cmd_vel` and the `/motor_guard/arm` client.
   - Confined to `ROS_LOCALHOST_ONLY=1` and authenticated via SROS2 operator enclave.
   - Has **NO** direct serial port access (`/dev/rrc` remains exclusively owned by `ros_robot_controller`).

---

## 2. Locked Frontend Build Dependencies

The frontend is a Vue 3 + TypeScript Single Page Application / Progressive Web App (PWA) built with Vite and packaged with the application release. The target Pi never runs Node.js or builds assets at runtime.

The workstation build dependencies are pinned and locked in `ubuntu_tank/web/package-lock.json`:

| Package | Version | Purpose |
|---|---|---|
| `vue` | `^3.4.21` | UI reactivity, component tree |
| `vite` | `^5.2.6` | Next-generation bundler and dev server |
| `vite-plugin-pwa` | `^0.19.8` | Service worker generation and PWA manifest |
| `typescript` | `^5.4.2` | Static type safety and contract enforcement |
| `vue-tsc` | `^2.0.7` | Template type checking |
| `@vitejs/plugin-vue` | `^5.0.4` | Vite Vue 3 SFC compilation support |
| `@types/node` | `^20.11.30` | Build script environment types |

### PWA Cache Strategy & Security Boundary

- **Asset-Only Caching**: The generated Service Worker caches strictly static assets (`*.js`, `*.css`, `*.html`, icons).
- **API Exclusion**: All `/api/` HTTP mutations, telemetry polling, and WebSocket `/api/v1/control` connections are configured with `NetworkOnly` and explicitly excluded from service worker caching and background synchronization.
- **Offline Safety**: If the network connection or Pi drops, the UI presents an offline banner and immediately disables driving controls. Offline requests never queue or replay motion.

---

## 3. Protocol & API Compatibility Specification

`src/ubuntu_tank_protocol/` is the installable, standard-library-only shared
package for constants, enums, schemas, web configuration validation, operator
IPC client, lifecycle wire limits, and API generation. Web depends on this
package, without importing ROS or the operator implementation. Runtime authority
and state transitions remain in `ubuntu_tank_operator`. The lifecycle client
imports shared wire limits; the C2 runtime server lives in
`ubuntu_tank_supervisor.lifecycle_service` and uses Supervisor, not host systemd.

Regenerate the unchanged public artifacts from the repository root:

```bash
.venv/bin/python ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/openapi_generator.py
.venv/bin/python ubuntu_tank/tests/test_container_protocol.py -v
```

The tests build and install protocol/web wheels into a temporary directory,
serve API requests with runtime imports forbidden, and compare generated
OpenAPI/TypeScript output with the committed artifacts.


The web UI/API has no user authentication or login. HTTPS supports the PWA;
connection ownership and short motion leases arbitrate control. Obsolete M10
login/logout schemas, generated API artifacts, and credential configurations were
removed in Milestone 12. No password-hashing dependency is required by this design.
Existing native SROS2 credentials and process-identity safeguards remain unchanged.

- **Protocol Version**: `3.0.0`
- **API Version**: `v1`
- **Schema Version**: `3`
- **Supported Releases**: Protocol `3.0.0` is required for acquisition, Start and Release; Stop remains available without a version field.

### Client Compatibility Handshake

Before attempting to acquire operator authority or submit commands, all clients (web browsers, CLI tools) MUST query `GET /api/v1/version`. If `protocol_version` major version does not match, the client MUST refuse to arm and prompt the operator for an update or page reload.

---

## 4. State Machine & Monotonic Lease Invariants

The operator state machine enforces strict mathematical guarantees:

1. **State Space**:
   - `NO_OWNER`: Controller running or idle, no operator connected. Motion disabled.
   - `OWNED_DISARMED`: Single operator session holds ownership. Disarmed.
   - `ARMING`: Arming transaction in progress; awaiting guard verification and downstream zero confirmation within 250 ms.
   - `ARMED_IDLE`: Guard armed; operator lease active; neutral zero intent maintained.
   - `DRIVING`: Active direction held under a valid challenge lease.
   - `INPUT_PAUSED`: Expired input commands zero while retaining healthy Arm; recovery needs fresh neutral after downstream zero confirmation.
   - `FAULT`: Safety or telemetry failure; commands zero and disarms.

2. **Monotonic Challenge Leases**:
   - **Lease Duration**: `lease_duration_sec` in `web.yaml`, default 1 second. Runtime validates and loads it at startup; status reports the effective value.
   - **Challenge Interval**: 50 ms (`CHALLENGE_INTERVAL_SEC = 0.050`).
   - Single-use cryptographically random tokens (`secrets.token_urlsafe(16)`).
   - Evaluated strictly against the Pi monotonic clock (`CLOCK_MONOTONIC`).
   - Responses arriving after monotonic deadline are rejected with `CHALLENGE_EXPIRED`.
   - Replayed tokens are rejected with `CHALLENGE_REUSED`.
   - Out-of-order sequence numbers are rejected with `SEQUENCE_OUT_OF_ORDER`.

3. **Continuous Hold Cap**:
   - Holding a direction key or button continuously is capped at 5.0 seconds (`MAX_CONTINUOUS_HOLD_SEC = 5.0`).
   - Exceeding 5.0 s immediately zeroes velocity, disarms the guard, transitions to `OWNED_DISARMED`, and records `MAX_HOLD_EXCEEDED`.
   - Motion cannot continue until the operator releases the key, re-arms, and presses again.

4. **Armed Idle Timeout**:
   - 30 seconds without directional input in `ARMED_IDLE` or `INPUT_PAUSED` automatically disarms the guard (`IDLE_TIMEOUT_SEC = 30.0`).

5. **Stop Priority**:
   - Space key, Stop UI button, or `POST /api/v1/control/stop` immediately invalidates the control epoch, sets requested velocity to zero, cancels outstanding challenges, and initiates disarm.
   - Stop is unprivileged and can be triggered by any connected client even if another tab owns driving.
   - Stop does not block on ROS service response.

6. **Compensating Disarm on Arming Timeout**:
   - Arming transaction must receive guard armed confirmation and downstream zero delivery confirmation within 250 ms (`FIRST_COMMAND_DEADLINE_SEC = 0.250`).
   - If confirmation is delayed or fails, compensating disarm is executed immediately.

---

## 5. Lock Hierarchy & Order

To guarantee complete deadlock freedom and ensure emergency stop availability, all lock acquisitions must observe the three-tier hierarchy:

1. **Level 1 (`DEPLOYMENT_LOCK`)**: `/run/lock/ubuntu_tank/deploy.lock` (wraps packaging, activation, rollback).
2. **Level 2 (`OPERATOR_LOCK`)**: `/run/lock/ubuntu_tank/operator.lock` (wraps operator ownership, arming, lease renewal).
3. **Level 3 (`LIFECYCLE_LOCK`)**: `/run/lock/ubuntu_tank/lifecycle.lock` (wraps systemd unit start/stop transitions).

**Rule**: Locks MUST be acquired in increasing numerical order (`1 -> 2 -> 3`).
**Emergency Exception**: Stop requests MUST NEVER block behind `DEPLOYMENT_LOCK`. Stop routines use non-blocking attempts (timeout <= 50 ms) and invoke immediate fail-closed zeroing regardless of lock contention.

## Controller and web interfaces

The [interface reference](CONTROL_INTERFACES.md) contains the implemented M14.5
operator/lifecycle Unix socket commands, HTTP routes, WebSocket messages,
version requirements, ownership/recovery rules and timeout configuration.
FastAPI serves its own route-derived schema; `openapi_v1.json` is a separate
generated artifact. Generation commands remain in §3 above.
