# MentorPi Conversation Memory

Last updated: 2026-10-03

Keep this file as a concise current handoff. Read `AGENTS.md` for working rules
and `GEMINI.md` for architecture and safety. Detailed history belongs in Git and
the linked designs. Recheck running services, images, devices and Git state
before relying on recorded observations.

## October 2 M15 continuation

- HTTPS certificate rotated on the Pi to include the owner's LAN DNS name,
  retaining all prior SANs. Old certificate/key and web configuration are backed
  up under `/var/lib/ubuntu_tank-container/host-backup/tls-rotation-*`.
  Corrected installed `tls_setup.py` from mode 0600 to 0644 so the non-root
  web image can read it. Same-release deployment and stopped target integration
  passed; curl verified HTTPS with the new certificate and requested hostname.
  Controller finished inactive, disarmed and ownerless. The owner subsequently
  confirmed Windows certificate trust works, closing the final scoped M15 check.
- Initial release `268674c59248a738ccf46242c5e15aac9b80b00113f8dc4bc3dcbf9b5ee0c69f`,
  source `73748c6`, includes the focus-retention fix. Fresh stopped integration
  passed. Full results: `docs/M15_ACCEPTANCE_2026-10-02.md`; raw local evidence:
  `ubuntu_tank/.work/m15-20261003/`.
- Owner reconfirmed raised tracks and authorized motor/fault tests; selected
  Wi-Fi and waived fault-to-track-stop measurements. Waived is not passed.
- Client acceptance is Windows Chrome only. Owner excluded Android/iOS browser
  and PWA testing and plans separate mobile client apps in future work.
  Owner reported all requested Windows Chrome checks passed on `153.0.8010.53`
  (Official Build, 64-bit); Windows OS version was not supplied.
- Owner removed serial disconnect/reconnect, Wi-Fi radio-loss, power-off/cold-boot
  and resource-exhaustion tests from the remaining M15 plan. These are excluded, not passed. Keep the
  observed boot-time clock failure in the report; no fix is implied.
- Real Pi/browser tests passed synthetic focus retention, 30-second armed-idle
  disarm, Release shutdown, Take restarting disarmed, and 300-second ownership
  expiry with polling not renewing it. Space reset the ownership countdown.
- Ten injected input-gap/recovery cycles passed with one Start, ten stale-motion
  replays rejected, and explicit Stop during recovery. Owner confirmed stopping
  between pulses and no unexpected movement. Physical timing was not measured.
- Boot initially failed TLS validation because the Pi clock was July, before
  certificate validity. After time synchronization, explicit service startup
  restored operation. The failure remains documented; owner excluded further
  power-off/cold-boot testing from this M15 scope.
- Immediate Take after Release once failed with runtime safety progress
  unavailable. The deployed fix and repeated test below supersede that open issue;
  preserve the original failure in acceptance.
- Nine Docker/process faults and browser offline/tab-close/crash were injected;
  stopped restoration passed. Tab close left the controller active but disarmed
  and ownerless before host restoration; it did not independently shut it down.
  Owner confirmed correct stopping in all these fault cases. Windows Chrome
  controls/tab-resume checks passed separately. Guard/bridge failure snapshots
  had unconfirmed disarm; do not infer physical stopping from stale telemetry.
  Do not infer completion from the older native certificate.
- Automated session finished with fresh stopped integration passing, both
  containers healthy, controller inactive, ownerless and no pending disarm.
  Fresh HTTPS state after Windows Chrome manual tests also confirmed inactive,
  disarmed, ownerless, no pending disarm or last fault, and `EXPLICIT_RELEASE`.
  A subsequent interrupted same-release redeployment test passed: killing the
  installer after admission closure produced `DEPLOYMENT_BUSY`; normal recovery
  passed stopped integration. See `interrupted-deploy.log`.
