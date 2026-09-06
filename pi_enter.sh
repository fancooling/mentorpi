#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-MentorPi_T1_20260822.img}"
MOUNT_DIR="/mnt/rpi-rootfs"

if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (e.g., sudo $0 [image_path])" >&2
  exit 1
fi

if [ ! -f "$IMAGE" ]; then
  echo "Error: Image file '$IMAGE' not found." >&2
  exit 1
fi

# 1. Map partitions
echo "[+] Mapping partitions for $IMAGE..."
MAP_OUTPUT=$(kpartx -av "$IMAGE")
echo "$MAP_OUTPUT"

# Extract the mapped loop device identifiers
BOOT_DEV=$(echo "$MAP_OUTPUT" | awk '/p1 / {print $3}')
ROOT_DEV=$(echo "$MAP_OUTPUT" | awk '/p2 / {print $3}')

if [ -z "$ROOT_DEV" ]; then
  echo "Error: Failed to identify root partition." >&2
  exit 1
fi

# 2. Mount partitions
echo "[+] Mounting root filesystem to $MOUNT_DIR..."
mkdir -p "$MOUNT_DIR"
mount "/dev/mapper/$ROOT_DEV" "$MOUNT_DIR"

if [ -n "$BOOT_DEV" ]; then
  mkdir -p "$MOUNT_DIR/boot"
  mount "/dev/mapper/$BOOT_DEV" "$MOUNT_DIR/boot" 2>/dev/null || true
fi

# 3. Copy QEMU interpreters
echo "[+] Syncing QEMU static binaries..."
[ -f /usr/bin/qemu-aarch64-static ] && cp -u /usr/bin/qemu-aarch64-static "$MOUNT_DIR/usr/bin/"
[ -f /usr/bin/qemu-arm-static ] && cp -u /usr/bin/qemu-arm-static "$MOUNT_DIR/usr/bin/"

# 4. Disable ld.so.preload
if [ -f "$MOUNT_DIR/etc/ld.so.preload" ]; then
  sed -i 's/^/#/' "$MOUNT_DIR/etc/ld.so.preload"
fi

# 5. Spawn shell
echo "[+] Entering environment via systemd-nspawn (type 'exit' when done)..."
systemd-nspawn -q -D "$MOUNT_DIR"