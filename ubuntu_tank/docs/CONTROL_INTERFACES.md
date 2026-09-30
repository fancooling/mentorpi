# Controller and web interface reference

Implemented interfaces for the paired Ubuntu Tank runtime/web containers as of
M14.6. Protocol is `3.0.0`, schema version is `3`, and the HTTP API prefix is
`/api/v1`. Pi deployment and physical acceptance of this contract remain M15.

This reference covers operator IPC, controller lifecycle IPC, HTTP and WebSocket
interfaces. Internal ROS topics/services and Supervisor's private control socket
are implementation interfaces outside this catalog.

## Transport and authority

| Interface | Default address | Consumer |
| --- | --- | --- |
| Operator IPC | `/run/ubuntu_tank/operator.sock` | Web relay, terminal and bench clients. |
| Lifecycle IPC | `/run/ubuntu_tank/lifecycle.sock` | Runtime acquisition/release coordinator; web status/log queries. |
| HTTP | `https://ROBOT_HOST:8443/api/v1/` | Browser and API clients. |
| WebSocket | `wss://ROBOT_HOST:8443/api/v1/control` | Browser challenge/intent connection. |

Socket paths, web listening/TLS settings and allowed origins come from installed
web configuration. Both Unix sockets use `AF_UNIX` stream connections with
UTF-8 JSON objects terminated by a newline. Frame limits are 65,536 bytes.
Linux peer credentials authorize local connections; application ownership is
separate and belongs to one operator connection. Use the shared
[IPC client](../src/ubuntu_tank_protocol/ubuntu_tank_protocol/ipc_client.py) and
[lifecycle client](../src/ubuntu_tank_protocol/ubuntu_tank_protocol/lifecycle_client.py)
for framing and timeouts.

There is no web login. HTTP mutations check a supplied Origin against the allowed
list; WebSocket connections require an allowed Origin. Operation and bind tokens
protect connection/session association and must not appear in public status or
logs. An observer may Stop; observer traffic does not renew the owner's inactivity
deadline. Disconnect stops/disarms and revokes ownership but does not prove that
an explicit controller-shutdown operation completed.

## Operator Unix socket

Every request has an `action`. Include `protocol_version: "3.0.0"` except for
`version`, `status`, `stop`, `disarm` and `get_observations`, which the server
permits without a matching version. Unsupported versions return
`INCOMPATIBLE_PROTOCOL` before dispatch. Requests and responses are paired on
the connection; this socket does not push WebSocket-style events.

Most replies contain `success` and optional `error`/`message`. `status` and
`version` return their data objects directly. Optional fields below are marked
`?`; all rows also inherit the action/version envelope above.

