# M15 live acceptance — October 2, 2026

Status: complete within the owner-approved scope. The owner confirmed Windows
certificate trust works, closing the final scoped check. This report separates
installed behavior, owner observations, exclusions and waived measurements. It
does not certify instrumented physical stopping or on-ground use.

## Scope and identity

The owner confirmed raised, clear tracks and presence for motor/fault testing.
The owner selected Wi-Fi and waived fault-to-track-stop testing. Physical timing
and stopping-distance evidence are therefore waived, not passed. Ethernet is
outside this session. The owner narrowed client acceptance to Windows Chrome.
Android/iOS browser and PWA tests are out of scope; separate mobile client apps
are planned for future work. The owner reported all requested Windows Chrome
checks passed using version `153.0.8010.53` (Official Build, 64-bit). The Windows
OS version was not supplied. Mobile tests are excluded, not passed. The owner
also removed actual Wi-Fi loss, power-off/cold-boot and resource-exhaustion tests
from the remaining M15 plan; none is counted as passed. Serial disconnect/reconnect
testing was subsequently excluded by owner instruction as well.

- Source: `73748c6ff2810d2941a737b3bad5b072dfef0804` (focus-retention fix).
- Release: `268674c59248a738ccf46242c5e15aac9b80b00113f8dc4bc3dcbf9b5ee0c69f`.
- Protocol: `3.0.0`; lease/hold/armed-idle/ownership limits: 1/5/30/300 seconds.
- Automated client: Linux headless Chromium `149.0.7827.55`, deployed HTTPS UI.
  The test context bypassed certificate verification; it does not prove client trust.
- Pi Wi-Fi snapshot: 5745 MHz, −29 dBm, transmit rate 433.3 Mbit/s.
  Ethernet had no carrier. These are observations, not a network reliability claim.
- Raw artifacts and temporary live drivers: `ubuntu_tank/.work/m15-20261003/`.
  `identity.json` binds release, image digests, configuration hashes and boot ID.
  These ignored files are local evidence, not a shipped acceptance command.

## Confirmed installed behavior

- Owner-confirmed Windows Chrome checks passed: refreshed page, Take/Start,
  four directions with buttons and W/A/S/D, stopping on direction release,
  Space stop/disarm, tab switching stopping motion, return requiring Start and
  a fresh press, and Release stopping the controller. These are manual
  observations, not instrumented timing. See `owner-observations.json`.
- The installed release already contained the focus fix. Both containers became
  healthy and a fresh `install.py target-test` passed, controller inactive and
  ownerless, before testing.
- Take control started the controller without arming.
- Synthetic blur and hidden/resume events in the deployed browser disarmed while
  retaining ownership. Resume required explicit Start. This exercised the real
  Pi/API/controller. The later owner-confirmed Windows Chrome test also passed.
- Armed idle disarmed after approximately 30 seconds while retaining ownership.
- Release confirmed controller shutdown. A later Take restarted it disarmed.
- The installed 300-second ownership timeout passed under continuous browser
  polling. Space reset the countdown; polling did not. At the first post-expiry
  observation (about 303 seconds), controller inactive, disarmed, `NO_OWNER` and
  `CONTROL_IDLE_TIMEOUT` agreed with the browser. Fresh Take restarted disarmed.
- Ten injected Wi-Fi input-gap/recovery cycles passed with one Start request.
  Each entered `INPUT_PAUSED`, retained Arm, rejected a replayed stale motion
  command, and required input release, acknowledged neutral and a fresh press.
  The owner confirmed stopping between pulses and no unexpected movement.
  Stop during recovery passed; final Release stopped the controller.
- The recovery trace contains 141 intent acknowledgments: client-proxy median
  9 ms, maximum 349 ms. Pi challenge timestamps and client arrival/send/ack events
  are retained. Clocks were not calibrated; these are not physical-stop times.
- Sustained input loss reached disarmed state after about five seconds and
  stayed disarmed when delivery returned. This does not establish that the
  30-second armed-idle timer caused that disarm.
- The web-container pause was injected while driving. HTTPS became unavailable;
  the paused pair was stopped and recreated without resuming queued control.
  Fresh stopped integration passed. Physical observation is recorded separately.

## Failures and limitations

- The Pi booted with a July clock before time synchronization. TLS correctly
  rejected the certificate, and the container service stayed failed after the
  clock synchronized. Starting the installed service restored operation without
  changing certificates. Automatic cold-boot recovery remains unresolved.
- An early Take was cancelled with `STALE_TRANSACTION`. After the owner closed
  other robot-control pages, acquisition passed. The cancellation source was not
  conclusively identified; observer focus-loss Stop is a possible cause.
- Immediate Take after Release once failed with “Runtime safety progress
  unavailable.” A later attempt passed. The transient readiness rejection remains
  recorded. The later fix and 20-cycle retest are documented below.
- Test-driver failures included incorrect accessible selectors, a closed idle HTTP
  connection and Chromium `ERR_NETWORK_CHANGED` during initial navigation.
  Corrected attempts retain their separate logs; failed attempts are not passes.

## Fault campaign

All nine cases below were injected after the API reported `DRIVING`. Each pair
was subsequently stopped, restored through the installed host workflow, and
passed fresh stopped integration. The owner subsequently confirmed observing
correct stopping in every container/process and browser-fault case. Those are
physical observations, not measured stopping times.

| Fault | Observed software result before restoration |
|---|---|
| Web pause / kill | HTTPS unavailable; runtime remained present |
| Operator hang / kill | Web reported agent unavailable; runtime restarted |
| Guard / bridge kill | Controller inactive and ownerless; disarm unconfirmed in last telemetry |
| Supervisor hang | Runtime failure path exercised; stopped restoration passed |
| Whole-runtime pause | Runtime remained paused; web reported agent timeout |
| Host service stop | Both containers removed |

