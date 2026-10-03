# C2 runtime supervision

C2 replaces the controller runner and lifecycle server with container process
supervision. Native service startup is no longer supported by these entrypoints.
Images and Compose are defined in the [C3 image guide](../../docker/ubuntu_tank/README.md).
C4 host deployment is implemented. M15 supplies owner-scoped Docker acceptance;
see the [M16 handoff](../../docs/M16_RELEASE_HANDOFF.md) for evidence and exclusions.
Historical native acceptance does not certify the container runtime.

## Processes and interfaces

`docker/ubuntu_tank/supervisord.conf` runs one operator, one lifecycle adapter and
one independent safety monitor. Controller autostart, automatic restart and
startup retries are disabled. Controller Start launches the installed guarded
ROS graph; Stop leaves the operator and lifecycle sockets available. Required
process exits and stalled progress stop the controller. Infrastructure faults
terminate Supervisor so the container restart policy can recover to a
stopped controller.

The lifecycle server now lives in `ubuntu_tank_supervisor.lifecycle_service`.
The web package remains a ROS-free socket client. Start/Stop/Status/Logs retain
their wire fields and historical service identifier. Only the application UID
can connect; IPC sockets use mode `0600`. Supervisor's control socket, heartbeat
files and ROS PID files stay in the private runtime directory. Logs read the
fixed controller log through Supervisor, capped at 24 KiB and 200 lines before
JSON framing. Controller logs rotate at 1 MiB with two backups.

Each Start has an admission token. Stop revokes it before signalling processes,
including while a Start waits for readiness. Runner readiness must match that
token. Runtime epoch changes clear operator ownership, challenges, motion and
pending Arm state; late requests cannot restore them. Recovery requires fresh
ownership and Arm. Guard/bridge failure leaves the controller stopped until an
explicit Start.

An immediate Start after Stop waits up to 10 seconds for the old controller
record to clear, the monitor to acknowledge `controller_pid: 0`, and fresh
monitor/operator progress. The monitor acknowledges only after its old-group
cleanup finishes. This prevents that cleanup from revoking a new Start permit.
Stop or deployment revocation cancels the wait; it never arms or queues motion.

## Deadlines

| Check | Deadline or interval | Failure action |
|---|---|---|
| Operator ROS timer and IPC loop, lifecycle socket loop, running controller runner | 500 ms progress age | Direct controller group stop; infrastructure failure terminates runtime |
| Safety monitor, checked by controller runner | 500 ms progress age; 50 ms poll | Controller stops and exits |
| Supervisor control API | 200 ms socket timeout; 50 ms monitor interval | Direct group stop and runtime termination |
| First progress / controller readiness | 10 s startup allowance | Reject Start or terminate failed runtime |
| Guard and bridge | Existing controller configuration deadlines and check interval | Runner stops the complete ROS group |
| ROS group cleanup | SIGINT, then SIGKILL after 500 ms | Includes hung launch parents and descendants |
| Runner cleanup progress | 700 ms maximum | Monitor terminates runtime if cleanup stalls |

Progress comes from each monitored event loop, not an independent heartbeat
thread. Detection adds polling/API scheduling time to progress age (nominally
up to 750 ms for the external monitor). These are process-failure deadlines,
not physical motor-stop allowances. Existing command freshness, serial watchdog,
and physical-stop requirements remain unchanged. The STM32 watchdog is still
the final safeguard when the entire runtime is frozen.

## Runtime entrypoint contract for C3

Before starting Supervisor, the image entrypoint must source the installed ROS
and workspace environments, apply loopback DDS/SROS2 configuration, and provide:

- `UBUNTU_TANK_PRIVATE_DIR=/run/ubuntu_tank-private`: application-owned private
  tmpfs directory. It contains Supervisor's Unix control socket and progress.
- `UBUNTU_TANK_PREFIX=/opt/ubuntu_tank/current`: installed release root.
- `UBUNTU_TANK_PYTHON`: absolute runtime Python executable with installed packages.
- Existing controller configuration, SROS2 keystore and ROS log mounts.

Supervisor 4.3.0 is the development-tested version; the runtime image dependency
manifest records its packaged version. Only the test environment uses
`UBUNTU_TANK_SIMULATION=1` and `_UBUNTU_TANK_TEST_CHILD_CMD`; production must not
set either. Optional `UBUNTU_TANK_OPERATOR_SOCKET` and
`UBUNTU_TANK_LIFECYCLE_SOCKET` relocate sockets for motor-free tests; production
uses `/run/ubuntu_tank/{operator,lifecycle}.sock`.

## Development validation

From the repository root:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python ubuntu_tank/tests/test_container_supervision.py -v
./ubuntu_tank/deploy.sh test
```

The process suite runs the actual Supervisor configuration, lifecycle socket,
operator and controller runner. Real child processes stand in for ROS guard and
bridge; they emit credentialed heartbeats and never access hardware. It covers
stopped boot, Start/Stop races, malformed frames, controller exit, each monitored
hang, group cleanup, restart admission, and operator epoch invalidation during
driving and pending Arm. Existing web/PWA tests exercise the unchanged client
contract. Installed-Pi and owner-observed physical evidence is recorded separately in M15;
physical stopping measurements remain waived. Local process tests do not replace it.
