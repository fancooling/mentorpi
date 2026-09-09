#!/usr/bin/env bash
# verify_runtime.sh - Live ROS graph, topic ownership, and guard state verification
# Planned for Milestone 4 & 6 implementation
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/verify_runtime.sh [options]

Verifies running controller health:
- Confirms /ubuntu_tank_safety/motor_guard is running and disarmed by default.
- Verifies /ubuntu_tank_supervisor is actively reporting systemd health.
- Verifies /dev/rrc serial bridge connection and topic ownership.

Note: This script scaffold is defined in Milestone 1; full automated execution
is implemented in Milestones 4 and 6.
EOF
}

if [ "${1:-}" = "help" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

echo "verify_runtime.sh: Planned for Milestone 4 (Guarded bringup and safe teleop)." >&2
exit 1
