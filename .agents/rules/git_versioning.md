# Git Versions and Review Revisions Rule

## Git Versioning Policy

- Create a new Git version (commit) only for a new feature or milestone.
- Fold revisions addressing code review comments into the existing commit for
  that feature or milestone by amending or squashing them; do not leave separate
  review-fix commits or create a new version for those revisions.
- Always run code formatting and linting before each commit: format modified
  authored Python code using `.venv/bin/ruff format <files>`, format modified
  authored shell scripts using `.venv/bin/shfmt -i 2 -ci -w <files>` and verify
  with `.venv/bin/shellcheck <files>`, ensure `git diff --check` reports zero
  whitespace or formatting errors. Keep file-level purpose/rationale and vendor
  origin/adaptation documentation current; Git records source revisions.
- Preserve vendor copyright/license notices and avoid unrelated vendor-code reformatting.
- Every commit must include an informational body explaining what changed and
  why, including relevant operational effects and validation. Keep that body
  current when incorporating review revisions.
