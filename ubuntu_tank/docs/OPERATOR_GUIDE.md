# Docker operator guide

The accepted setup is Ubuntu 26.04 Pi 5, paired ARM64 runtime/web containers,
and Windows Chrome 153.0.8010.53 over Wi-Fi. The Windows version was not recorded.
Android/iOS and mobile installation are future work. Acceptance covers raised
tracks only; on-ground driving remains unauthorized. See the
[release handoff](../../docs/M16_RELEASE_HANDOFF.md) for evidence and restrictions.

## Open the page

Use the configured `https://YOUR_PI_NAME:8443` address. The certificate must cover
that exact name and `web.yaml` must allow that exact origin. Windows certificate
trust was owner-confirmed during M15. For another Windows profile, import the
verified public certificate into that user's Trusted Root Certification
Authorities store, then reopen Chrome and check the certificate and hostname.
Never copy the private key or bypass a certificate warning to drive.

For first provisioning, follow the [TLS setup procedure](../../docker/ubuntu_tank/README.md#host-preparation-and-deployment).
`setup-tls` creates a missing pair only while applications are stopped; it does
not rotate an existing invalid pair. Preserve installed calibration, allowed
origins, TLS and SROS2 keys during updates. Clock errors can invalidate otherwise
correct certificates; check the Pi clock before replacing a certificate.

## Control the robot

Keep tracks raised and clear and the power disconnect accessible. Use one control
page at a time; another page's Stop or focus-loss handling can interrupt control.

1. **Take control** starts the controller if necessary and acquires ownership
   without arming or moving.
2. **Start** arms. A fresh direction press is required before any movement.
3. Hold a direction button or W/S/A/D. Release stops motion while retaining Arm.
4. **Stop** or Space zeroes and disarms, retaining ownership. Space also works
   when Start has keyboard focus. Observers can Stop without owning control.
5. **Release control** waits for controller shutdown before relinquishing ownership.
   If shutdown fails, retry Release; do not treat a disconnected page as confirmation.

The accepted installed limits are a 1-second input lease, 5-second continuous
hold cap, 30-second armed-idle timeout and 300-second ownership inactivity timeout.
Installed configuration can override limits. Polling and automatic heartbeats
never renew ownership inactivity; accepted owner actions do. These software
limits are not measurements of physical stopping time.

| What happened | Recovery |
|---|---|
| Brief input gap, `INPUT_PAUSED`, ownership and Arm retained | Release every held control, wait for fresh neutral acknowledgment, then press again. Do not keep an old direction held. |
| Stop, focus loss, hidden tab, idle/hold limit or disarming fault | Release held controls, return to fresh status, use Start, then a fresh direction press. Resolve any reported fault first. |
| Connection loss or ownership expiry | Wait for fresh status; Take control again, Start, then a fresh press. Nothing resumes automatically. |
| Failed Release or pending shutdown | Retry Release and wait for confirmation. If the page is unavailable, use Pi recovery below. |

Tab switching stops/disarms but retains a healthy owner binding. Background time
counts toward ownership expiry. Closing a tab can leave the controller active
but disarmed and ownerless; use explicit Release when finishing.

## Offline and application updates

Cached static assets can show an offline page; they cannot authorize motion.
Unavailable or stale telemetry disables driving. Restore connectivity and follow
the recovery table. An offline page is not proof that the robot has stopped.

When **Update Now** appears, it releases this session and waits for confirmed
shutdown before activating the new worker and reloading. A failed cleanup leaves
the old page available for retry. After reload, Take and Start are explicit.
Older incompatible clients cannot acquire control; refresh/update the page.
The actual two-release update was tested with automated Chromium 149.0.7827.55;
Windows Chrome controls and certificate trust were checked separately.

## Pi recovery and terminal handoff

Run these commands through SSH on the Pi. Status and logs are read-only. Stop
interrupts both containers and closes control admission; it does not depend on
browser ownership.

```bash
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py status
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py logs
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py stop
```

If motion does not stop as expected, use the physical power disconnect. Software
status and old telemetry cannot substitute for observing the tracks.

For a boot-clock/TLS failure, inspect `timedatectl status`, `date -u` and
`sudo journalctl -u ubuntu-tank-container.service -b --no-pager`. Restore correct
system time using the Pi's configured time synchronization and verify it before
retrying the installed service. Do not disable certificate validity checks.

```bash
sudo systemctl restart ubuntu-tank-container.service
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py target-test
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py status
```

The restart replaces the pair and leaves the controller stopped/disarmed/ownerless.
Require `PASS_STOPPED_INTEGRATION` before Take. The observed M15 failure needed an
explicit service start after clock synchronization; automatic cold-boot recovery
is unresolved. These instructions do not claim a new cold-boot test.

After an interrupted update, inspect status/logs, then explicitly redeploy the
retained manifest and repeat the stopped check:

```bash
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py deploy /var/lib/ubuntu_tank-container/release.json
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py target-test
```

That manifest selects the last deployment-attempt record; verify its release ID
against the intended release first. If choosing another retained release, use
its verified manifest with matching images already loaded. There is no automatic
rollback. Native fallback requires the separate
[manual Git fallback procedure](../../docker/ubuntu_tank/README.md#manual-git-fallback),
including restoring host state; checking out old code alone is insufficient.

For terminal control, first Release in the browser and close its control pages.
Use the [container terminal commands](../../docker/ubuntu_tank/README.md#mounts-and-startup)
with the regular operator IPC client. The terminal client requires a running
controller; after browser Release, start it disarmed through the lifecycle socket:

```bash
runtime_id="$(sudo docker ps -q --filter label=com.docker.compose.project=ubuntu-tank --filter label=com.docker.compose.service=runtime)"
sudo docker exec "$runtime_id" runtime-entrypoint python3 -c 'from ubuntu_tank_protocol.lifecycle_client import LifecycleClient; result = LifecycleClient().start_controller(); print(result); raise SystemExit(0 if result[0] else 1)'
```

Continue only when Start succeeds. Run keyboard teleop without
`--ack-tracks-raised` to acquire disarmed; after confirming raised tracks, R arms,
W/S/A/D drive, Space stops and Ctrl-C exits. Terminal input stalls require explicit
rearming. After exiting, run the host `install.py stop` command to shut down the
pair, or use `LifecycleClient().stop_controller()` through the same socket and
verify its success. Do not use `--direct-ros` to bypass an unavailable operator agent.

## Build, deploy and interfaces

From the development computer, use the repository `.venv` and the
[build/transfer/deploy guide](../README.md#build-and-deploy-to-pi-5).
`docker/ubuntu_tank/deploy.sh YOUR_PI_SSH_ALIAS` builds and transfers the pair;
`--build-dir EXISTING_BUILD` reuses and rechecks a completed build. The wrapper
uses `python` from PATH, so activate the repository environment first.
Pi host operations use system Python. Never run Compose `up` directly.

```bash
source .venv/bin/activate
./docker/ubuntu_tank/deploy.sh YOUR_PI_SSH_ALIAS
# Development checks, without connected robot hardware:
./ubuntu_tank/deploy.sh test
npm --prefix ubuntu_tank/web ci
npm --prefix ubuntu_tank/web run type-check
npm --prefix ubuntu_tank/web run build
```

The [interface reference](CONTROL_INTERFACES.md) documents API operations and
errors. [OpenAPI](openapi_v1.json) and the
[schema-generation guide](WEB_DEPENDENCY_CLOSURE.md#3-protocol--api-compatibility-specification)
cover protocol 3.0.0/schema 3 and generated browser types. Backend dependencies
stay in images; Node.js/npm is needed only on the development computer.
