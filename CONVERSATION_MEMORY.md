# MentorPi Conversation Memory

Last updated: 2026-09-29

Keep this file as a concise current handoff. Read `AGENTS.md` for working rules
and `GEMINI.md` for architecture and safety. Detailed history belongs in Git and
the linked designs. Recheck running services, images, devices and Git state
pushbefore relying on recorded observations.

## Current state

- Current delivery is paired ARM64 runtime/web containers on Ubuntu 26.04 Pi 5
  with ROS 2 Lyrical. Runtime owns ROS, Supervisor, operator authority and
  `/dev/rrc`; the ROS-free web service uses read-only shared IPC.
- C1–C4 implementation is complete. C4 passed owner-scoped Pi deployment,
  redeployment, reboot and shutdown/power-on checks. USB reconnect,
  competing-owner and interrupted-update tests were waived, not passed.
- M14.3–M14.6 are implemented. M14.6 is commit `6645425`; it and documentation
  through `d1410d3` are in the locally recorded pushed baseline.
- Latest owner-provided output confirms M14.6 Pi deployment and stopped
  integration on September 29 (September 30 UTC). M15 physical acceptance
  remains unverified.
  M15 supplies container C5 evidence; C6/M16 release handoff remains pending.
- Local M14.6 validation: 580 Python tests passed, two skipped, and 42 browser
  scenarios passed; build/type checks and independent review passed. Browser
  evidence uses real HTTP/WS/IPC with simulated controller telemetry.

## Current control contract

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

- Latest release: `549acd669a7af59bb689a4d6e5fae85d5a68b43e3830e16450296b8251ad74f2`;
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
- Next: confirm updated browser/PWA controls and retained settings, then complete
  M15 raised-track direction, recovery, shutdown,
  PWA and instrumented stopping tests. Finish stopped/disarmed/ownerless.
- Historical native controller M6/M9 acceptance was `ACCEPTED`: four directions,
  nine failure conditions and an observed 280 ms STM32 watchdog. This does not
  certify Docker or the revised web producer. On-ground motion remains unauthorized.
- Installation/service tests belong on the real Pi; do not replace missing target
  evidence with development-machine installation simulations or source assertions.
  M14.2 removed those simulations. Retained native scripts are not the current
  deployment path; historical cleanup remains part of C6.
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
