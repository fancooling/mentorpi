#!/usr/bin/env bash
# ubuntu_tank/deploy.sh - Native Ubuntu 26.04 Tank Controller Operations & Deployment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

py_bin="python3"
if [ -x "${WORKSPACE_ROOT}/.venv/bin/python" ]; then
  py_bin="${WORKSPACE_ROOT}/.venv/bin/python"
fi

usage() {
  cat <<'EOF'
MentorPi Native Ubuntu Tank Controller - Deployment & Operations CLI

Usage:
  ./deploy.sh <command> [options]

Core Commands:
  help                           Display this help manual and safety guidelines
  test                           Run hardware-free unit tests and source boundary verification
  target-test                    Verify a built/installed Pi release; optional prebuilt deployment scenarios

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
  start                          Start the controller and operator stack (starts disarmed)
  stop                           Stop the controller and operator stack
  status                         Display service health, ROS graph, and guard state
  logs                           Show recent systemd journal logs for mentorpi-tank.service

Hardware Actuation (Milestones 4, 6, 9, & 15):
  arm --ack-tracks-raised        Explicitly arm motor guard (requires physical safety acknowledgment)
  disarm                         Immediately disarm and send repeated zero commands
  teleop                         Run interactive keyboard teleoperation (W/A/S/D)
  bench --ack-tracks-raised      Run bounded bench test of forward/reverse/left/right motion
                                 Options: [--physical-observations <file>] [--interactive-observations]
  web-acceptance --ack-tracks-raised
                                 Run Milestone 15 raised-track web movement & failure acceptance suite
                                 Options: [--physical-observations <file>] [--interactive-observations]
                                          [--expected-release-id <id>] [--mock] [--require-target]
                                          [--resume-campaign <file>] [--campaign-file <file>]
                                 Only a complete interactive run on the Pi 5 can report ACCEPTED;
                                 static observations are schema evidence and remain pending.

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
  echo "--> Running Source Boundary Gate..."
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
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_safety" "${py_bin}" -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_safety/test" -p "test_*.py" -v

  echo ""
  echo "--> Running ubuntu_tank_supervisor unit tests..."
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_supervisor/test" -p "test_*.py" -v

  echo ""
  echo "--> Running ubuntu_tank_teleop unit tests..."
  PYTHONPATH="${SCRIPT_DIR}/src/ubuntu_tank_teleop" "${py_bin}" -m unittest discover -s "${SCRIPT_DIR}/src/ubuntu_tank_teleop/test" -p "test_*.py" -v

  echo ""
  echo "--> Running Milestone 2 installation workflow unit tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_install_workflow.py" -v

  echo ""
  echo "--> Running Milestone 3 Lyrical port and dependency closure unit tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone3_port.py" -v

  echo ""
  echo "--> Running Milestone 4 Guarded bringup and safe teleop tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone4_bringup.py" -v

  echo ""
  echo "--> Running Milestone 4 Hardware-Free RMW & Runtime Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_rmw_integration.py" -v

  echo ""
  echo "--> Verifying SROS2 Security Policies..."
  "${py_bin}" "${SCRIPT_DIR}/scripts/sros2_policy.py"

  echo ""
  echo "--> Running Milestone 5 Native Host Deployment & Operations tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone5_deployment.py" -v

  echo ""
  echo "--> Running Milestone 6 Raised-Track Controller Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone6_acceptance.py" -v

  echo ""
  echo "--> Running Milestone 7 Fast DDS Loopback & Service Correction tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone7_dds_correction.py" -v

  echo ""
  echo "--> Running Milestone 8 Bounded Arming & Verified Delivery Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone8_delivery.py" -v

  echo ""
  echo "--> Running Milestone 9 Physical Acceptance Closure tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone9_physical_closure.py" -v

  echo ""
  echo "--> Running Milestone 10 Web Control Protocol, State Machine, & Lease tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone10_protocol.py" -v

  echo ""
  "${py_bin}" "${SCRIPT_DIR}/tests/test_container_protocol.py" -v

  echo "--> Running Milestone 11 Shared Operator Agent & CLI Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone11_operator_agent.py" -v

  echo ""
  echo "--> Running Milestone 12 Web API, Service Lifecycle, & WebSocket tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone12_web_api.py" -v

  echo ""
  echo "--> Running Milestone 13 Vue Browser & PWA Driving Interface tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone13_browser_pwa.py" -v

  echo ""
  echo "--> Running Milestone 14 Installed Pi Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone14_installed_integration.py" -v

  echo ""
  echo "--> Running Milestone 14.1 Target Test Orchestrator unit & contract tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_target_test.py" -v

  echo ""
  echo "--> Running Milestone 15 Raised-Track Web Movement & Failure Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone15_web_acceptance.py" -v

  echo ""
  echo "============================================================"
  echo "All tests PASSED successfully!"
  echo "============================================================"
}

