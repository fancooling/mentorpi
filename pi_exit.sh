#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-MentorPi_T1_20260822.img}"
MOUNT_DIR="/mnt/rpi-rootfs"

if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (e.g., sudo $0 [image_path])" >&2
  exit 1
fi

# 1. Unmount directories
echo "[+] Unmounting $MOUNT_DIR..."
if mountpoint -q "$MOUNT_DIR/boot"; then
  umount "$MOUNT_DIR/boot"
fi

if mountpoint -q "$MOUNT_DIR"; then
  umount "$MOUNT_DIR"
fi

# 2. Teardown kpartx mappings
if [ -f "$IMAGE" ]; then
  echo "[+] Removing partition maps for $IMAGE..."
  kpartx -dv "$IMAGE"
fi

echo "[+] Cleanup complete."
