#!/usr/bin/env bash
# ubuntu_tank/deploy.sh - Native Ubuntu 26.04 Tank Controller Operations & Deployment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
MentorPi Native Ubuntu Tank Controller - Deployment & Operations CLI

Usage:
  ./deploy.sh <command> [options]

Core Commands:
  help                           Display this help manual and safety guidelines
  test                           Run hardware-free unit tests and source boundary verification

Host & ROS Setup (Milestone 2):
  check-host                     Read-only verification of OS, architecture, EEPROM, and devices
  prepare-host                   Upgrade base Ubuntu packages and configure locales
  verify-lock                    Validate dependency lock integrity against versions.lock
  verify-closure                 Validate candidate packages and solver closure against versions.lock
  install-ros                    Install pinned ROS 2 Lyrical packages and build tools
  install-deps                   Resolve and verify locked rosdep dependencies

Workspace Build & Package (Milestones 3 & 5):
  build                          Build ROS 2 packages natively using colcon
  package                        Build isolated release in .work/ and emit archive in dist/

Target Operations (Milestone 5):
  install <archive>              Install versioned release under /opt/ubuntu_tank/releases/
  activate <release-id>          Atomically switch /opt/ubuntu_tank/current to release
  rollback                       Roll back to previously recorded working release
  start                          Start mentorpi-tank.service (starts disarmed)
  stop                           Disarm and stop mentorpi-tank.service
  status                         Display service health, ROS graph, and guard state
  logs                           Show recent systemd journal logs for mentorpi-tank.service

Hardware Actuation (Milestones 4 & 6):
  arm --ack-tracks-raised        Explicitly arm motor guard (requires physical safety acknowledgment)
  disarm                         Immediately disarm and send repeated zero commands
  bench --ack-tracks-raised      Run bounded bench test of forward/reverse/left/right motion

Safety Rules:
  1. Motor guard starts disarmed by default after every restart.
  2. Tracks must be physically raised clear of the surface during bench testing.
  3. Commands timeout after 250 ms of inactivity, disarming the controller.
EOF
}

cmd_test() {
  echo "============================================================"
  echo "Running Hardware-Free Test Suite"
  echo "============================================================"

  # 1. Source boundary gate
  echo ""
  echo "--> Running Source Boundary & Provenance Gate..."
  bash "${SCRIPT_DIR}/tests/test_source_boundary.sh"

  # 2. Negative boundary regression test
  echo ""
  echo "--> Running Negative Boundary Regression Tests..."
  bash "${SCRIPT_DIR}/tests/test_negative_boundary.sh"

  # 3. Dependency closure and lockfile verification gate
  echo ""
  echo "--> Running Dependency Closure & Lockfile Gate..."
  bash "${SCRIPT_DIR}/tests/test_dependency_closure.sh"

  # 4. Python module unit tests
  echo ""
  echo "--> Running ubuntu_tank_safety unit tests..."
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_safety" python3 -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_safety/test" -p "test_*.py" -v

  echo ""
  echo "--> Running ubuntu_tank_supervisor unit tests..."
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_supervisor" python3 -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_supervisor/test" -p "test_*.py" -v

  echo ""
  echo "--> Running ubuntu_tank_teleop unit tests..."
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_teleop" python3 -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_teleop/test" -p "test_*.py" -v

  echo ""
  echo "--> Running Milestone 2 installation workflow unit tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" python3 "${SCRIPT_DIR}/tests/test_install_workflow.py" -v

  echo ""
  echo "--> Running Milestone 3 Lyrical port and dependency closure unit tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" python3 "${SCRIPT_DIR}/tests/test_milestone3_port.py" -v

  echo ""
  echo "============================================================"
  echo "All Milestone 1, 2, & 3 tests PASSED successfully!"
  echo "============================================================"
}

cmd_stub() {
  local cmd="$1"
  local milestone="$2"
  echo "Command '${cmd}' is planned for ${milestone} according to MENTORPI_FRESH_CONTROLLER_DESIGN.md." >&2
  echo "Milestone 2 (Target-Pi Ubuntu and ROS installation workflow) is current." >&2
  exit 1
}

COMMAND="${1:-help}"
shift || true

case "${COMMAND}" in
  help|-h|--help)
    usage
    ;;
  test)
    cmd_test "$@"
    ;;
  check-host)
    "${SCRIPT_DIR}/scripts/check_host.sh" "$@"
    ;;
  prepare-host)
    "${SCRIPT_DIR}/scripts/install_ros2.sh" prepare-host "$@"
    ;;
  verify-lock)
    "${SCRIPT_DIR}/scripts/install_ros2.sh" verify-lock "$@"
    ;;
  verify-closure)
    "${SCRIPT_DIR}/scripts/install_ros2.sh" verify-closure "$@"
    ;;
  install-ros)
    "${SCRIPT_DIR}/scripts/install_ros2.sh" install-ros "$@"
    ;;
  install-deps)
    "${SCRIPT_DIR}/scripts/install_ros2.sh" install-deps "$@"
    ;;
  build)
    "${SCRIPT_DIR}/scripts/build_workspace.sh" "$@"
    ;;
  arm|disarm)
    cmd_stub "${COMMAND}" "Milestone 4 (Guarded bringup and safe teleop)"
    ;;
  package|install|activate|rollback|start|stop|status|logs)
    cmd_stub "${COMMAND}" "Milestone 5 (Native host deployment and operations)"
    ;;
  bench)
    cmd_stub "${COMMAND}" "Milestone 6 (Raised-track controller acceptance)"
    ;;
  *)
    echo "Unknown command: ${COMMAND}" >&2
    usage
    exit 1
    ;;
esac