- Current release `4b320e602524306678496ec7a008e0e454f6d81a66301984f6d7518df92928db`
  was built with the immediate-restart fix on source `73748c6`, subsequently
  committed in `2c5bb5d`. Start waits
  for monitor cleanup acknowledgement and fresh safety progress. Fourteen
  supervision tests, full development suite, ARM64 smoke/deployment and independent
  review passed. Twenty consecutive live Take/Release cycles passed without Arm.
  An earlier 14-cycle attempt ended in browser “Failed to fetch”; Chromium also
  recorded `ERR_NETWORK_CHANGED` during the successful repeat. Network reliability
  is not established by the lifecycle pass. See `reacquire.jsonl`.
- Actual same-origin application update passed across older/new deployed releases:
  Update Now released control and activated the new worker stopped/disarmed/
  ownerless. Automated Chromium used a pinned-certificate exception; this does not
  prove Windows trust. Final stopped integration and certificate-verified HTTPS
  passed with healthy containers, inactive/disarmed/ownerless and no pending
  disarm or last fault. Windows initially reported “Not secure”; the later
  certificate rotation and owner confirmation above close that trust issue.

## Current state

- New-host Python setup is declared in root `requirements-build.txt` (PyYAML)
  and `requirements-dev.txt` (local tests/tools plus pinned transitive libraries).
  Recreate one root `.venv` per host and install with its `python -m pip`; never
  copy the environment. Docker/Buildx, optional host browser tooling and Pi
  system-Python operations remain separate. See the container README host setup.
  Both files resolved with pip's clean-install dry run; minimal build staging
  passed with only PyYAML available. ARM64/Python 3.12 wheel resolution passed
  except ShellCheck, whose upstream source wrapper downloads a verified ARM64
  binary. That source install and ARM64 test execution were not run.
- M16/C6 handoff is complete on October 3; the owner authorized its commit.
  No push is authorized.
  `docs/M16_RELEASE_HANDOFF.md`, `docs/M16_EVIDENCE_INDEX.json` and
  `ubuntu_tank/docs/OPERATOR_GUIDE.md` bind accepted images/source/evidence and
  document controls, terminal handoff and Docker recovery. Mobile remains future
  work; all M15 exclusions, timing waivers and operational restrictions remain.
- C6 removed native units/environment/tmpfiles, disposable-root/bootstrap/recovery
  scripts, native CLI dispatch and their installation simulations. Shared Docker
  helpers and runtime/safety tests remain. All 158 accepted build inputs are
  unchanged; generated lock content and 157 files match committed `2c5bb5d`.
- M16 validation: 561 Python tests passed, one native-ROS skip, 45 browser scenarios
  passed, formatting/shell/whitespace checks passed and no new lint findings.
  Independent committed-release and worktree review passed. Local logs/checksums:
  `ubuntu_tank/.work/m16/` and the evidence index.
- October 3 read-only Pi inspection found the accepted release/configuration
  hashes still recorded, but the host service currently failed with exit status 1
  and a TLS error in the current-boot journal. No restart, target-test, Arm or
  motion was issued. The saved stopped-integration pass is historical. Follow
  documented clock/TLS recovery and obtain a fresh stopped pass before operating.
- Current delivery is paired ARM64 runtime/web containers on Ubuntu 26.04 Pi 5
  with ROS 2 Lyrical. Runtime owns ROS, Supervisor, operator authority and
  `/dev/rrc`; the ROS-free web service uses read-only shared IPC.
- C1–C4 implementation is complete. C4 passed owner-scoped Pi deployment,
  redeployment, reboot and shutdown/power-on checks. USB reconnect,
  competing-owner and interrupted-update tests were waived, not passed.
- M14.3–M14.6 are implemented. M14.6 is commit `6645425`; it and documentation
  through `d1410d3` are in the locally recorded pushed baseline.
