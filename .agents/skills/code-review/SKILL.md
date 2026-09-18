---
name: code-review
description: >-
  Review only committed and uncommitted Git changes since the latest pushed baseline
  for critical runtime failures and logic bugs caused by the diff. Never report untouched-code
  defects, style, optimization, or architecture concerns unless explicitly requested.
  Always overwrite the repository-root review.md with actionable findings or exactly PASS.
  Use for reviews and re-reviews, not implementation.
---

# Code Review (`code-review`)

Review the combined committed and uncommitted changes since the latest pushed
baseline, unless the user explicitly requests another scope. Review only the
baseline-to-local diff and report only substantiated critical runtime failures or
logic bugs caused or materially worsened by those changes. Prioritize whether the
intended user workflows actually work.

---

## Personal-Project Review Bar

For personal, single-owner projects such as MentorPi, assume the owner trusts
local users and owner-approved software unless an explicit threat model says
otherwise. Focus on build/install/startup failures, incorrect behavior, broken
operator workflows, crashes, hangs, data loss, and failed recovery or rollback.

- **Relaxed bar for security and authentication**: Do not report speculative
  attacker scenarios, enterprise hardening, role-isolation preferences,
  least-privilege deviations, credential-storage preferences, or missing
  authentication alone as critical functional defects.
- **Workflow-breaking security issues**: Report authentication or security
  configuration when it concretely breaks an intended workflow, such as
  preventing service startup or blocking the authorized operator. Explain the
  functional trigger and impact.
- **Exploitable security issues**: Report exploitable security issues only when
  they have a demonstrated material impact within the project's actual exposure
  and trusted-owner assumptions, or when the user explicitly requests a
  security-focused review.
- **Physical safety and data integrity**: Unintended motion, ineffective stops,
  stale commands, serial-loss handling, consequential races, and destructive
  rollback remain strictly in scope.
- **Relaxed bar for human-timing races**: Assume normal human input is much
  slower than computer execution and that the owner can retry; do not report a
  merely theoretical human-timing race unless it causes irreversible harm or a
  critical safety failure under a credible normal workflow. Continue to examine
  races between two software components, processes, callbacks, or threads
  because those can interleave at machine speed without a human pacing the
  operations.
- **Design discrepancies**: Treat security-related design discrepancies as
  findings only when they meet this impact bar; a stricter checklist by itself
  is insufficient.

---

## Select the Baseline and Review Scope

1. **Read Repository Guidance**:
   - Read applicable repository instructions (`GEMINI.md`, `AGENTS.md`) and
     relevant architecture, design, and requirements documents.
   - Honor an explicit baseline, range, commit, or file scope specified by the
     user; otherwise, review every local change since this branch's latest pushed
     baseline, including unpushed commits, index/worktree edits or deletions, and
     relevant non-ignored untracked source, configuration, and test files.

2. **Resolve Baseline from Local Refs**:
   - Prefer `@{push}` and use `@{upstream}` only for the intended push destination.
   - Do not fetch or assume `origin/main`, `HEAD^`, or the latest local commit.
   - If the tracking tip is not an ancestor of `HEAD`, use a merge base only when
     the intended history is clear and exclude remote-only changes.
   - Ask the user when the baseline, divergence, or rewritten history is
     ambiguous; never guess or claim `PASS`. State that remote state is locally
     recorded and disclose any merge-base fallback.

3. **Record Diffs and Status**:
   - Record the baseline and HEAD SHAs, Git status, and reviewed diff using
     `run_command`:
     ```bash
     git rev-parse @{push}
     git rev-parse HEAD
     git status
     git diff @{push}
     ```
   - Inspect staged and unstaged layers and non-ignored untracked files,
     treating relevant untracked files as additions. Exclude `review.md` and
     unrelated transient or generated files, but retain behavior-affecting
     tracked generated changes.
   - Review the resulting file contents using `view_file`, not only Git diffs or
     the index.

4. **Preserve Working Tree & Index**:
   - Do not stage, stash, reset, clean, commit, amend, push, or rewrite history.
   - If isolation is needed, snapshot the reviewed contents into temporary scratch
     files.

