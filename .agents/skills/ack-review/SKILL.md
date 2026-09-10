---
name: ack-review
description: >-
  Read code review comments from review.md, systematically implement the required
  fixes and regression tests, verify the test suite and source provenance, and fold
  the revisions into the existing feature commit via git commit --amend.
---

# Acknowledge and Remediate Code Review (`ack-review`)

Use this skill whenever addressing code review findings recorded in `review.md`,
or when instructed to fix review comments and fold them into an existing Git commit.

## Workflow Overview

Per repository policy in `AGENTS.md`, `GEMINI.md`, and `.agents/rules/git_versioning.md`:
1. Never create separate review-fix commits.
2. Fold all review remediations into the existing feature or milestone commit via `git commit --amend`.
3. Keep the commit message's informational body updated with what changed, why, and validation evidence.

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

### 2. Architecture & Safety Invariant Check
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

### 3. Implement Remediations & Regression Tests
- Make necessary edits to source files, configuration files, XML permissions, and scripts.
- **Always add regression tests**:
  - Add test cases in the relevant test suite (e.g., `ubuntu_tank/tests/test_milestone4_bringup.py` or `ubuntu_tank/tests/test_rmw_integration.py`).
  - Test cases must specifically assert the failure condition and verify the fix.
- **Update Source Manifest**:
  - If any file under `ubuntu_tank/` is created, modified, or deleted, update `ubuntu_tank/source-manifest.txt` with new SHA-256 hashes and accurate file counts.
  - Run `ubuntu_tank/tests/test_source_boundary.sh` to ensure 100% manifest and provenance compliance.

### 4. Execute Full Verification Suite
Run the full regression test suite:
```bash
./ubuntu_tank/deploy.sh test
```
Verify:
- 100% test pass rate across all unit, integration, and provenance tests.
- Zero whitespace errors:
  ```bash
  git diff --check
  ```
- If material architectural decisions were made, update `CONVERSATION_MEMORY.md`.

### 5. Fold Revisions into Existing Git Commit
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
  - Summarize the newly addressed review findings and why changes were made.
  - List the modified/added components.
  - Record the updated test suite results and validation counts.

### 6. Verify and Report
- Verify `git status` is clean.
- Verify `git log -n 1` shows the expected commit message and parent.
- Provide a structured response to the user detailing each finding addressed, key changes, and verification results.