- October 2 M15 results are recorded above: Windows Chrome controls, ten recovery
  cycles, lifecycle checks and automated fault injection/restoration completed.
  The M15 checklist separates these checked items from unexecuted tests,
  historical failures and owner exclusions/waivers. M15 is complete within the
  owner-approved Windows Chrome/Wi-Fi scope, including confirmed certificate
  trust. Instrumented physical certification remains waived, not passed. M15
  supplies C5 evidence; C6/M16 handoff is recorded below in the release documents.
- Local M14.6 validation: 580 Python tests passed, two skipped, and 42 browser
  scenarios passed; build/type checks and independent review passed. Browser
  evidence uses real HTTP/WS/IPC with simulated controller telemetry.

## Current control contract

- Focus-retention fix, now deployed and partly verified in the October 2 continuation: hidden/resumed pages invalidate cached
  telemetry without clearing the owner/session binding. Blur and hiding still
  stop/disarm; return requires fresh status and explicit Start. Background time
  counts toward the configured ownership timeout; real transport loss still
  revokes ownership. Hidden-page regression reproduced the prior release; all
  45 browser scenarios, frontend build/type checks, PWA checks and independent
  review passed. Owner-confirmed Windows Chrome refresh/focus acceptance passed.
- Release fix `9428c22` is included in the current deployed release; the owner
  confirmed Windows Chrome Release passed. Status polling could observe ownership
  loss before the shutdown result, disconnect the WebSocket and discard its
  operation, producing “Operation not found.” Defer ownership-loss cleanup while
  Release awaits its result, then reconcile. A delayed-shutdown browser regression
  reproduced the error before the fix; all 43 browser scenarios, frontend build,
  lint and independent review passed afterward. The later Windows Chrome
  refresh/Release test and stopped final state passed.
- Protocol 3: Take control starts the controller if needed and acquires ownership
  without arming. Start arms but requires a fresh direction press before motion.
  Stop/Space zeroes and disarms while retaining ownership. Release stops the
  controller and relinquishes ownership only after confirmed shutdown.
- Release/expiry fence Start and driving while shutdown is pending. Failed
  Release retains retry authority; a new Take confirms previous shutdown first.
  Observer Stop remains available but does not renew the owner's inactivity timer.
- `web.yaml` controls `lease_duration_sec` (default 1 second) and
  `control_idle_timeout_sec` (default 300 seconds). Polling, heartbeats and
  automatic neutral traffic do not renew ownership. Preserve valid installed
  overrides; change settings through the stopped deployment workflow.
- Input expiry commands zero and enters `INPUT_PAUSED`, retaining healthy Arm
  and ownership. Recovery requires actual input release, acknowledged fresh
  neutral and a new press. Hard faults, disconnect and explicit Stop disarm.
  Never replay buffered motion, automatically arm or automatically reacquire.
- Browser status polls every second and immediately on resume. Stale reads or
  runtime revision resets cannot restore authority. PWA updates wait for
  successful session cleanup before activation/reload; failed cleanup permits retry.
- The proposed input-loss physical-rest bound is 1.2 seconds, pending M15
  measurement. The earlier 300 ms browser/network bound is superseded; independent
  downstream watchdogs remain. Remote network stalls are still unresolved.

## Deployment tooling in the worktree

- Workstation entrypoint: `docker/ubuntu_tank/deploy.sh HOST`.
  It builds, smoke-tests, verifies exported image contents, transfers images and
  the host bundle, then stops, prepares, stages, deploys and verifies the Pi.
  It honors SSH configuration and remote sudo; requires an already provisioned Pi.
- Default Pi parent: `~/mentorpi-releases`, overridden by `--remote-dir PATH`.
  `--build-dir PATH` reuses a completed build; `--output PATH` selects a new one.
  Unique run directories retain artifacts on failure. No automatic rollback.
- Per owner preference, this wrapper uses `python` from `PATH`: activate a venv
  or provide an already configured global development environment. Repository
  development/tests otherwise use `.venv`; Pi host operations use system Python.
