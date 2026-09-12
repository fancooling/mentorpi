#!/usr/bin/env bash
# recover_activation.sh - Boot-time write-ahead transaction recovery runner
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/recover_activation.sh [options]

Replays or reconciles incomplete release activation transactions using the
journal under /var/opt/ubuntu_tank/deployment/activation-journal before
starting the controller service.

Options:
  -h, --help               Display this help text and exit
  --opt-dir <dir>          Path to /opt/ubuntu_tank directory
  --etc-dir <dir>          Path to /etc/opt/ubuntu_tank directory
  --var-dir <dir>          Path to /var/opt/ubuntu_tank directory
  --lock-path <path>       Path to deployment lock file
EOF
}

if [ "${1:-}" = "help" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

PYTHONPATH="${WORKSPACE_ROOT}" exec python3 "${SCRIPT_DIR}/deployment_manager.py" recover "$@"