| Action | Request fields | Result and side effects |
| --- | --- | --- |
| `version` | None | Protocol/schema/API versions, supported protocols and release ID. |
| `status` | None | Operator/owner/epoch, guard and telemetry, input generation/recovery, limits, inactivity and release status. Read-only. |
| `take_control` | `request_id`, `operator_id`, `max_linear_speed?`, `max_angular_speed?` | Runtime starts controller only if needed, waits for readiness and acquires without arming. Returns private operation result. Busy/admission/readiness failures prevent acquisition. |
| `acquisition_result`, `operation_result` | `operation_id` | Query an operation on its originating connection. Returns `operation_id`, `status`, `success`, `epoch`, `error`, `message`. Another connection gets `NOT_OWNER`. |
| `bind_control` | `operation_id` | Confirm a completed acquisition before its five-second bind deadline; returns `success`. |
| `cancel_acquisition` | None | Cancel pending/unbound acquisition for this connection. Not a confirmed controller shutdown. |
| `cancel_all_acquisitions` | None | Cancel pending/unbound acquisitions globally and request startup cleanup. Internal Stop coordination; returns `success`. |
| `acquire` | `operator_id`, `max_linear_speed?`, `max_angular_speed?` | Direct local ownership acquisition; returns `epoch`. Does not orchestrate controller startup. Used by local clients with existing runtime readiness. |
| `release`, `release_control` | `request_id?`, `epoch?`, `operation_id?`, `reason?`, `operator_id?` | Owner/setup-scoped combined release. Zero/disarm, stop controller, then relinquish ownership after confirmed inactive. Returns pollable operation; failures retain a shutdown fence and retry path. |
| `start` | `request_id`, positive integer `epoch` | Owner-only explicit, idempotent arming through preflight and neutral gates. Returns `success`, `status`, `error`, `message`; never commands movement. |
| `arm` | `request_id`, `epoch` | Internal local-client arming operation with downstream confirmation. Owner-only; returns `success`, `error`, `message`. Not a public HTTP route. |
| `stop` | `request_id?`, `epoch?`, `operator_id?` | Any authorized connection can cancel setup and zero/disarm. Retains bound ownership and running controller. Returns `success`, `disarmed`; owner identity affects inactivity accounting only. |
| `disarm` | None | Internal any-client Stop/disarm path; cancels setup and returns `success`, `disarmed`. |
| `challenge` | `epoch` | Owner-only challenge: `token`, `epoch`, `input_generation`, `recovery_required`, `issued_monotonic_ns`, `deadline_monotonic_ns`, plus `success`. |
| `intent` | `response` object, or its fields at top level | Owner-only challenge response with `token`, `epoch`, `sequence`, `direction`, `input_generation`, optional `client_timestamp_ms`. Returns active direction, generation, operator state, recovery readiness and success/error. |
| `get_observations` | `since_mono_ns?` | Returns `observations` from the configured runtime callback, or an empty list. Acceptance evidence, not a motion command. |
| `reset_observations` | None | Clears callback observations and returns `reset_monotonic_ns`. |
| `motion_burst` | None supported | Always fails with `OPERATION_FAILED`; use deadline-checked challenge/intent renewals. |

Acquisition request IDs deduplicate within the originating connection. Operation
states are `pending`, `completed` and `failed`; retained history is bounded.
Release waits run outside the shared IPC loop. On shutdown failure, keep driving
blocked and retry release with a new request ID. After loss of that connection,
a fresh Take first confirms the old controller is stopped before reacquiring.
Production supplies a lifecycle client; the no-lifecycle fallback used in local
fixtures releases ownership without certifying controller shutdown.

Implementation: [operator dispatcher](../src/ubuntu_tank_operator/ubuntu_tank_operator/ipc_server.py),
[acquisition coordinator](../src/ubuntu_tank_operator/ubuntu_tank_operator/acquisition.py),
[shared schemas](../src/ubuntu_tank_protocol/ubuntu_tank_protocol/schemas.py).

## Controller lifecycle Unix socket

These are internal controller process operations, distinct from public Start
(arming) and Stop (disarming). They use an `action` without the operator protocol
version envelope. Each shared-client call opens a separate connection. Production
controls the Supervisor-managed controller group; the historical `service` field
is not a request to operate an arbitrary host systemd unit.

| Action | Request fields | Result and side effects |
| --- | --- | --- |
| `status` | None | `success`, `state`, `active`, `service`, `message`; observes `active`, `inactive` or `failed`. |
| `start` | None | Starts the controller under deployment admission and readiness checks. Returns success/state or explicit failure. Does not grant ownership or arm. |
| `stop` | None | Revokes startup admission and stops controller processes; `success`, `state`, `stopped`, `message`. Successful completion requires observed inactive. |
| `logs` | `limit?` (default 50) | Bounded controller logs: `lines`, `total_lines`, `total_bytes`, `success`. Limit is clamped to 1–200; serialized output also fits the frame budget. |

Unauthorized peers get `UNAUTHORIZED`; unknown actions get `INVALID_PAYLOAD`.
Unavailable admission returns `DEPLOYMENT_BUSY`; execution failures return
`OPERATION_FAILED` or an unsuccessful state result. The standard client uses
3-second status, 15-second start, 10-second stop and 5-second log timeouts.