5. **Diff Attribution**:
   - Limit findings to defects caused or materially worsened by the scoped diff.
   - Inspect unchanged code only for context, tie every finding to the responsible
     changed lines, and do not broaden the review into an unrelated repository audit.

---

## Independent Review and Validation via Antigravity Subagent

To guarantee objectivity, execute the review in an independent context:

1. **Invoke Clean Subagent (`invoke_subagent`)**:
   - In Antigravity, invoke a separate reviewer subagent with fresh context:
     ```json
     {
       "TypeName": "self",
       "Role": "Independent Code Reviewer",
       "Prompt": "<prompt>"
     }
     ```
   - Provide **only** the requested scope and repository material:
     - Baseline SHA and reviewed commit/diff range.
     - Applicable repository instructions (`GEMINI.md`, `AGENTS.md`).
     - Relevant architecture and design documents.
     - The criteria defined in this skill.
     - Current committed/uncommitted contents and combined diff.
   - **Do not** provide conversational history, earlier `review.md` contents,
     suggested findings, or prior conclusions to the reviewer subagent.
   - Allow the subagent to complete asynchronously; Antigravity will notify you
     when it finishes (no polling needed).

2. **Verify and Organize Findings**:
   - Have the reviewer return its assessment without editing implementation.
   - The primary agent verifies each finding against the codebase and diff.
   - Trace concrete triggers, runtime behavior, and impact.
   - Verify dependency-sensitive claims against pinned APIs (`versions.lock`,
     virtual environment packages) or authoritative vendor disk sources before
     treating them as findings.
   - Run focused, hardware-free checks using the repository virtual environment
     (`.venv/bin/python`, `.venv/bin/ruff`, etc.). Distinguish actual runtime
     evidence from mocks, static inspection, and environment-induced test
     failures. Do not operate hardware, deploy, or install dependencies merely
     to conduct this review.

3. **Exclude Non-Qualifying Items**:
   - Exclude style, formatting, naming, documentation quality, optimization,
     scalability, architecture preferences, refactoring suggestions, minor
     robustness improvements, generic missing-test complaints, deferred physical
     acceptance, and speculative risks without a credible trigger.
   - Discuss architecture or performance only when the user explicitly requests it.
   - A missing test belongs in a finding only when it directly reproduces or
     prevents an identified critical runtime failure or logic bug.

4. **Re-Reviews**:
   - Reassess the current combined local state against the selected baseline on
     every re-review.
   - Remove resolved and lower-severity findings rather than carrying forward an
     old report.

---

## Overwrite `review.md`

Always replace the entire repository-root `review.md` using `write_to_file`
(with `Overwrite: true`), even when it is ignored by Git or already contains an
earlier review. Do not append or stage it.

### If No Qualifying Issues Remain:
Write exactly these four bytes to `review.md`, with no heading, explanation,
punctuation, or trailing newline:

```text
PASS
```

For a review-only request, the final response to the user must also be exactly
`PASS` when no qualifying issues remain, subject to higher-priority requirements.

### If Actionable Issues Exist:
Write only actionable critical runtime or logic findings caused by the reviewed
diff to `review.md`. For each finding:

1. **Title & Priority**:
   - Clearly label severity (`[P1]`, `[P2]`, etc.) and a descriptive title.
2. **Affected Files / Lines**:
   - Identify the affected file and narrow line range in the reviewed local state
     (or selected commit for a committed-only review).
3. **What breaks if left unfixed**:
   - Plain language explanation of practical consequences.
   - Describe the normal user action or operating condition that triggers the defect.
   - Expected result vs actual failure.
   - Step-by-step causal steps connecting the changed code to the broken workflow.
4. **Concrete Impact**:
   - What the user observes (e.g. startup failure, crash, data loss, runaway motion).
   - Available recovery or workarounds supported by evidence.
   - Avoid vague claims ("may cause issues") and do not assume downstream safeguards fail.
5. **Supporting Evidence**:
   - Distinguish source-based analysis from executed test reproductions.
   6. **Required Correction & Verification**:
   - Prescribe the minimal fix requirement and boundary constraints.
   - Focused, reproducible verification procedure or regression test.

Do not mix lower-priority observations into the failure report. Briefly direct
the user to `review.md` in the final response. Inspect the completed file and
confirm the baseline, HEAD, and Git status before finishing.

