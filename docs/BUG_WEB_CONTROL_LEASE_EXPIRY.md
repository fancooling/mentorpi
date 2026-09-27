# Web controls disable after brief movement; Arm does not recover

Status: fixed locally; Pi verification pending. Reported 2026-09-26.
No deployment performed.

## Environment and impact

Pi 5 running the paired Ubuntu runtime/web containers over HTTPS.
Observed release: `3ac501e52612b5b2a5b0352e5f5285e5fa6481dd304d6a4add7a75ae57c64b05`.
The owner reports that even one direction press and release can leave all
direction buttons disabled. Repeated Arm attempts do not reliably recover.
This blocks ordinary web teleoperation.

## Reproduction on the robot

Run only with tracks raised and the required physical safety acknowledgment.

1. Start the controller, take control, confirm tracks raised, and Arm.
2. Press and release a direction button; repeat if necessary.
3. Observe the direction buttons becoming grey.
4. Click Arm again and observe whether controls recover and remain usable.

Expected: releasing a direction stops movement and leaves the controller armed.
A genuine communication timeout must stop and disarm; explicit recovery must
restore a usable control session without resuming an old held direction.

Actual: the operator lease expires, the controller disarms, and rearming alone
can leave the browser unable to renew its lease.

## Evidence

Read-only requests to `/api/v1/status` during diagnosis returned:

```json
{
  "service_state": "active",
  "operator_state": "OWNED_DISARMED",
  "guard_armed": false,
  "disarm_pending": false,
  "last_fault": "Input lease expired (> 150 ms); authority expired"
}
```

The owner remained assigned. Battery readings were approximately 12.2 V and
guard telemetry was fresh. `/api/v1/logs` showed repeated Arm/disarm events.
These observations establish a lease fault, not its initial timing cause.
No diagnostic motor commands were issued.

## Confirmed recovery defect

In the deployed version of `ubuntu_tank/web/src/services/wsClient.ts`, a
`LEASE_EXPIRED` error sets
`isBound` to false. `handleChallenge()` then ignores all further challenges.
In `useControlSession.ts`, Arm updates the epoch but does not restore or replace
that binding. The socket can remain connected while lease responses stop.

A motor-free reproduction used the actual TypeScript client with an in-memory
WebSocket substitute:

1. Connect, bind, acknowledge the bind, and deliver a challenge: one intent sent.
2. Deliver `LEASE_EXPIRED`, update the epoch as Arm does, and deliver another
   challenge: still only one intent sent; connected=true, bound=false.

This confirms the client defect. No browser frame capture was obtained from
the owner's session, so this exact error sequence is not confirmed for every
observed failed Arm attempt.

## Timing investigation still needed

The protocol uses a 150 ms lease and a nominal 50 ms challenge interval.
The lease deadline is measured from challenge creation on the Pi, not response
arrival. Network transit and processing consume that window. The deployed web relay
also waits for IPC work before sleeping for the next challenge interval.
Lease expiry is enforced in both `DRIVING` and `ARMED_IDLE`.

Pointer release selects neutral input; the operator state machine handles
neutral by entering `ARMED_IDLE`, not by intentionally disarming.

Measure challenge issue, browser receipt/response, IPC completion, and renewal
timing to distinguish network delay, browser scheduling, and server contention.
The current evidence does not establish that increasing the lease is necessary
or sufficient. Any increase changes the allowed motion duration after lost
input and requires renewed stop-latency validation.

## Fix acceptance

- Add a behavioral regression for expiry followed by explicit recovery; verify
  challenge responses resume and old held input cannot resume movement.
- Repeated direction press/release cycles remain usable and return to armed
  neutral when communication is healthy.
- A lost connection, stalled browser, stale response, or expired lease still
  stops and disarms within the documented bounds.
- Verify on the Pi with tracks raised; local tests alone do not certify motor
  stopping behavior. Record timing evidence before changing lease limits.

Temporary recovery to try: Release control, Take control, confirm tracks raised,
then Arm. This creates a new binding but does not address the initial timeout;
it has not been verified on the robot during this diagnosis.


## Local correction

- Keep the owner binding after `LEASE_EXPIRED`; clear pending intent and retain
  the existing input reset and Stop behavior. Only `NOT_OWNER` invalidates the
  binding. Explicit Arm can then renew authority on the same connection.
- Subtract challenge processing time from the 50 ms relay interval. After an
  overrun, yield at least 10 ms to incoming intents and Stop requests instead
  of issuing catch-up challenges that contend for the IPC lock.
- Keep the 150 ms lease and all motion-safety deadlines unchanged.

A real Unix-socket/WebSocket regression with 60 ms injected challenge-delivery
latency fails with `LEASE_EXPIRED` on the original relay and passes with the
correction. The browser regression injects `LEASE_EXPIRED` during a held key,
then verifies disarming, explicit Arm recovery, neutral renewal while the old
key remains held, and forward/reverse press-and-release cycles. All 13 browser
tests pass locally. The injected delay is a regression scenario, not a measured
cause of the owner's Pi failure. Repeat the workflow on the deployed Pi before
closing the report.
