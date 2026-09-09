#!/usr/bin/env bash
# build_workspace.sh - rosdep resolution and colcon build script template
# Planned for Milestone 3 implementation
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/build_workspace.sh [options]

Resolves locked dependencies with rosdep and builds workspace packages natively
with colcon for ROS 2 Lyrical.

Note: This script scaffold is defined in Milestone 1; full automated execution
is implemented in Milestone 3.
EOF
}

if [ "${1:-}" = "help" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

echo "build_workspace.sh: Planned for Milestone 3 (Lyrical port and dependency closure)." >&2
exit 1
