---
name: embedded-robot-disk-analysis
description: Workflow for inspecting, reverse-engineering, and extracting setups from mounted embedded Linux / robotics disk images (e.g., Raspberry Pi OS, Jetson) using hybrid host-container architectures.
---

# Embedded Robotics Disk Image Analysis & Clean Setup

Use this skill when analyzing, troubleshooting, or extracting code from mounted multi-partition disk images (`.img`) of embedded robots (e.g., Hiwonder, Yahboom, Waveshare).

## 1. Inspecting Architecture & Locating Code

1. **Check for Containerized Workspaces:**
   - On Raspberry Pi OS images running modern ROS 2 (e.g., Humble, Iron), ROS is almost always containerized in Docker to bypass Debian/Ubuntu binary incompatibilities.
   - Inspect `/etc/systemd/system/` and `/home/*/*.sh` on the host for `docker exec` calls.
   - Inspect `/var/lib/docker/overlay2/<layer>/diff/` to locate the container's `/home/ubuntu` or `/root` workspace (`ros2_ws`, `third_party_ws`).

2. **Trace Hardware & Motion Pipelines:**
   - **Kinematics & Odom:** Look in `driver/controller` or `chassis` nodes subscribing to `cmd_vel` (`Twist`) and publishing motor states (`MotorsState`).
   - **Actuation:** Look for STM32/MCU serial drivers (`/dev/rrc`, `/dev/ttyACM*`, `/dev/ttyUSB*`) operating at high baud rates (e.g., 1,000,000 baud).
   - **Sensors:** Look for LiDAR nodes (`sensor_msgs/LaserScan` on `/scan` or `/scan_raw`) and depth camera nodes (`sensor_msgs/Image`, `PointCloud2`).

## 2. Extracting for Clean Re-installation

When extracting assets to set up a clean OS, keep the layout modular:

1. **`third_party_src/`**: Extract only proprietary, unhosted, or vendor-specific drivers (e.g., Deptrum depth camera SDKs, Oradar LiDAR drivers).
2. **`host_setup/`**: Extract `/etc/udev/rules.d/*.rules` (serial symlinks and camera USB permissions) and host systemd units.
3. **`container_env/`**: Extract presets (`.typerc`, `.robotrc`) and stop scripts.
4. **`tools_and_models/`**: Extract calibration GUIs, tuning tools, and weights.
5. **Always Filter Out:**
   - Standard ROS packages available via `apt install ros-<distro>-*` (`nav2`, `slam_toolbox`, `laser_filters`, `teb_local_planner`).
   - Core crash dumps (`core` files) and transient build artifacts (`__pycache__`, build dirs).
   - Nested `.git` directories inside demo packages.

## 3. Virtual Cross-Architecture Testing (x86_64 to ARM64)

- Do not default to slow full-system VM emulation (`qemu-system-aarch64`).
- Check for `qemu-aarch64-static` and `/proc/sys/fs/binfmt_misc/qemu-aarch64`.
- Run user-mode ARM64 Docker containers directly on the x86 host:
  ```bash
  docker run --platform linux/arm64 -it -v $(pwd):/workspace -w /workspace arm64v8/ros:humble /bin/bash
  ```
