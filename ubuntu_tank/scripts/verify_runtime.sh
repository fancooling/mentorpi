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

ERRORS=0
ALLOW_ARMED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-armed)
      ALLOW_ARMED=1
      shift
      ;;
    help | --help | -h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

echo "============================================================"
echo "Verifying Native Ubuntu Tank Controller Runtime"
echo "============================================================"

# 1. Verify localhost-only DDS setting
echo "--> Checking ROS_LOCALHOST_ONLY..."
if [ "${ROS_LOCALHOST_ONLY:-0}" != "1" ]; then
  echo "ERROR: ROS_LOCALHOST_ONLY must be 1 (found: '${ROS_LOCALHOST_ONLY:-0}'). DDS discovery must not leak to local network." >&2
  ERRORS=$((ERRORS + 1))
else
  echo "    ROS_LOCALHOST_ONLY=1 confirmed."
fi

# 2. Verify SROS2 security enforcement environment
echo "--> Checking SROS2 security settings..."
if [ "${ROS_SECURITY_ENABLE:-}" != "true" ]; then
  echo "ERROR: ROS_SECURITY_ENABLE must be 'true' (found: '${ROS_SECURITY_ENABLE:-}')." >&2
  ERRORS=$((ERRORS + 1))
else
  echo "    ROS_SECURITY_ENABLE=true confirmed."
fi

if [ "${ROS_SECURITY_STRATEGY:-}" != "Enforce" ]; then
  echo "ERROR: ROS_SECURITY_STRATEGY must be 'Enforce' (found: '${ROS_SECURITY_STRATEGY:-}')." >&2
  ERRORS=$((ERRORS + 1))
else
  echo "    ROS_SECURITY_STRATEGY=Enforce confirmed."
fi

# 3. Verify ROS 2 CLI availability
if ! command -v ros2 >/dev/null 2>&1; then
  echo "ERROR: ros2 CLI is not found. Please source your ROS 2 environment." >&2
  exit 1
fi

STATUS_ENV=(
  env
  ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"
  ROS_SECURITY_ENABLE="${ROS_SECURITY_ENABLE:-true}"
  ROS_SECURITY_STRATEGY="${ROS_SECURITY_STRATEGY:-Enforce}"
  ROS_SECURITY_KEYSTORE="${ROS_SECURITY_KEYSTORE:-/etc/opt/ubuntu_tank/security/keystore}"
  ROS_SECURITY_ENCLAVE_OVERRIDE="${ROS_SECURITY_ENCLAVE_OVERRIDE:-/ubuntu_tank/status}"
)

# 4. Verify Guard State (transient-local topic) and disarmed default
echo "--> Querying guard state (/ubuntu_tank_safety/state)..."
state_out=$("${STATUS_ENV[@]}" timeout 3 ros2 topic echo --qos-durability transient_local /ubuntu_tank_safety/state --once 2>&1) || true

if echo "${state_out}" | grep -q "data: false"; then
  echo "    Guard state confirmed DISARMED (data: false)."
elif echo "${state_out}" | grep -q "data: true"; then
  if [ "${ALLOW_ARMED}" -eq 1 ]; then
    echo "    Guard state is ARMED (permitted via --allow-armed)."
  else
    echo "ERROR: Guard state is currently ARMED (expected initial state: DISARMED / data: false)." >&2
    ERRORS=$((ERRORS + 1))
  fi
else
  echo "ERROR: Failed to query /ubuntu_tank_safety/state or invalid response: ${state_out}" >&2
  ERRORS=$((ERRORS + 1))
fi

verify_endpoint_records() {
  local topic="$1"
  local exp_pub_count="$2"
  local exp_pub_node="$3"
  local exp_sub_count="$4"
  local exp_sub_node="$5"

  python3 -c '
import sys, re

topic = sys.argv[1]
exp_pub_count = int(sys.argv[2])
exp_pub_node = sys.argv[3]
exp_sub_count = int(sys.argv[4])
exp_sub_node = sys.argv[5]

raw = sys.stdin.read()

pub_m = re.search(r"^Publisher count:\s*(\d+)", raw, re.MULTILINE)
sub_m = re.search(r"^Subscription count:\s*(\d+)", raw, re.MULTILINE)

if not pub_m or not sub_m:
    sys.stderr.write(f"ERROR: {topic} output missing Publisher or Subscription count: {raw}\n")
    sys.exit(1)

pub_count = int(pub_m.group(1))
sub_count = int(sub_m.group(1))

parts = re.split(r"^Subscription count:\s*\d+", raw, flags=re.MULTILINE)
pub_block = parts[0]
sub_block = parts[1] if len(parts) > 1 else ""

pub_nodes = re.findall(r"Node name:\s*([^\s\n]+)", pub_block)
sub_nodes = re.findall(r"Node name:\s*([^\s\n]+)", sub_block)

errs = []
if exp_pub_count >= 0 and pub_count != exp_pub_count:
    errs.append(f"Publisher count is {pub_count}, expected exactly {exp_pub_count}")
if exp_pub_node:
    if [exp_pub_node] != pub_nodes:
        errs.append(f"Publisher node(s) {pub_nodes}, expected exact [{exp_pub_node}]")
elif pub_nodes:
    if topic == "/ubuntu_tank_safety/motor_input" and any(n != "controller" for n in pub_nodes):
        errs.append(f"Unexpected publisher node(s) {pub_nodes}, only controller allowed")

if exp_sub_count >= 0 and sub_count != exp_sub_count:
    errs.append(f"Subscription count is {sub_count}, expected exactly {exp_sub_count}")
if exp_sub_node:
    if [exp_sub_node] != sub_nodes:
        errs.append(f"Subscriber node(s) {sub_nodes}, expected exact [{exp_sub_node}]")

if errs:
    for e in errs:
        sys.stderr.write(f"ERROR: {topic}: {e}\n")
    sys.exit(1)

sys.exit(0)
' "${topic}" "${exp_pub_count}" "${exp_pub_node}" "${exp_sub_count}" "${exp_sub_node}"
}

# 5. Verify Guarded Topic Graph & Exclusive Ownership
echo "--> Verifying topic connections and publisher ownership..."
input_info=$("${STATUS_ENV[@]}" timeout 3 ros2 topic info -v /ubuntu_tank_safety/motor_input 2>&1) || true
if ! echo "${input_info}" | verify_endpoint_records "/ubuntu_tank_safety/motor_input" -1 "" 1 "motor_guard"; then
  echo "ERROR: /ubuntu_tank_safety/motor_input topic endpoint verification failed." >&2
  ERRORS=$((ERRORS + 1))
else
  echo "    /ubuntu_tank_safety/motor_input verified (subscriber: motor_guard)."
fi

guarded_info=$("${STATUS_ENV[@]}" timeout 3 ros2 topic info -v /ros_robot_controller/set_motor_guarded 2>&1) || true
if ! echo "${guarded_info}" | verify_endpoint_records "/ros_robot_controller/set_motor_guarded" 1 "motor_guard" 1 "ros_robot_controller"; then
  echo "ERROR: /ros_robot_controller/set_motor_guarded topic endpoint verification failed." >&2
  ERRORS=$((ERRORS + 1))
else
  echo "    /ros_robot_controller/set_motor_guarded verified (publisher: motor_guard, subscriber: ros_robot_controller)."
fi

if [ "${ERRORS}" -gt 0 ]; then
  echo "ERROR: Runtime verification failed with ${ERRORS} check failure(s)." >&2
  exit 1
fi

echo "Runtime verification completed successfully."
exit 0
