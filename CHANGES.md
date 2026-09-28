# M14.4 change inventory

Changes against M14.3 baseline `2c26015`: **20 files — 19 modified, one added,
none deleted**. Paths are repository-relative; includes this inventory.

M14.4 combines startup/acquisition into Take control, removes the checkbox and
adds browser pause recovery without rearming. Stop, faults and disconnects still
disarm. Input requires physical release, fresh neutral acknowledgment and a new press.

| Status | File | What changed and why |
| --- | --- | --- |
| Modified | `CHANGES.md` | Replaces the M14.3 inventory with every M14.4 file change, validation result and remaining robot acceptance. |
| Modified | `CONVERSATION_MEMORY.md` | Records the browser contract, restored driving tests and pending commit/deployment for the next session. |
| Modified | `docker/ubuntu_tank/README.md` | Describes the completed local protocol-2 browser flow and retains M15 deployment/physical acceptance requirements. |
| Modified | `docs/BUG_WEB_CONTROL_LEASE_EXPIRY.md` | Records local browser recovery and lost-neutral retry behavior without closing real-network or physical validation. |
| Modified | `docs/MENTORPI_WEB_CONTROL_DESIGN.md` | Marks M14.4 implemented and records the distinction between local browser validation and M15 robot acceptance. |
| Modified | `ubuntu_tank/docs/WEB_DEPENDENCY_CLOSURE.md` | Documents combined Take control, explicit Arm, physical release/neutral acknowledgment, cancellation and stale-client rejection. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/routes_ws.py` | Includes the rejected intent sequence in error frames so the browser clears only the corresponding outstanding intent. |
| Modified | `ubuntu_tank/tests/test_milestone13_browser_pwa.py` | Restores the full browser runner; supplies real lifecycle IPC and periodic health/zero/deadline simulation, with fault/startup injection and proper fixture teardown. |
| Modified | `ubuntu_tank/web/src/App.vue` | Connects bound-session and recovery readiness to the UI; clears local direction immediately on release or controller Stop. |
| Modified | `ubuntu_tank/web/src/components/DrivePanel.vue` | Disables motion until recovery is acknowledged and shows release, neutral-confirmation and new-press readiness messages. |
| Modified | `ubuntu_tank/web/src/components/ServiceControls.vue` | Removes Start controller and the tracks-raised checkbox, keeps one Take control flow and enables Arm only after binding. Stop remains available during setup. |
| Modified | `ubuntu_tank/web/src/composables/useControlSession.ts` | Coordinates bounded acquisition polling and acknowledged binding, rejects duplicate setup, cancels late responses and clears sessions after ownership loss. Arm remains explicit. |
| Modified | `ubuntu_tank/web/src/composables/useDriveInput.ts` | Preserves actual key/pointer release across pauses, gates neutral recovery and blocks presses before readiness. Focus/suspension/conflict paths still Stop. |
| Modified | `ubuntu_tank/web/src/composables/useRobotState.ts` | Requires protocol 2, fails closed when compatibility is unknown and treats INPUT_PAUSED as armed while separate readiness gates movement. |
| Modified | `ubuntu_tank/web/src/services/apiClient.ts` | Removes the obsolete Start endpoint and passes private operation tokens when polling acquisition. |
| Modified | `ubuntu_tank/web/src/services/wsClient.ts` | Bounds connect/bind, ignores superseded sockets, correlates responses and input generations, retains one pending intent and retries only fresh neutral recovery after loss. |
| Modified | `ubuntu_tank/web/src/types/ui.ts` | Removes unused tracks-raised confirmation state from the drive view model. |
| Modified | `ubuntu_tank/web/tests/browser_control.spec.ts` | Migrates driving, ownership, Arm, Stop, focus, mixed-input and hold-limit browser scenarios to protocol 2 without the checkbox. |
| Added | `ubuntu_tank/web/tests/input_recovery.spec.ts` | Adds real-browser scenarios for setup cancellation/failure/timeout, 450 ms delays, 1.1–1.5 s gaps, lost/delayed responses, fresh keyboard/touch release, faults and socket/PWA lifecycle. |
| Modified | `ubuntu_tank/web/tests/protocol_fence.spec.ts` | Verifies old acquisition contracts are rejected while the migrated browser can take control. |

The ignored `review.md` contains the independent review result and is not counted
above. Browser tests use real HTTP/WebSocket/IPC/lifecycle services with simulated
controller health and zero delivery; they do not certify physical stopping.

Validation: `./ubuntu_tank/deploy.sh test` passed 555 Python tests, including
the browser runner executing all 27 Playwright scenarios. Frontend build/type
checking, source/negative-boundary/dependency gates, Python formatting and
whitespace checks passed, with no new Python lint violations. Independent review
reports PASS. M14.4 is committed locally and undeployed; M15 robot/network
acceptance and M16 release handoff remain pending.
