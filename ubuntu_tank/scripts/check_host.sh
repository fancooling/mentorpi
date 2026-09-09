#!/usr/bin/env bash
# check_host.sh - Read-only host, architecture, EEPROM, and device preflight checks
# Planned for Milestone 2 implementation
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/check_host.sh [options]

Performs non-mutating preflight checks:
- Verifies Ubuntu 26.04 LTS on ARM64 (Raspberry Pi 5).
- Checks Raspberry Pi bootloader EEPROM firmware date.
- Confirms absence of factory/sidecar/replacement Docker containers.
- Verifies STM32 RRC serial port (/dev/rrc) permissions and symlink.

Note: This script scaffold is defined in Milestone 1; full automated execution
is implemented in Milestone 2.
EOF
}

if [ "${1:-}" = "help" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

echo "check_host.sh: Planned for Milestone 2 (Ubuntu and ROS installation workflow)." >&2
exit 1