cmd_target_test() {
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol${PYTHONPATH:+:${PYTHONPATH}}" "${py_bin}" "${SCRIPT_DIR}/scripts/target_test.py" "$@"
}

cmd_web_acceptance() {
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_protocol${PYTHONPATH:+:${PYTHONPATH}}" "${py_bin}" "${SCRIPT_DIR}/scripts/web_acceptance.py" "$@"
}

# Load ROS and the active release overlay for operator commands, falling back to
# the local install tree. Disable nounset while sourcing upstream setup scripts.
ensure_ros_env() {
  local pre_ros_path="${PATH:-}"
  if [ -f /opt/ros/lyrical/setup.bash ]; then
    set +u
    # shellcheck source=/dev/null
    source /opt/ros/lyrical/setup.bash
    set -u
  fi
  if [ -f /opt/ubuntu_tank/current/install/setup.bash ]; then
    set +u
    # shellcheck source=/dev/null
    source /opt/ubuntu_tank/current/install/setup.bash
    set -u
  elif [ -f "${WORKSPACE_ROOT}/install/setup.bash" ]; then
    set +u
    # shellcheck source=/dev/null
    source "${WORKSPACE_ROOT}/install/setup.bash"
    set -u
  fi

  # Preserve caller PATH precedence (e.g. test harness mocks or explicit wrappers)
  if [ -n "${pre_ros_path}" ]; then
    local new_path=""
    local p
    local oifs="${IFS}"
    IFS=':'
    for p in ${pre_ros_path} ${PATH}; do
      [ -n "${p}" ] || continue
      case ":${new_path}:" in
        *":${p}:"*) ;;
        *)
          if [ -z "${new_path}" ]; then
            new_path="${p}"
          else
            new_path="${new_path}:${p}"
          fi
          ;;
      esac
    done
    IFS="${oifs}"
    PATH="${new_path}"
    export PATH
  fi

  # Resolve Fast DDS loopback profile
  local prof=""
  if [ -f /opt/ubuntu_tank/current/config/fastdds/loopback.xml ]; then
    prof="$(readlink -f /opt/ubuntu_tank/current/config/fastdds/loopback.xml 2>/dev/null || echo /opt/ubuntu_tank/current/config/fastdds/loopback.xml)"
  elif [ -f "${SCRIPT_DIR}/config/fastdds/loopback.xml" ]; then
    prof="${SCRIPT_DIR}/config/fastdds/loopback.xml"
  elif [ -n "${FASTDDS_DEFAULT_PROFILES_FILE:-}" ] && [ -f "${FASTDDS_DEFAULT_PROFILES_FILE}" ]; then
    prof="${FASTDDS_DEFAULT_PROFILES_FILE}"
  fi

  if [ -n "${prof}" ] && [ -r "${prof}" ]; then
    export RMW_IMPLEMENTATION="rmw_fastrtps_cpp"
    export ROS_DOMAIN_ID="0"
    export ROS_LOCALHOST_ONLY="1"
    export ROS_AUTOMATIC_DISCOVERY_RANGE="SYSTEM_DEFAULT"
    export FASTDDS_DEFAULT_PROFILES_FILE="${prof}"
  fi
}

