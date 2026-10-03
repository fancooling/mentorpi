# M16 Docker release handoff

Date: October 3, 2026. Scope: personal-owner Windows Chrome/Wi-Fi release with
raised tracks. M15 acceptance is complete within its approved scope. M16/C6
handoff is complete, with validation and independent review recorded below. This is not
instrumented physical certification or permission for on-ground driving.

## Operator entrypoints

- [Operator guide](../ubuntu_tank/docs/OPERATOR_GUIDE.md): certificates, page
  controls, input recovery, offline/update behavior, terminal handoff and Pi recovery.
- [Build and deployment](../ubuntu_tank/README.md#build-and-deploy-to-pi-5): workstation
  wrapper, retained-build reuse, image transfer and Pi-side `install.py`.
- [Host operations and manual fallback](../docker/ubuntu_tank/README.md): explicit
  stopped deployment, admission, configuration retention and native Git fallback.
- [Control/API reference](../ubuntu_tank/docs/CONTROL_INTERFACES.md) and
  [schema/build guide](../ubuntu_tank/docs/WEB_DEPENDENCY_CLOSURE.md).

## Accepted artifact and source

Release: `4b320e602524306678496ec7a008e0e454f6d81a66301984f6d7518df92928db`.
Protocol: `3.0.0`; schema: `3`. The build recorded `73748c6` with dirty source;
the immediate-reacquisition fix was subsequently committed in `2c5bb5d`.
Do not identify the deployed content solely by the original source revision.

The retained manifest is `ubuntu_tank/.work/m15-reacquisition-fix/release.json`.
On October 3, all 157 file inputs matched committed `2c5bb5d`; the remaining
input, generated `versions.json`, matched parsed `ubuntu_tank/versions.lock`.
All 158 image-build inputs also match the current M16 worktree. M16 changes
handoff documentation and retired native tooling, not the accepted image payload.
A future build creates its own identity and must not inherit physical acceptance
merely because its Git revision looks similar.

[The evidence index](M16_EVIDENCE_INDEX.json) records full source/context identity,
paired image manifest/configuration digests, Pi-local image IDs, configuration
hashes and SHA-256 checksums of retained local artifacts. Docker transport can
change local manifest IDs; the verified configuration digests bind image content.
The index contains no admission token, credentials or private key contents.

## Evidence and limits

The [M15 report](M15_ACCEPTANCE_2026-10-02.md) remains the authoritative narrative.
Evidence paths below are relative to `ubuntu_tank/.work/m15-20261003/` unless noted.
These ignored files are local artifacts, not public downloads or a shipped
acceptance runner. Preserve them with the retained build before cleaning `.work`;
raw diagnostics may contain private deployment data and need review before sharing.

| Evidence | What it supports |
|---|---|
| `identity.json` | Initial M15 release/configuration identity; not the later fixed release |
| `owner-observations.json`, M15 report | Owner's raised-track and Windows Chrome observations; trust confirmation is recorded in the report |
| `lifecycle.jsonl`, `idle.jsonl` | Installed Take/Release, armed idle and ownership-expiry behavior |
| `recovery.jsonl`, `recovery-summary.json` | Ten input-gap recoveries, stale replay rejection and explicit Stop |
| `faults.jsonl`, `fault-*.log` | Injected process/browser faults and restoration; owner-observed stopping is recorded separately |
| `interrupted-deploy.log` | Admission closure at the tested interruption checkpoint, then stopped recovery |
| `browser-update.jsonl` | Real two-release worker update in Chromium; not Windows trust evidence |
| `reacquire.jsonl` | Twenty immediate Take/Release cycles on the fixed release, without Arm |
| `fix-final-state.txt`, `fix-final-status.json` | Historical stopped integration and inactive/disarmed/ownerless HTTPS state |
| `full-fix-tests.log`, `supervision-fix-tests.log`, `fix-deploy.log` | Local software, ARM64 smoke and deployment evidence for the runtime fix |
| `../m16/pi-record.json`, `../m16/service-readback.json` | October 3 read-only Pi identity/configuration and current failed-service observation |

The October 3 readback confirms the accepted release and matching controller/web
hashes against the saved `PASS_STOPPED_INTEGRATION` report. That report remains
historical: its `physical_acceptance: PENDING` is intentional because target-test
cannot certify motors. The host service currently reports `failed`, exit status
1, and the current-boot journal contains a certificate/TLS failure. No service
restart, target-test, Arm or motion command was issued for M16. The handoff does
not claim that the Pi is currently ready to drive. Follow clock/TLS recovery and
require a fresh stopped integration pass before operating it.

Windows Chrome 153.0.8010.53 (64-bit) over Wi-Fi is accepted; the Windows OS version
was not reported. Automated Chromium 149.0.7827.55 supplied update/fault evidence
with certificate exceptions; the owner separately confirmed Windows trust.
Android/iOS and mobile installation are excluded and reserved for future work.

Physical stopping time/distance, including the proposed 1.2-second input-loss
bound, remain waived, not passed. Serial reconnect, actual Wi-Fi radio loss,
power-off/cold-boot and resource-exhaustion tests were excluded from M15. Ethernet
was outside scope. C4's other waived cases remain waived; the M15 interrupted
redeployment pass covers only its recorded checkpoint. Boot-clock/TLS recovery
is manual, and network stalls/`ERR_NETWORK_CHANGED` remain unresolved. The
[updated lease report](BUG_WEB_CONTROL_LEASE_EXPIRY.md) separates recovery usability
from network reliability and measured stopping.

## C6 cleanup and validation

Removed obsolete native systemd units/environment/tmpfiles assets and the
native disposable-root builder/bootstrap/recovery scripts. The old deployment
CLI now rejects every operation. `install_ros2.sh` retains only read-only lock
and archive-closure checks used by development; it cannot install host packages.

Removed tests that simulated those retired native host assets and commands.
Preserved Docker build helpers, configuration migration, source/dependency gates,
operator/web behavior, motion-safety and process-supervision regressions. The
serial identity template and historical imported acceptance/deployment helpers
remain regression inputs, not a supported native delivery path; native recovery
requires restoring the historical Git version and recorded host state.

Validation on October 3:

- `./ubuntu_tank/deploy.sh test`: 562 Python tests run, 561 passed and one skipped
  because native ROS is unavailable; all 45 browser scenarios passed. Includes
  frontend build/type checks, source/dependency gates and 14 Supervisor tests.
- Modified Python formatted with Ruff; syntax/undefined-name checks passed.
  Full-rule lint comparison introduced no new findings; existing legacy lint
  findings remain. Modified shell scripts passed shfmt/ShellCheck; whitespace
  and relative-document links/anchors passed.
- Independent clean-context review: PASS for committed release changes
  `9428c22..3b24374` and separately for M16 changes against the locally recorded
  pushed baseline `3b24374`. The retained review snapshot contains exactly `PASS`. The reviewer
  independently staged the Docker context and reproduced all 158 accepted inputs
  and context hash, then passed the process and retained M5 checks.
- Logs and the review-scope record are retained under `ubuntu_tank/.work/m16/`
  and checksummed in the evidence index. Initial sandbox socket denials were
  resolved by rerunning the hardware-free suite with local socket access.

No new image build, Pi deployment or physical test was needed or performed.
Completion records the accepted artifact and scoped limitations; it does not
turn the current failed host service into a ready-to-drive state.
