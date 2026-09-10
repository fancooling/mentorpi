# Git Versions and Review Revisions Rule

## Git Versioning Policy

- Create a new Git version (commit) only for a new feature or milestone.
- Fold revisions addressing code review comments into the existing commit for
  that feature or milestone by amending or squashing them; do not leave separate
  review-fix commits or create a new version for those revisions.
- Every commit must include an informational body explaining what changed and
  why, including relevant operational effects and validation. Keep that body
  current when incorporating review revisions.