cmd_arm() {
  ensure_ros_env
  local ack=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ack-tracks-raised)
        ack="true"
        shift
        ;;
      *)
        shift
        ;;
    esac
  done

  if [ "${ack}" != "true" ]; then
    echo "ERROR: Arming requires physical safety acknowledgment: --ack-tracks-raised" >&2
    echo "Tracks must be physically raised clear of the surface before motor commands are enabled." >&2
    exit 1
  fi

  # Mutual exclusion: ensure factory or replacement containers are not running
  if command -v docker >/dev/null 2>&1; then
    local docker_out
    if ! docker_out="$(docker ps -a --format '{{.Names}}' 2>&1)"; then
      echo "ERROR: Docker command is available but container inventory could not be enumerated: ${docker_out}" >&2
      echo "Cannot verify mutual exclusion with containerized stacks. Failing closed." >&2
      exit 1
    fi
    if echo "${docker_out}" | grep -E -qw "MentorPi|MentorPiFan|mentorpi|runtime-core"; then
      echo "ERROR: Conflicting container (MentorPi, MentorPiFan, mentorpi, or runtime-core) is running." >&2
      echo "Native Ubuntu controller must never run beside containerized stacks." >&2
      exit 1
    fi
  fi

  if command -v systemctl >/dev/null 2>&1; then
    for srv in mentorpi.service mentorpi-fan.service; do
      if systemctl is-active --quiet "${srv}" 2>/dev/null; then
        echo "ERROR: Conflicting host service '${srv}' is running." >&2
        echo "Native Ubuntu controller must never run beside factory services." >&2
        exit 1
      fi
    done
  fi

  if ! command -v ros2 >/dev/null 2>&1; then
    echo "ERROR: ros2 CLI is not found in PATH. Ensure ROS 2 environment is sourced." >&2
    exit 1
  fi

  echo "--> Calling /ubuntu_tank_safety/set_arm with data=True..."
  env ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}" \
    ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}" \
    ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}" \
    ROS_SECURITY_KEYSTORE="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}" \
    ROS_SECURITY_ENCLAVE_OVERRIDE="${ROS_SECURITY_ENCLAVE_OVERRIDE:-/ubuntu_tank/operator}" \
    ros2 run ubuntu_tank_bringup operator_client --arm
}

cmd_disarm() {
  ensure_ros_env
  if ! command -v ros2 >/dev/null 2>&1; then
    echo "ERROR: ros2 CLI is not found in PATH. Ensure ROS 2 environment is sourced." >&2
    exit 1
  fi

  echo "--> Calling /ubuntu_tank_safety/set_arm with data=False..."
  env ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}" \
    ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}" \
    ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}" \
    ROS_SECURITY_KEYSTORE="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}" \
    ROS_SECURITY_ENCLAVE_OVERRIDE="${ROS_SECURITY_ENCLAVE_OVERRIDE:-/ubuntu_tank/operator}" \
    ros2 run ubuntu_tank_bringup operator_client --disarm
}

cmd_teleop() {
  ensure_ros_env
  if ! command -v ros2 >/dev/null 2>&1; then
    echo "ERROR: ros2 CLI is not found in PATH. Ensure ROS 2 environment is sourced." >&2
    exit 1
  fi

  local teleop_config_args
  if ! teleop_config_args="$("${py_bin}" "${SCRIPT_DIR}/scripts/config_migration.py" teleop-args "${UBUNTU_TANK_CONFIG:-/etc/opt/ubuntu_tank/controller.yaml}")"; then
    return 1
  fi
  local -a host_teleop_args
  mapfile -t host_teleop_args <<<"${teleop_config_args}"
  echo "--> Launching interactive keyboard teleoperation..."
  exec env ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}" \
    ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}" \
    ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}" \
    ROS_SECURITY_KEYSTORE="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}" \
    ROS_SECURITY_ENCLAVE_OVERRIDE="${ROS_SECURITY_ENCLAVE_OVERRIDE:-/ubuntu_tank/operator}" \
    ros2 run ubuntu_tank_teleop teleop_key "$@" --ros-args "${host_teleop_args[@]}"
}

