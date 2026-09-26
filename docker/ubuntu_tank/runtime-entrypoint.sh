#!/usr/bin/env bash
# Source the installed ARM64 workspace and enforce private loopback ROS settings.
set -eo pipefail
# shellcheck source=/dev/null
source /opt/ros/lyrical/setup.bash
# shellcheck source=/dev/null
source /opt/ubuntu_tank/current/install/setup.bash
set -u
export UBUNTU_TANK_PREFIX=/opt/ubuntu_tank/current
export UBUNTU_TANK_PRIVATE_DIR=/run/ubuntu_tank-private
export UBUNTU_TANK_PYTHON=/usr/bin/python3
export UBUNTU_TANK_CONFIG=/etc/opt/ubuntu_tank/controller.yaml
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=1
export ROS_AUTOMATIC_DISCOVERY_RANGE=SYSTEM_DEFAULT
export FASTDDS_DEFAULT_PROFILES_FILE=/opt/ubuntu_tank/current/config/fastdds/loopback.xml
export ROS_SECURITY_ENABLE=true ROS_SECURITY_STRATEGY=Enforce
export ROS_SECURITY_KEYSTORE=/etc/opt/ubuntu_tank/security/keystore
export ROS_LOG_DIR=/var/opt/ubuntu_tank/ros-log
if [[ -n ${UBUNTU_TANK_SIMULATION:-} || -n ${_UBUNTU_TANK_TEST_CHILD_CMD:-} ]]; then
  echo 'Production runtime rejects simulation overrides' >&2
  exit 1
fi
if [[ $# -gt 0 ]]; then
  exec "$@"
fi
exec /usr/bin/python3 /opt/ubuntu_tank/container/runtime_start.py
