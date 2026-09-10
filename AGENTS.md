# MentorPi Repository Instructions

Before making changes in this repository:

1. Read `GEMINI.md` for the project architecture, hardware targets, and safety constraints.
2. Read `CONVERSATION_MEMORY.md` for the latest cross-session decisions, completed work, and validation status.
3. Recheck transient facts such as running containers, available Docker images, and hardware connections instead of assuming the saved checkpoint is still current.

Keep `CONVERSATION_MEMORY.md` concise and update it after material project decisions or completed deployment work. Record durable outcomes and reproducible commands, not raw chat transcripts, credentials, tokens, host addresses, or other secrets.

## Git versions and review revisions

- Create a new Git version (commit) only for a new feature or milestone.
- Fold revisions addressing code review comments into the existing commit for
  that feature or milestone by amending or squashing them; do not leave separate
  review-fix commits or create a new version for those revisions.
- Every commit must include an informational body explaining what changed and
  why, including relevant operational effects and validation. Keep that body
  current when incorporating review revisions.