cmd_status() {
  ensure_ros_env
  echo "============================================================"
  echo "MentorPi Tank Controller Status"
  echo "============================================================"

  # 1. Release information
  if [ -L "/opt/ubuntu_tank/current" ]; then
    local target
    target="$(readlink -f /opt/ubuntu_tank/current 2>/dev/null || true)"
    echo "Active Release: $(basename "${target}") (${target})"
  else
    echo "Active Release: None (/opt/ubuntu_tank/current not linked)"
  fi

  # 2. Systemd service status
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet mentorpi-tank.service 2>/dev/null; then
      echo "Service: ACTIVE (mentorpi-tank.service)"
    else
      echo "Service: INACTIVE (mentorpi-tank.service)"
    fi
    if systemctl is-active --quiet mentorpi-tank-operator.service 2>/dev/null; then
      echo "Operator: ACTIVE (mentorpi-tank-operator.service)"
    else
      echo "Operator: INACTIVE (mentorpi-tank-operator.service)"
    fi
    if systemctl is-active --quiet mentorpi-tank-web.service 2>/dev/null; then
      echo "Web: ACTIVE (mentorpi-tank-web.service)"
    else
      echo "Web: INACTIVE (mentorpi-tank-web.service)"
    fi
    if systemctl is-active --quiet mentorpi-tank-lifecycle.service 2>/dev/null; then
      echo "Lifecycle: ACTIVE (mentorpi-tank-lifecycle.service)"
    else
      echo "Lifecycle: INACTIVE (mentorpi-tank-lifecycle.service)"
    fi
    if systemctl is-active --quiet mentorpi-tank-stack.target 2>/dev/null; then
      echo "Stack: ACTIVE (mentorpi-tank-stack.target)"
    else
      echo "Stack: INACTIVE (mentorpi-tank-stack.target)"
    fi
  else
    echo "Service: systemctl not available"
  fi

  # 3. Serial hardware status
  if [ -e "/dev/rrc" ]; then
    echo "Hardware Serial: PRESENT (/dev/rrc -> $(readlink -f /dev/rrc 2>/dev/null || echo '/dev/rrc'))"
  else
    echo "Hardware Serial: ABSENT (/dev/rrc not found)"
  fi

  # 4. Guard state and telemetry (if ROS is active)
  if command -v ros2 >/dev/null 2>&1; then
    echo "--> Querying guard state and telemetry..."
    if ! env ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}" \
      ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}" \
      ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}" \
      ROS_SECURITY_KEYSTORE="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}" \
      ROS_SECURITY_ENCLAVE_OVERRIDE="${ROS_SECURITY_ENCLAVE_OVERRIDE:-/ubuntu_tank/status}" \
      ros2 run ubuntu_tank_bringup status_client; then
      echo "    Guard state topic unavailable or not publishing."
      return 1
    fi
  else
    echo "ros2 CLI not available in current environment."
  fi
}

cmd_bench() {
  local ack=""
  for arg in "$@"; do
    if [ "$arg" = "--ack-tracks-raised" ]; then
      ack="true"
      break
    fi
  done

  if [ "${ack}" != "true" ]; then
    echo "ERROR: Bench testing requires physical safety acknowledgment: --ack-tracks-raised" >&2
    echo "Tracks must be physically raised clear of the surface before motor commands are enabled." >&2
    echo "Under NO circumstances does Milestone 6 authorize on-ground motion." >&2
    exit 1
  fi

  # Deployment lock check
  local lock_file="${UBUNTU_TANK_LOCK_FILE:-/run/lock/ubuntu_tank/deploy.lock}"
  if [ -f "${lock_file}" ]; then
    if command -v python3 >/dev/null 2>&1; then
      if ! python3 -c "import fcntl, os, sys; fd = os.open('${lock_file}', os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)" 2>/dev/null; then
        echo "ERROR: Deployment lock is held by another process; cannot run bench test." >&2
        exit 1
      fi
    fi
  fi

  # Mutual exclusion: ensure factory or replacement containers are not running
  if [ -n "${UBUNTU_TANK_MOCK_DOCKER_FAIL:-}" ]; then
    echo "ERROR: Docker is installed but container inventory check failed. Cannot verify mutual exclusion. Bench test blocked." >&2
    exit 1
  elif [ -n "${UBUNTU_TANK_MOCK_DOCKER_PS:-}" ]; then
    local docker_out="${UBUNTU_TANK_MOCK_DOCKER_PS}"
    if [ "${docker_out}" = "none" ] || [ "${docker_out}" = "EMPTY" ]; then
      docker_out=""
    fi
    if [ -n "${docker_out}" ] && echo "${docker_out}" | grep -E -qw "MentorPi|MentorPiFan|mentorpi|runtime-core|tank_runtime"; then
      echo "ERROR: Conflicting container (MentorPi, MentorPiFan, mentorpi, or runtime-core) is running or present." >&2
      exit 1
    fi
  elif [ -z "${UBUNTU_TANK_BENCH_MOCK:-}" ] && command -v docker >/dev/null 2>&1; then
    local docker_out
    if ! docker_out="$(docker ps -a --format '{{.Names}}' 2>&1)"; then
      echo "ERROR: Docker command is available but container inventory could not be enumerated: ${docker_out}" >&2
      echo "Cannot verify mutual exclusion with containerized stacks. Failing closed." >&2
      exit 1
    fi
    if echo "${docker_out}" | grep -E -qw "MentorPi|MentorPiFan|mentorpi|runtime-core"; then
      echo "ERROR: Conflicting container (MentorPi, MentorPiFan, mentorpi, or runtime-core) is running." >&2
      echo "Native Ubuntu controller must never run beside containerized stacks." >&2
      exit 1
    fi
  fi

  ensure_ros_env
  local sec_keystore="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}"
  echo "--> Running Milestone 6 raised-track bench acceptance..."
  ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}" \
    ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}" \
    ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}" \
    ROS_SECURITY_KEYSTORE="${sec_keystore}" \
    PYTHONPATH="${WORKSPACE_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${py_bin}" "${SCRIPT_DIR}/scripts/bench_acceptance.py" "$@"
}

