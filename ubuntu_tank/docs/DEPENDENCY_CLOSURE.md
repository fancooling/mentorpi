# Native Ubuntu Tank Direct Dependency Verification & Target Closure Specification

This document records the direct runtime dependency declarations, AST-verified Python imports, and controller-only allowlist for Milestone 1, and defines the target transitive closure specification for subsequent package resolution in Milestones 2 and 3.

---

## 1. Direct Package Dependencies

Every retained package in `ubuntu_tank/src/` declares its exact runtime dependencies in its `package.xml`:

| Package | Declared Runtime Dependencies | System Dependencies | Purpose |
| --- | --- | --- | --- |
| `ros_robot_controller_msgs` | `std_msgs`, `rosidl_default_runtime` | - | Message definitions (chassis state, servos, battery) |
| `ros_robot_controller` | `rclpy`, `std_msgs`, `std_srvs`, `sensor_msgs`, `ros_robot_controller_msgs`, `launch`, `launch_ros` | `python3-serial`, `python3-yaml` | Serial bridge node to STM32 RRC |
| `controller` | `rclpy`, `geometry_msgs`, `nav_msgs`, `sensor_msgs`, `std_srvs`, `ros_robot_controller_msgs` | `python3-yaml` | Tank kinematics & command-integrated odometry |
| `ubuntu_tank_safety` | `rclpy`, `ros_robot_controller_msgs`, `std_msgs`, `std_srvs` | - | Disarmed-by-default motor guard & lease timeout |
| `ubuntu_tank_supervisor` | `rclpy` | - | AND-gated process & heartbeat watchdog supervisor |
| `ubuntu_tank_teleop` | `rclpy`, `geometry_msgs` | - | Renewable 150 ms command leases & fail-closed stop |

---

## 2. Python AST Import Verification

An automated AST import scan across all `.py` files in `ubuntu_tank/src` verifies that every imported external module or package is formally declared in its enclosing package manifest (`package.xml`):

- `rclpy` -> declared in `ros_robot_controller`, `controller`, `ubuntu_tank_safety`, `ubuntu_tank_supervisor`, `ubuntu_tank_teleop`.
- `std_msgs` -> declared in `ros_robot_controller`, `ros_robot_controller_msgs`, `ubuntu_tank_safety`.
- `std_srvs` -> declared in `ros_robot_controller`, `controller`, `ubuntu_tank_safety`.
- `geometry_msgs` -> declared in `controller`, `ubuntu_tank_teleop`.
- `nav_msgs` -> declared in `controller`.
- `sensor_msgs` -> declared in `ros_robot_controller` (for `Imu`, `Joy`), `controller` (for `JointState`).
- `ros_robot_controller_msgs` -> declared in `ros_robot_controller`, `controller`, `ubuntu_tank_safety`.
- `launch` -> declared in `ros_robot_controller`.
- `launch_ros` -> declared in `ros_robot_controller`.
- `serial` -> declared as `python3-serial` in `ros_robot_controller`.
- `yaml` -> declared as `python3-yaml` in `ros_robot_controller`, `controller`.

---

## 3. Target Transitive Dependency Specification (Milestones 2 & 3)

In Milestone 1, verification checks direct package declarations and Python imports without invoking `rosdep` or building on the target OS. The expected transitive closure of these packages resolves within standard ROS 2 `ros-base` and Python system dependencies:

```text
[Direct ROS Packages]
├── rclpy
│   ├── rcl
│   ├── rmw / rmw_implementation
│   ├── rcutils / rcpputils
│   └── rosidl_runtime_py / rosidl_runtime_c
├── std_msgs / std_srvs / geometry_msgs / nav_msgs / sensor_msgs
│   ├── builtin_interfaces
│   └── rosidl_default_runtime
├── launch / launch_ros
│   └── ament_index_python / osrf_pycommon
└── ros_robot_controller_msgs
    └── std_msgs
```

Authoritative recursive dependency resolution and lockfile generation with cryptographic package hashes take place in Milestone 2 (`install-ros` / `verify-lock`) and Milestone 3 (`rosdep check` and `colcon build`).

---

## 4. Controller-Only Allowlist

The source boundary and dependency gate strictly enforces that all direct declared and imported dependencies must belong to this allowlist:

1. **Direct ROS Packages**: `rclpy`, `std_msgs`, `std_srvs`, `geometry_msgs`, `nav_msgs`, `sensor_msgs`, `ros_robot_controller_msgs`, `ros_robot_controller`, `controller`, `ubuntu_tank_safety`, `ubuntu_tank_supervisor`, `ubuntu_tank_teleop`.
2. **Build, Test, and Launch Tooling**: `ament_cmake`, `ament_copyright`, `ament_flake8`, `ament_lint_auto`, `ament_lint_common`, `ament_pep257`, `python3-pytest`, `launch`, `launch_ros`, `rosidl_default_generators`, `rosidl_default_runtime`.
3. **System Dependencies**: `python3-serial`, `python3-yaml`, `setuptools`.
4. **Base ROS Plumbing**: `builtin_interfaces`, `action_msgs`, `unique_identifier_msgs`, `rcl`, `rcutils`, `rcpputils`, `rmw`, `rmw_implementation`, `rosidl_runtime_c`, `rosidl_runtime_cpp`, `rosidl_runtime_py`, `rosidl_typesupport_*`, `tracetools`.
5. **Python Standard Library**: `math`, `os`, `sys`, `time`, `signal`, `threading`, `socket`, `struct`, `select`, `enum`, `queue`, `termios`, `tty`, `typing`, `unittest`, `glob`.

---

## 5. Perception and AI Exclusion Boundary

Perception, camera, LiDAR, and AI packages are strictly excluded from both the dependency manifests and the source code:

- **Forbidden ROS packages**: `cv_bridge`, `image_transport`, `camera_info_manager`, `depthimage_to_laserscan`, `laser_geometry`, `slam_toolbox`, `cartographer`, `rtabmap`, `pcl_ros`, `pcl_conversions`, `opencv2`, `vision_opencv`, `nav2_*`, `robot_localization`.
- **Forbidden message types**: `sensor_msgs/msg/Image`, `sensor_msgs/msg/CompressedImage`, `sensor_msgs/msg/LaserScan`, `sensor_msgs/msg/PointCloud2`, `sensor_msgs/msg/CameraInfo`.
  - *Note*: While `sensor_msgs` is present in direct declarations for chassis IMU (`sensor_msgs/msg/Imu`), SBUS/joystick (`sensor_msgs/msg/Joy`), and joint state (`sensor_msgs/msg/JointState`), the AST-level import gate enforces that no camera or LiDAR types are imported.
- **Forbidden AI/Vision libraries**: `cv2`, `torch`, `torchvision`, `mediapipe`, `ultralytics`, `yolov5`, `pygame`.