- Default builder is `mentorpi-c3`. The x86 default Docker builder cannot execute
  ARM64. Use the isolated BuildKit builder and its container-scoped emulator;
  do not change host-wide binfmt. `--builder` and `--emulator` permit overrides.
- Pi CLI renamed from `deploy.py` to `install.py`; service, bundle, documentation
  and command references are updated. Stop uses the transferred CLI, allowing an
  old installation to migrate before `prepare-host` updates installed files.
  `export_images.py` retains archive identity verification on the development host.
- `install.py` is Pi-side only: host preparation, TLS, staging, replacement,
  boot/stop, status/logs and stopped integration. Compose forbids image builds and
  pulls. Temporary image export by `image_identity.py` verifies portable identity;
  it is not development packaging and must remain on the Pi.
- Local Bash/CLI, ShellCheck/shfmt, Python lint and real Docker export/retag
  rejection checks passed. Independent diff review passed before the later
  PATH-Python adjustment; that adjustment passed shell checks. Owner-provided
  output now confirms full wrapper transfer, renamed CLI deployment and stopped
  verification on Pi. That run prompted for sudo; removal of `sudo -v` still
  needs a fresh-run check. Stdout/stderr logs are saved in the build directory.

## Last recorded Pi evidence

- October 1 (October 2 UTC) live verification: release
  `d683078e2e8dfde01b6cd1bf28b2556190ddf8448fa4f17c7ef680ddf6e949e4`,
  source `9428c22`, protocol `3.0.0`. Matching build/deployment log in
  `ubuntu_tank/.work/release-20261002T043958008430682-922607/` confirms ARM64
  smoke and deployment passed. A fresh installed `install.py target-test` passed;
  API reported inactive, disarmed and ownerless. Effective lease/hold/ownership
  limits: 1/5/300 seconds. Wi-Fi connected; Ethernet had no carrier.
  Preflight evidence: `ubuntu_tank/.work/m15-rpitank-20261002/preflight.json`.
  Owner confirmed raised tracks and authorized physical tests. Owner reported
  Take/Start without movement, brief forward/release, Stop and Release all worked.
  Owner also reported the requested 30 button and 30 keyboard cycles across
  all directions passed over Wi-Fi. These observations are uninstrumented; client
  versions, recovery, faults, mobile PWA and physical timing remain pending.
  Owner additionally reported Space, hold cap, focus loss and ownership timeout
  worked. Live API afterward remained active/NO_OWNER/disarmed with last release
  EXPLICIT_RELEASE; five-minute controller shutdown is not corroborated and an
  isolated repeat acquired at 05:05:28 UTC but lost its connection/ownership
  about nine seconds later. A check after five minutes still showed active,
  ownerless and disarmed; inactivity expiry remains unverified. Observations are saved beside preflight as
  `owner-observations.json`.
- Historical September 30 release: `549acd669a7af59bb689a4d6e5fae85d5a68b43e3830e16450296b8251ad74f2`;
  local manifest records source `701fa10` and protocol `3.0.0`. Build directory:
  `ubuntu_tank/.work/release-20260930T031803090371540-3100908`.
  Owner-provided run `release-20260930T050813715815737-3198114` passed ARM64 smoke,
  transfer, preparation, staging, deployment and `target-test`. Both containers
  reported healthy; controller finished stopped, disarmed and ownerless.
  Run log: `deploy-release-20260930T050813715815737-3198114.log` in that build
  directory. No physical-motion acceptance is inferred from this output.
- M14.4 build: `ubuntu_tank/.work/build-m14-4`; release:
  `9b372bb3eb5da79a6bb2fe2373ff4d1803a6690d8a2f79904aa7e5969dbd620c`.
  Deployment and `target-test` passed with lifecycle inactive and `NO_OWNER`;
  effective input lease was 1 second. No agent-issued Arm or motion commands.
- An outdated installed Compose file initially lacked the runtime `web.yaml`
  mount. Refreshing the complete host-tool bundle fixed deployment. Always
  transfer `install.py`, `image_identity.py`, `tls_setup.py`, `compose.yaml` and
  `ubuntu-tank-container.service` together, then run `prepare-host`.