cmd_package() {
  echo "--> Packaging release..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/scripts/deployment_manager.py" package "$@"
}

cmd_install() {
  echo "--> Installing release artifact..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/scripts/deployment_manager.py" install "$@"
}

cmd_activate() {
  echo "--> Activating release..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/scripts/deployment_manager.py" activate "$@"
}

cmd_rollback() {
  echo "--> Rolling back to previous release..."
  PYTHONPATH="${WORKSPACE_ROOT}" "${py_bin}" "${SCRIPT_DIR}/scripts/deployment_manager.py" rollback "$@"
}

cmd_start() {
  # 1. Mutual exclusion: Docker container inventory
  if [ -n "${UBUNTU_TANK_MOCK_DOCKER_FAIL:-}" ]; then
    echo "ERROR: Docker is installed but container inventory check failed. Cannot verify mutual exclusion. Startup blocked." >&2
    exit 1
  elif [ -n "${UBUNTU_TANK_MOCK_DOCKER_PS:-}" ]; then
    local docker_out="${UBUNTU_TANK_MOCK_DOCKER_PS}"
    if [ "${docker_out}" = "none" ] || [ "${docker_out}" = "EMPTY" ]; then
      docker_out=""
    fi
    if [ -n "${docker_out}" ] && echo "${docker_out}" | grep -E -qw "MentorPi|MentorPiFan|mentorpi|runtime-core|tank_runtime"; then
      echo "ERROR: Conflicting container (MentorPi, MentorPiFan, mentorpi, or runtime-core) is running or present." >&2
      exit 1
    fi
  elif command -v docker >/dev/null 2>&1; then
    local docker_out
    if ! docker_out="$(docker ps -a --format '{{.Names}}' 2>&1)"; then
      echo "ERROR: Docker is installed but container inventory check failed: ${docker_out}" >&2
      echo "Cannot verify mutual exclusion of factory container. Startup blocked." >&2
      exit 1
    fi
    if echo "${docker_out}" | grep -E -qw "MentorPi|MentorPiFan|mentorpi|runtime-core|tank_runtime"; then
      echo "ERROR: Conflicting container (MentorPi, MentorPiFan, mentorpi, or runtime-core) is running or present." >&2
      exit 1
    fi
  fi

  # 2. Conflicting host services
  if [ -z "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    local factory_units=("mentorpi.service" "mentorpi-start.service" "mentorpi-fan.service" "hiwonder-chassis.service")
    for unit in "${factory_units[@]}"; do
      if [ -f "/etc/systemd/system/${unit}" ] || [ -f "/lib/systemd/system/${unit}" ]; then
        if command -v systemctl >/dev/null 2>&1; then
          if systemctl is-active --quiet "${unit}" 2>/dev/null; then
            echo "ERROR: Conflicting factory systemd unit is active: ${unit}. Startup blocked." >&2
            exit 1
          fi
        fi
      elif command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "${unit}" 2>/dev/null; then
          echo "ERROR: Conflicting factory systemd unit is active: ${unit}. Startup blocked." >&2
          exit 1
        fi
      fi
    done
  fi

  # 3. Serial port device exclusivity check
  local serial_dev="${UBUNTU_TANK_MOCK_SERIAL_DEV:-/dev/rrc}"
  if [ -n "${UBUNTU_TANK_MOCK_SERIAL_HOLDER:-}" ]; then
    echo "ERROR: Conflicting process PID(s) ${UBUNTU_TANK_MOCK_SERIAL_HOLDER} hold ${serial_dev} open. Startup blocked." >&2
    exit 1
  elif [ -e "${serial_dev}" ]; then
    local real_dev
    real_dev="$(readlink -f "${serial_dev}" || echo "${serial_dev}")"
    if [ -c "${real_dev}" ]; then
      if ! command -v fuser >/dev/null 2>&1; then
        echo "ERROR: ${serial_dev} character device is present but 'fuser' command is not available to verify exclusivity. Startup blocked." >&2
        exit 1
      else
        local holder
        holder="$(fuser "${real_dev}" 2>/dev/null || true)"
        if [ -n "${holder}" ]; then
          echo "ERROR: Conflicting process PID(s) ${holder} currently hold ${real_dev} open. Startup blocked." >&2
          exit 1
        fi
      fi
    fi
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found on this system." >&2
    exit 1
  fi

  # Check deployment lock and journal state before starting
  local lock_file="${UBUNTU_TANK_LOCK_FILE:-/run/lock/ubuntu_tank/deploy.lock}"
  local journal_file="${UBUNTU_TANK_JOURNAL_FILE:-/var/opt/ubuntu_tank/deployment/activation-journal}"
  if [ -f "${lock_file}" ]; then
    if command -v python3 >/dev/null 2>&1; then
      if ! python3 -c "import fcntl, os, sys; fd = os.open('${lock_file}', os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)" 2>/dev/null; then
        echo "ERROR: Deployment lock is held by another process; cannot start controller." >&2
        exit 1
      fi
    fi
  fi
  local dep_dir
  dep_dir="$(dirname "${journal_file}")"
  if [ -f "${journal_file}" ]; then
    if command -v python3 >/dev/null 2>&1; then
      if ! python3 -c "import json, sys
try:
    with open('${journal_file}', 'r', encoding='utf-8') as f:
        d = json.load(f)
    if not isinstance(d, dict) or 'format_version' not in d:
        sys.exit(1)
    if d.get('current_transaction') is not None:
        sys.exit(1)
except Exception:
    sys.exit(1)" 2>/dev/null; then
        echo "ERROR: Activation journal at '${journal_file}' has uncommitted transaction or invalid state; cannot start controller." >&2
        exit 1
      fi
    fi
  elif [ -d "${dep_dir}" ]; then
    echo "ERROR: Activation journal missing at '${journal_file}'; cannot start controller." >&2
    exit 1
  fi

  if ! systemctl cat mentorpi-tank-stack.target >/dev/null 2>&1; then
    echo "ERROR: mentorpi-tank-stack.target is not installed. Activate a release containing the stack target before startup." >&2
    exit 1
  fi

  echo "--> Starting mentorpi-tank-stack.target..."
  if systemctl is-active --quiet mentorpi-tank-stack.target 2>/dev/null &&
    { ! systemctl is-active --quiet mentorpi-tank.service 2>/dev/null ||
      ! systemctl is-active --quiet mentorpi-tank-operator.service 2>/dev/null; }; then
    # A member may have been stopped independently while the passive target
    # remained active. Restart the target to re-evaluate its Wants= members.
    sudo systemctl restart mentorpi-tank-stack.target
  else
    sudo systemctl start mentorpi-tank-stack.target
  fi
  if ! systemctl is-active --quiet mentorpi-tank.service; then
    echo "ERROR: mentorpi-tank.service failed to start. Check logs with './deploy.sh logs'." >&2
    sudo systemctl stop mentorpi-tank-stack.target || true
    exit 1
  fi
  if ! systemctl is-active --quiet mentorpi-tank-operator.service; then
    echo "ERROR: mentorpi-tank-operator.service failed to start. Check logs with './deploy.sh logs operator'." >&2
    sudo systemctl stop mentorpi-tank-stack.target || true
    exit 1
  fi
  echo "mentorpi-tank-stack.target is ACTIVE (controller and operator active)."
  echo "NOTE: Controller starts in DISARMED state. Use './deploy.sh arm --ack-tracks-raised' to arm motors."
}

cmd_stop() {
  echo "--> Stopping MentorPi Tank stack..."
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found on this system." >&2
    exit 1
  fi

  if systemctl cat mentorpi-tank-stack.target >/dev/null 2>&1; then
    sudo systemctl stop mentorpi-tank-stack.target
    # Compatibility for services enabled or started independently before the
    # stack target was introduced. Normal target-managed operation stops both
    # through PartOf= without these fallback calls.
    if systemctl is-active --quiet mentorpi-tank-operator.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-operator.service
    fi
    if systemctl is-active --quiet mentorpi-tank-web.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-web.service
    fi
    if systemctl is-active --quiet mentorpi-tank-lifecycle.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-lifecycle.service
    fi
    if systemctl is-active --quiet mentorpi-tank.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank.service
    fi
  else
    echo "NOTE: Stack target is not installed; using legacy per-service shutdown."
    if systemctl is-active --quiet mentorpi-tank-web.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-web.service
    fi
    if systemctl is-active --quiet mentorpi-tank-lifecycle.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-lifecycle.service
    fi
    if systemctl is-active --quiet mentorpi-tank-operator.service 2>/dev/null; then
      sudo systemctl stop mentorpi-tank-operator.service
    fi
    if command -v ros2 >/dev/null 2>&1; then
      cmd_disarm 2>/dev/null || true
    fi
    sudo systemctl stop mentorpi-tank.service
  fi

  if systemctl is-active --quiet mentorpi-tank-web.service 2>/dev/null; then
    echo "ERROR: mentorpi-tank-web.service remains active after stack shutdown." >&2
    exit 1
  fi
  if systemctl is-active --quiet mentorpi-tank-lifecycle.service 2>/dev/null; then
    echo "ERROR: mentorpi-tank-lifecycle.service remains active after stack shutdown." >&2
    exit 1
  fi
  if systemctl is-active --quiet mentorpi-tank-operator.service 2>/dev/null; then
    echo "ERROR: mentorpi-tank-operator.service remains active after stack shutdown." >&2
    exit 1
  fi
  if systemctl is-active --quiet mentorpi-tank.service 2>/dev/null; then
    echo "ERROR: mentorpi-tank.service remains active after stack shutdown." >&2
    exit 1
  fi
  echo "mentorpi-tank-stack.target is STOPPED (controller, operator, web, and lifecycle inactive)."
}

cmd_logs() {
  local target_service="mentorpi-tank.service"
  local lines="50"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      mentorpi-tank-web.service | web)
        target_service="mentorpi-tank-web.service"
        shift
        ;;
      mentorpi-tank-lifecycle.service | lifecycle)
        target_service="mentorpi-tank-lifecycle.service"
        shift
        ;;
      mentorpi-tank-operator.service | operator)
        target_service="mentorpi-tank-operator.service"
        shift
        ;;
      mentorpi-tank.service | controller)
        target_service="mentorpi-tank.service"
        shift
        ;;
      [0-9]*)
        lines="$1"
        shift
        ;;
      *)
        shift
        ;;
    esac
  done
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u "${target_service}" -n "${lines}" --no-pager
  else
    echo "journalctl not available."
  fi
}

