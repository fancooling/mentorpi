#!/usr/bin/env bash
# install_ros2.sh - Ubuntu and ROS repository/package setup script template
# Planned for Milestone 2 implementation
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/install_ros2.sh [options]

Prepares a clean Ubuntu 26.04 installation and installs pinned ROS 2 Lyrical
packages according to versions.lock.

Note: This script scaffold is defined in Milestone 1; full automated execution
is implemented in Milestone 2.
EOF
}

if [ "${1:-}" = "help" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

echo "install_ros2.sh: Planned for Milestone 2 (Ubuntu and ROS installation workflow)." >&2
exit 1