Browser network loss was injected at Chromium's network interface; it does not
prove loss of the Pi radio or access point. Browser offline and browser crash
produced controller inactive, disarmed and ownerless through independent HTTPS
observation. Tab close left the controller active, disarmed and ownerless;
subsequent host restoration stopped it. Stopped integration passed after each case.

The first tab-close observer context closed with the tab. The first browser-crash
attempt lost its completion callback; an independent host Stop removed both
containers. Both were repeated with an independent observer and bounded crash
wait. The earlier incomplete attempts remain in the logs, not counted as passes.

## Scoped completion

Windows Chrome controls, tab/resume, immediate reacquisition and actual automated
application-update checks passed. Following Pi certificate rotation to include
the owner's LAN DNS name, same-release deployment, stopped target integration
and certificate-verified HTTPS passed. The owner subsequently confirmed Windows
certificate trust works and requested M15 closure. All retained M15 checks are
complete; physical stopping observations for the fault cases are owner-confirmed.

Android/iOS browser and PWA acceptance, serial disconnect/reconnect, actual Wi-Fi
loss, power-off/cold-boot and resource-exhaustion tests remain excluded by owner
instruction. Physical stopping time and distance remain waived, not passed.
The observed boot-time clock failure remains unresolved. This scoped completion
does not establish full instrumented certification or complete M16 release handoff.

## Final automated-session state

At 03:19 UTC on October 3 (October 2 local), fresh stopped integration passed.
Both containers were healthy; HTTPS reported controller `inactive`, `NO_OWNER`,
no active owner, zero requested speeds and no pending disarm. Guard telemetry was
null because the controller was stopped, not a fresh physical-rest measurement.
The release and installed limits were unchanged. See `final-state.txt`.

In that initial session, no production code, installed configuration, certificate
or calibration changed. The later runtime fix is recorded below.
Temporary live drivers were independently reviewed before use; documentation
claims were checked against the retained evidence. `git diff --check` passed.
No commit or push was made. After the owner completed Windows Chrome testing,
a fresh HTTPS check confirmed the same release, controller inactive, `NO_OWNER`,
no active owner, `guard_armed: false`, zero requested speeds, no pending disarm,
no last fault and `EXPLICIT_RELEASE`. These are reported software values; telemetry
ages reflect the stopped controller. See `windows-final-state.json`.

## Remaining-test continuation

The owner excluded serial disconnect/reconnect testing and confirmed correct
stopping for every previously run fault case. A real same-release redeployment
was killed immediately after it closed admission. The HTTPS acquisition API
returned `DEPLOYMENT_BUSY`; normal deployment then restored stopped integration.
This passes that interruption checkpoint, not every possible failure point.
Evidence: `interrupted-deploy.log`.

The immediate-restart regression reproduced a race with old controller cleanup.
A local fix waits for the safety monitor's explicit cleanup acknowledgement and
fresh progress before granting a new permit. Stop remains able to cancel the
wait. All 14 real-process supervision tests, the full development suite
(including 45 browser scenarios), ARM64 smoke tests and independent review passed.
Deployment and the 20-cycle live retest subsequently passed.

The fixed paired images deployed as release
`4b320e602524306678496ec7a008e0e454f6d81a66301984f6d7518df92928db`.
This build includes the uncommitted runtime fix on source `73748c6`; the source
revision alone does not identify that fix. Stopped Pi integration passed.

An isolated Chromium 149 browser loaded the older `d683078e` release and its
service worker before deployment. After deployment, Update Now appeared. With
control owned but disarmed, clicking it released control and activated the new
worker. The new release reported inactive, disarmed and ownerless. Service-worker
hashes changed, and no Arm or direction command was sent. A pinned-certificate
exception enabled this automated check; it does not establish Windows trust.
Evidence: `browser-update.jsonl` and `fix-deploy.log`.

The first live reacquisition run passed 14 immediate Take/Release cycles, then
the browser displayed “Failed to fetch” on the next Take. HTTPS observation
showed the controller inactive, disarmed and ownerless. The driver stopped the
pair, then normal deployment and stopped integration restored it. This was not
the original runtime-readiness error, but the incomplete run is not a 20-cycle
pass. The repeat records failed network requests and command responses.

The instrumented repeat passed all 20 consecutive Take/Release cycles on the
fixed release without arming or driving. Every Take reached active/disarmed;
every Release reached inactive/disarmed/ownerless before the next immediate Take.
Chromium also recorded `ERR_NETWORK_CHANGED` requests during the successful run;
this result resolves the runtime-readiness regression, not general network
reliability. Evidence: `reacquire.jsonl` (`PASS_IMMEDIATE_REACQUISITION`).

After the repeat, fresh installed `target-test` passed and both containers were
healthy. HTTPS verified using the exported public certificate reported the fixed
release inactive, disarmed and ownerless, with no pending disarm or last fault.
Evidence: `fix-final-state.txt` and `fix-final-status.json`. Windows trust was
still unresolved at that point; the later certificate rotation and owner
confirmation recorded above close it. No commit or push was made during live
testing. The runtime fix and acceptance evidence were subsequently committed
in `2c5bb5d`.

## Release handoff index

[M16 handoff](M16_RELEASE_HANDOFF.md) binds the fixed accepted artifact to committed
source and [retained evidence checksums](M16_EVIDENCE_INDEX.json), documents current
recovery and records the later read-only Pi status. It preserves all M15 exclusions
and waivers. The later failed-service observation does not replace the historical
acceptance results or claim a fresh stopped-integration pass.