cmd_stub() {
  local cmd="$1"
  local milestone="$2"
  echo "Command '${cmd}' is planned for ${milestone} according to MENTORPI_FRESH_CONTROLLER_DESIGN.md." >&2
  exit 1
}

COMMAND="${1:-help}"
shift || true

case "${COMMAND}" in
  help | -h | --help)
    usage
    ;;
  test)
    cmd_test "$@"
    ;;
  target-test)
    cmd_target_test "$@"
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
  package)
    cmd_package "$@"
    ;;
  install)
    cmd_install "$@"
    ;;
  activate)
    cmd_activate "$@"
    ;;
  rollback)
    cmd_rollback "$@"
    ;;
  start)
    cmd_start "$@"
    ;;
  stop)
    cmd_stop "$@"
    ;;
  logs)
    cmd_logs "$@"
    ;;
  arm)
    cmd_arm "$@"
    ;;
  disarm)
    cmd_disarm "$@"
    ;;
  teleop)
    cmd_teleop "$@"
    ;;
  status)
    cmd_status "$@"
    ;;
  bench)
    cmd_bench "$@"
    ;;
  web-acceptance)
    cmd_web_acceptance "$@"
    ;;

  *)
    echo "Unknown command: ${COMMAND}" >&2
    usage
    exit 1
    ;;
esac
