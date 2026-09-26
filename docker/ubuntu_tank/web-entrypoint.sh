#!/usr/bin/env bash
# Serve mounted TLS and configuration with one ROS-free API worker.
set -euo pipefail
exec /usr/bin/python3 -m ubuntu_tank_web.entrypoint \
  --config /etc/opt/ubuntu_tank/web/web.yaml --host 0.0.0.0 --port 8443 \
  --static-dir /opt/ubuntu_tank/current/web/dist "$@"