- Browser acquisition was restored by adding the owner's HTTPS origin to retained
  `web.yaml`. Unlisted origins still returned HTTP 403. Configuration backups:
  `/var/lib/ubuntu_tank-container/host-backup/web.yaml.20260929T175419Z` and
  `web.yaml.20260929T175808Z` in the same directory.
- Target evidence: `/var/lib/ubuntu_tank-container/target-test.json`.
  C4 reboot/power-cycle evidence: `ubuntu_tank/.work/c4-pi-evidence/`, including
  its nested `evidence/` directory. These are historical observations, not live status.
- Earlier Pi-local control cycles passed, but remote delivery stalled by about
  409 ms. Disabling Wi-Fi power saving did not fix it; its original enabled state
  was restored. Ethernet retesting and network-specific acceptance remain pending.

## Safety and remaining work

- Boot/restart must leave the controller stopped, disarmed and ownerless.
  Admission binds release, deployment generation and boot ID; stale/missing
  approval blocks control. Preserve Stop, serial exclusivity and single ownership.
- Preserve Pi calibration, browser origins, TLS and signed SROS2 keys across
  updates. Missing/invalid TLS fails closed; `setup-tls` is explicit and requires
  stopped applications. Browser certificate trust is a separate client operation.
- M15 checklist now records completed Wi-Fi direction cycles and owner-observed
  controls separately from open gates; use it to avoid repeating completed work.
  October 2 continuation above supersedes the old next steps: focus-fix deployment,
  five-minute shutdown and ten recovery cycles now have live evidence. Windows
  certificate trust is owner-confirmed; actual update and scoped faults passed.
  Physical timing was waived.
  Preserve earlier evidence and finish stopped/disarmed/ownerless.
- Historical native controller M6/M9 acceptance was `ACCEPTED`: four directions,
  nine failure conditions and an observed 280 ms STM32 watchdog. This does not
  certify Docker or the revised web producer. On-ground motion remains unauthorized.
- Installation/service tests belong on the real Pi; do not replace missing target
  evidence with development-machine installation simulations or source assertions.
  M14.2 removed those simulations. Native host assets and CLI dispatch were removed in C6; retained imported
  regression helpers are not a supported deployment path.
- Factory mode remains separate: preserve the vendor image and sole hardware
  owner `MentorPi`; `MentorPiFan` is observer-only. `/mnt/rpi-rootfs` is authoritative
  for factory-image research. Do not assume historical `docker/original` exists.

## References and commands

- Build, transfer, deploy and prerequisites: `ubuntu_tank/README.md` and
  `docker/ubuntu_tank/README.md`.
- Runtime interfaces/configuration: `ubuntu_tank/docs/CONTROL_INTERFACES.md`,
  `WEB_DEPENDENCY_CLOSURE.md` and `CONTAINER_SUPERVISION.md` in that directory.
- Milestones/acceptance: `docs/MENTORPI_WEB_CONTROL_DESIGN.md` and
  `docs/MENTORPI_CONTAINER_REFACTOR_DESIGN.md`; network diagnosis:
  `docs/BUG_WEB_CONTROL_LEASE_EXPIRY.md`.
- Historical native/factory decisions: `docs/MENTORPI_FRESH_CONTROLLER_DESIGN.md`,
  `GEMINI.md`, root `README.md` and Git history. Git owns source revision history;
  preserve file purpose/vendor notices and generated release/dependency integrity.
- Development checks: `./ubuntu_tank/deploy.sh test`. Deployment wrapper:
  `./docker/ubuntu_tank/deploy.sh HOST --build-dir EXISTING_BUILD`.
  Installed Pi CLI: `sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py --help`.
- Commit/push only on explicit instruction. Use a fresh independent reviewer;
  amend review fixes into their feature commit. See `AGENTS.md` for full rules.