Implementation: [lifecycle service](../src/ubuntu_tank_supervisor/ubuntu_tank_supervisor/lifecycle_service.py).

## HTTP API

JSON requests reject extra fields. Acquire, Start and Release require
`protocol_version: "3.0.0"`; Stop deliberately does not require that field.
Validation errors use HTTP 422; rejected origins use 403. Application failures
normally return HTTP 200 with `success: false` and an error code: inspect the
body as well as the HTTP status.

| Method and path | Input | Output and behavior |
| --- | --- | --- |
| `GET /api/v1/version` | None | `protocol_version`, `api_version`, `schema_version`, `release_id`, `supported_protocols`. Query before acquiring. |
| `GET /api/v1/status` | None | Controller service state plus operator/guard/owner, epoch, telemetry, limits, freshness and faults; session/revision, input recovery, inactivity and release progress/reason. Never renews activity. |
| `GET /api/v1/logs` | Query `limit` (1–200, default 50) | Bounded controller `lines`, `total_lines`, `total_bytes`. No arbitrary log filters. |
| `POST /api/v1/control/acquire` | Version, `request_id`, `operator_id`, optional positive speed caps | Combined Take. Returns `success`, operation ID/token/status and, on completed acquisition, epoch/bind token. Requested caps are limited by configuration. |
| `POST /api/v1/control/release` | Version, `request_id`; owned `epoch` or private setup `operation_id`/`operation_token`; optional `operator_id` | Combined cancel/zero/disarm/shutdown/release. Returns success/status, operation credentials and error/message. Poll pending results. |
| `POST /api/v1/control/start` | Version, `request_id`, positive integer `epoch`, optional `operator_id` | Explicit owner arming; returns `success`, `error`, `message`. The current HTTP response has no pollable Start operation. |
| `POST /api/v1/control/stop` | `request_id`, optional `epoch`/`operator_id` | Stop independent of ownership. Returns `success`, `disarmed` for request dispatch; not a measurement of physical rest. Retains bound ownership. |
| `GET /api/v1/operations/{id}` | Query `operation_token` for private operations | `operation_id`, `status`, `success`, `error`, `message`; acquisition results can include `epoch`, `bind_token`. Unknown/inaccessible operation returns 404. |
| `GET /api/v1/openapi.json` | None | FastAPI's live schema generated from registered HTTP routes and Pydantic models. |

Public controller start/stop and control arm/disarm routes are absent, with no
redirect to replacement actions. Built-in Swagger UI and ReDoc are disabled.
The web application also serves the compiled PWA assets.

[openapi_v1.json](openapi_v1.json) is a separately generated reference artifact;
FastAPI does not load it. Its generator also emits TypeScript definitions. The
live schema and checked-in artifact have separate generation paths and can
differ. Neither supplies the complete Unix socket or WebSocket wire contract.

Implementation: [HTTP routes](../src/ubuntu_tank_web/ubuntu_tank_web/routes_api.py),
[HTTP models](../src/ubuntu_tank_web/ubuntu_tank_web/models.py),
[application](../src/ubuntu_tank_web/ubuntu_tank_web/app.py).

## WebSocket API

Connect to `/api/v1/control` with an allowed Origin. Send JSON text frames shaped
as `{"action":"...","payload":{...}}`; server frames use
`{"type":"...","payload":{...}}`. Frames larger than 65,536 bytes close with
1009. Origin/policy violations can close with 1008. HTTP acquisition establishes
protocol compatibility; there is no separate WebSocket version action.

