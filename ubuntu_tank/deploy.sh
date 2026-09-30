#!/usr/bin/env bash
# Development validation entrypoint. Production operations use docker/ubuntu_tank.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
py_bin="${WORKSPACE_ROOT}/.venv/bin/python"
# Existing shell validation helpers inherit the repository interpreter.
export PATH="${WORKSPACE_ROOT}/.venv/bin:${PATH}"

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
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_install_workflow.py" -v

  echo ""
  echo "--> Running Milestone 3 Lyrical port and dependency closure unit tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone3_port.py" -v

  echo ""
  echo "--> Running Milestone 4 Guarded bringup and safe teleop tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone4_bringup.py" -v

  echo ""
  echo "--> Running Milestone 4 Hardware-Free RMW & Runtime Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_rmw_integration.py" -v

  echo ""
  echo "--> Verifying SROS2 Security Policies..."
  "${py_bin}" "${SCRIPT_DIR}/scripts/sros2_policy.py"

  echo ""
  echo "--> Running Milestone 5 Native Host Deployment & Operations tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone5_deployment.py" -v

  echo ""
  echo "--> Running Milestone 6 Raised-Track Controller Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone6_acceptance.py" -v

  echo ""
  echo "--> Running Milestone 7 Fast DDS Loopback & Service Correction tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone7_dds_correction.py" -v

  echo ""
  echo "--> Running Milestone 8 Bounded Arming & Verified Delivery Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone8_delivery.py" -v

  echo ""
  echo "--> Running Milestone 9 Physical Acceptance Closure tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone9_physical_closure.py" -v

  echo ""
  echo "--> Running Milestone 10 Web Control Protocol, State Machine, & Lease tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone10_protocol.py" -v

  echo ""
  "${py_bin}" "${SCRIPT_DIR}/tests/test_container_protocol.py" -v
  "${py_bin}" "${SCRIPT_DIR}/tests/test_container_supervision.py" -v
  "${py_bin}" "${SCRIPT_DIR}/tests/test_container_web.py" -v
  "${py_bin}" "${SCRIPT_DIR}/tests/test_container_admission.py" -v

  echo "--> Running Milestone 11 Shared Operator Agent & CLI Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_supervisor" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone11_operator_agent.py" -v

  echo ""
  echo "--> Running Milestone 12 Web API, Service Lifecycle, & WebSocket tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_supervisor:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone12_web_api.py" -v

  echo ""
  "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone145_control_lifecycle.py" -v

  echo "--> Running Milestone 13 Vue Browser & PWA Driving Interface tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_supervisor:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone13_browser_pwa.py" -v

  echo ""
  echo "--> Running Milestone 14 Installed Pi Integration tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_supervisor:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone14_installed_integration.py" -v

  echo ""
  echo "--> Running Milestone 14.1 Target Test Orchestrator unit & contract tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_supervisor:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_target_test.py" -v

  echo ""
  echo "--> Running Milestone 15 Raised-Track Web Movement & Failure Acceptance tests..."
  PYTHONPATH="${WORKSPACE_ROOT}:${SCRIPT_DIR}/src/ubuntu_tank_web:${SCRIPT_DIR}/src/ubuntu_tank_operator:${SCRIPT_DIR}/src/ubuntu_tank_supervisor:${SCRIPT_DIR}/src/ubuntu_tank_protocol" "${py_bin}" "${SCRIPT_DIR}/tests/test_milestone15_web_acceptance.py" -v

  echo ""
  echo "============================================================"
  echo "All tests PASSED successfully!"
  echo "============================================================"
}

case "${1:-help}" in
  test)
    shift
    cmd_test "$@"
    ;;
  help | -h | --help)
    echo './ubuntu_tank/deploy.sh test: development and browser checks'
    echo 'Pi host operations: /usr/bin/python3 docker/ubuntu_tank/install.py --help'
    ;;
  *)
    echo 'Native operations have been removed. Use docker/ubuntu_tank/install.py.' >&2
    exit 1
    ;;
esac
