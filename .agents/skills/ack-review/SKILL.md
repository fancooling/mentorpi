---
name: ack-review
description: >-
  Evaluate code review comments from review.md against the personal Pi 5 single-owner
  system context, systematically implement required fixes and regression tests for
  serious system failures and safety bugs, verify the test suite and source boundaries,
  and fold revisions into the existing feature commit via git commit --amend.
---

# Acknowledge and Remediate Code Review (`ack-review`)

Use this skill whenever addressing code review findings recorded in `review.md`,
or when instructed to fix review comments and fold them into an existing Git commit.

## Workflow Overview

Per repository policy in `AGENTS.md`, `GEMINI.md`, and `.agents/rules/git_versioning.md`:
1. Never create separate review-fix commits.
2. Fold all review remediations into the existing feature or milestone commit via `git commit --amend`.
3. Keep the commit message's informational body updated with what changed, why, and validation evidence, without appending or enumerating code review comments.

---

## Step-by-Step Procedure

### 1. Locate and Parse `review.md`
- Read `review.md` in the workspace root.
- Parse each finding and extract:
  - **Severity / Priority**: P1 (blocker / safety violation / runtime failure), P2 (correctness / reliability / operational hazard), P3 (maintainability / documentation / cleanliness).
  - **Affected Files & Lines**: Specific paths and line references.
  - **Reviewed Commit**: Compare with `git rev-parse --short HEAD`.
  - **Root Cause & Rationale**: The exact mechanism triggering the issue.
  - **Required Correction**: Prescribed fix requirements and boundary constraints.
  - **Verification Requirements**: Specific conditions and tests to confirm resolution.

### 2. Triage & Practical Impact Assessment (Personal Pi 5 Context)
Before implementing code changes, critically evaluate each finding against the project's actual operational environment: **a personal, single-owner robot running entirely on a fully controlled Raspberry Pi 5 board where all installed software and user accounts are owner-controlled on a trusted local network**.

Distinguish between serious system-failure bugs and minor/theoretical issues:

- **Serious Bugs & System Failures (Must Remediate)**:
  - **Runtime Failures & Crash Loops**: Issues that break service startup or cause crash loops on the target Pi 5 (e.g., missing system Python packages in `versions.lock`, permission errors under systemd confinement, unhandled exceptions in background daemons).
  - **Physical Motion Safety Violations**: Loss of fail-closed guarantees, unconfirmed stops falsely reported as inactive/stopped, emergency Stop blocking behind slow operations, broken lease expirations, or failure to command four-motor zero velocity on disconnect/fault.
  - **Deadlocks & Communication Breakdown**: Frame interleaving on shared IPC streams, blocking the single Uvicorn event loop on synchronous I/O, or priority inversions.
  - *Action*: Implement comprehensive architectural remediations and add automated regression tests.

- **Minor or Theoretical Issues Unlikely to Occur (Triage & Prune)**:
  - **Hostile Multi-Tenant Threat Models**: Findings assuming malicious local users or adversarial same-UID processes on the Pi. Per `GEMINI.md`, the owner controls the OS, services, and accounts; local PID/UID checks exist only to catch accidental misconfiguration, not hostile same-user isolation.
  - **Over-Engineered Defensive Scaffolding**: Extreme synthetic edge cases or defensive layers for multi-user/untrusted environments that cannot arise in this single-owner setup.
  - **Cosmetic Nitpicks & Premature Generalization**: Complex architectural shifts proposed for purely theoretical or negligible edge cases that add bloat without improving safety or stability.
  - *Action*: Document why the finding is low-risk or inapplicable given the single-owner Pi 5 context, or adopt a minimal pragmatic resolution rather than adding unnecessary complexity.

- **Consultation on Ambiguity (Ask the User First)**:
  - If you are ever unsure whether a finding represents a genuine operational hazard or a negligible edge case under this single-owner model, **always stop and consult the user before taking action**.
  - Present the finding clearly in plain language, explain the practical risk vs added complexity, and ask the user how they wish to proceed rather than making assumptions or writing unnecessary code.

### 3. Architecture & Safety Invariant Check
Before modifying any code, verify proposed changes against repository architectural rules:
- **Vendor Mode vs Native Mode**:
  - Sidecar (`MentorPiFan` on port 8081): Nginx + static assets only; no ROS, drivers, controllers, teleop, or `/dev` access.
  - Native Mode (`/opt/ubuntu_tank`): Dedicated service is sole `/dev/rrc` owner; guarded pipeline (`/controller/cmd_vel` -> `controller` -> `/ubuntu_tank_safety/motor_input` -> `motor_guard` -> `/ros_robot_controller/set_motor_guarded` -> `bridge`).
- **Fail-Closed Guarantees**:
  - On any fault, crash, timeout, or signal, output repeated 4-motor zero commands.
  - Timeouts and leases must strictly use monotonic clock (`time.monotonic()`), never wall clock or ROS sim time.
- **SROS2 / DDS Security Access Control**:
  - Enforce least privilege with default `DENY`.
  - Standard discovery (`ros_discovery_info`) must be explicitly permitted and encrypted.
  - Node infrastructure endpoints (parameter events, parameter services, type description services) must be bounded or disabled (`start_parameter_services=False`).
  - Do not use broad wildcards (`*`, `rt/*`, `rq/*`, `rr/*`) in permission documents.
  - Status enclaves must remain strictly read-only for application motion/arming.

### 4. Implement Remediations & Regression Tests
- Make necessary edits to source files, configuration files, XML permissions, and scripts.
- **Always add regression tests**:
  - Add test cases in the relevant test suite (e.g., `ubuntu_tank/tests/test_milestone4_bringup.py` or `ubuntu_tank/tests/test_rmw_integration.py`).
  - Test cases must specifically assert the failure condition and verify the fix.
- **Update File Documentation**:
  - Keep purpose/rationale in top-of-file documentation. For vendor-derived files, identify the original source and meaningful adaptations; preserve copyright/license notices. Git records revisions; no source inventory or per-file hashes are required.
  - Run `ubuntu_tank/tests/test_source_boundary.sh` to verify layout, imports, dependency declarations, and controller-only scope.

### 5. Execute Full Verification Suite
Run the full regression test suite:
```bash
./ubuntu_tank/deploy.sh test
```
Verify:
- 100% test pass rate across all unit, integration, and source-boundary tests.
- Run code formatting with `.venv/bin/ruff format <files>` across modified authored Python files (avoid unrelated vendor-code reformatting).
- Run code formatting with `.venv/bin/shfmt -i 2 -ci -w <files>` and linting with `.venv/bin/shellcheck <files>` across modified authored shell scripts.
- Zero whitespace errors:
  ```bash
  git diff --check
  ```
- If material architectural decisions were made, update `CONVERSATION_MEMORY.md`.

### 6. Fold Revisions into Existing Git Commit
- Check working tree status:
  ```bash
  git status
  ```
- Stage all changes:
  ```bash
  git add -A
  git diff --cached --check
  ```
- Amend the existing commit:
  ```bash
  git commit --amend
  ```
- Update the informational commit body:
  - Keep the overall description of what changed, why, and operational effects accurate and current.
  - Do not append, list, or explicitly call out addressed code review comments; describe fixes naturally as part of the overall feature/milestone implementation.
  - Record the updated test suite results and validation counts.

### 7. Verify and Report
- Verify `git status` is clean.
- Verify `git log -n 1` shows the expected commit message and parent.
- Provide a structured response to the user detailing each finding addressed, key changes, and verification results.