| Client action | Payload | Behavior |
| --- | --- | --- |
| `bind` | `operator_id`, `epoch`, `bind_token` | Bind the acquired session before its deadline. Uses the private token returned by acquisition. |
| `intent` | `token`, `epoch`, increasing `sequence`, `direction`, `input_generation`, optional `client_timestamp_ms` | Respond to a fresh runtime challenge; never buffer or retry nonzero intent. |
| `stop` | Optional `request_id`, `epoch` | Stop remains available even before binding; clears queued input and requests disarm. |
| `heartbeat` | No required fields | Acknowledgment only; does not renew motion or ownership inactivity. |

Directions are `neutral`, `forward`, `reverse`, `spin_left`, `spin_right`, `stop`.
Runtime monotonic timestamps are opaque to the browser; do not compare them to
browser wall-clock timestamps.

| Server type | Meaning |
| --- | --- |
| `challenge` | Token, epoch, input generation, recovery flag and runtime issue/deadline timestamps. |
| `ack` | Action result; intent acknowledgments include sequence/direction and recovery generation/state information. |
| `state` | Declared by shared client types, but not emitted by the current handler. Poll HTTP status for live state. |
| `error` | Error code/message and, where supplied, failed action context. |

Input expiry enters `INPUT_PAUSED` when controller health permits retaining Arm.
Release all held keys/pointers, send fresh neutral on a recovery challenge, wait
for acknowledgment, then require a new physical press. Expiry and recovery fence
old challenges with `input_generation`. Disconnect, focus loss and hard faults
still disarm; reconnect never reacquires or arms automatically. Terminal and
bench clients stop and require explicit rearming after a stall.

Implementation: [WebSocket handler](../src/ubuntu_tank_web/ubuntu_tank_web/routes_ws.py).

## Shared safety, errors and configuration

Epochs fence ownership/Stop transitions; input generations fence input recovery.
Start never moves without fresh directional intent. Stop has priority during
setup, arming, shutdown and closed deployment admission. Release completes only
after controller shutdown; local success never certifies physical stopping.

Common failures are `NOT_OWNER`, `DEPLOYMENT_BUSY`, `INVALID_EPOCH`,
`INVALID_STATE`, `INVALID_PAYLOAD`, `INCOMPATIBLE_PROTOCOL`, `PREFLIGHT_FAILED`,
`CONTROLLER_UNAVAILABLE`, `STALE_TELEMETRY`, `TIMEOUT`, `STALE_TRANSACTION` and
`OPERATION_FAILED`. Challenge errors include `CHALLENGE_REUSED`,
`CHALLENGE_EXPIRED`, `SEQUENCE_OUT_OF_ORDER`, `LEASE_EXPIRED`, `INPUT_CONFLICT`
and `MAX_HOLD_EXCEEDED`. See the [error enums](../src/ubuntu_tank_protocol/ubuntu_tank_protocol/enums.py).
Never retry arming/nonzero input automatically after reconnect; reconcile live
state and require fresh operator intent.

Both containers load installed `web/web.yaml`. `lease_duration_sec` defaults to
1 second, must be 0.050–1.000 seconds and exceed `challenge_interval_sec` (default
0.050). Ownership inactivity uses `control_idle_timeout_sec`, default 300 seconds,
finite and greater than zero up to 3600 seconds. Accepted owner actions renew it;
polling, duplicate/background neutral and challenge traffic do not. Expiry fences
arming/driving and runs combined Release. Status exposes remaining time and
release progress/reason; fresh acquisition is possible after completed expiry.

Change configuration while stopped/disarmed through the
[container deployment workflow](../../docker/ubuntu_tank/README.md). Installed
valid overrides are retained; changing shipped defaults does not replace them.
Restart is required; browser requests cannot alter deadlines or hot-reload them.

The runtime startup loader accepts `UBUNTU_TANK_WEB_CONFIG`; missing or invalid
configuration fails before ROS initialization. First-command delivery and the
independent guard/bridge watchdogs remain separate safety deadlines. The proposed
1.2-second input-loss physical-rest target remains unverified until M15 measures
stopping time and distance on the robot.
