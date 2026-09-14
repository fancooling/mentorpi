#!/usr/bin/env bash
# Verify workspace layout and declared imports/dependencies for the controller-only scope.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UBUNTU_TANK_DIR="${WORKSPACE_ROOT}/ubuntu_tank"
SRC_DIR="${UBUNTU_TANK_DIR}/src"

echo "============================================================"
echo "Running Ubuntu Tank Source Boundary Gate"
echo "============================================================"

echo "[1/3] Checking required directory layout assertion against Git repository..."
python3 - <<LAYOUTCHECK
import os
import sys
import subprocess

root = "${WORKSPACE_ROOT}"
tank_dir = "${UBUNTU_TANK_DIR}"
required_dirs = ['bin', 'config', 'config/fastdds', 'config/sros2', 'host', 'scripts', 'src', 'tests', 'docs']
required_files = [
    'bin/mentorpi-tank-run',
    'config/controller.yaml',
    'config/fastdds/loopback.xml',
    'config/sros2/README.md',
    'host/99-mentorpi-rrc.rules',
    'host/mentorpi-tank.service',
    'host/mentorpi-tank-recover.service',
    'host/mentorpi-tank.env',
    'host/ubuntu-tank.conf',
    'scripts/install_ros2.sh',
    'scripts/build_workspace.sh',
    'scripts/check_host.sh',
    'scripts/recover_activation.sh',
    'scripts/verify_runtime.sh',
    'scripts/deployment_manager.py',
    'scripts/config_migration.py',
    'scripts/fastdds_setup.py',
    'docs/RELEASE_MANIFEST_SPEC.md',
    'docs/DEPENDENCY_CLOSURE.md',
    'versions.lock',
    'README.md',
    'VERSION',
    'deploy.sh',
    'tests/test_source_boundary.sh',
    'tests/test_negative_boundary.sh',
    'tests/test_dependency_closure.sh',
    'tests/test_install_workflow.py',
    'tests/test_milestone3_port.py',
    'tests/test_milestone5_deployment.py',
]


try:
    git_tracked_out = subprocess.check_output(['git', 'ls-files', 'ubuntu_tank'], cwd=root, text=True)
    git_tracked_set = set(line.strip() for line in git_tracked_out.splitlines() if line.strip())
except Exception as e:
    print(f"FAIL: Could not query git ls-files: {e}", file=sys.stderr)
    sys.exit(1)

missing = []
for d in required_dirs:
    p = os.path.join(tank_dir, d)
    if not os.path.isdir(p):
        missing.append(f"Directory missing on disk: {d}")

for f in required_files:
    p = os.path.join(tank_dir, f)
    rel_f = f"ubuntu_tank/{f}"
    if not os.path.isfile(p):
        missing.append(f"File missing on disk: {f}")
    elif rel_f not in git_tracked_set:
        missing.append(f"Required file is not tracked by Git: {rel_f}")

if missing:
    print("FAIL: Workspace layout assertion failed:", file=sys.stderr)
    for m in missing:
        print(f"  - {m}", file=sys.stderr)
    sys.exit(1)

print(f"PASS: Verified complete required directory layout and tracked scaffold files in Git repository.")
LAYOUTCHECK

echo "[2/3] AST-based Python import audit against declared package manifests..."
python3 - <<ASTCHECK
import os
import ast
import sys
import xml.etree.ElementTree as ET

src_dir = "${SRC_DIR}"

IMPORT_TO_PKG_MAP = {
    'yaml': 'python3-yaml',
    'serial': 'python3-serial',
    'pytest': 'python3-pytest',
    'launch': 'launch',
    'launch_ros': 'launch_ros',
    'setuptools': 'setuptools',
}

STDLIB = {
    'os', 'sys', 'time', 'math', 'signal', 'threading', 'socket', 'struct',
    'select', 'enum', 'queue', 'termios', 'tty', 'typing', 'unittest', 'glob',
    'subprocess', 'tempfile', 'hashlib', 'ast', 'xml', 'json'
}

dep_tags = {'depend', 'build_depend', 'build_export_depend', 'exec_depend', 'test_depend'}

def get_package_imports_and_declared(pkg, pkg_path, xml_path, omit_declared=None):
    declared = set()
    if os.path.exists(xml_path):
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for child in root:
            if child.tag in dep_tags and child.text:
                declared.add(child.text.strip())
    if omit_declared:
        declared -= set(omit_declared)

    # Identify internal modules strictly belonging to this package's namespace
    pkg_internal_modules = {pkg}
    for entry in os.listdir(pkg_path):
        if entry.endswith('.py'):
            pkg_internal_modules.add(entry[:-3])
        elif os.path.isdir(os.path.join(pkg_path, entry)) and os.path.exists(os.path.join(pkg_path, entry, '__init__.py')):
            pkg_internal_modules.add(entry)

    # Scan all python files in this package
    pkg_imports = set()
    for dirpath, _, filenames in os.walk(pkg_path):
        for fn in filenames:
            if fn.endswith('.py'):
                fp = os.path.join(dirpath, fn)
                with open(fp, 'r', encoding='utf-8') as fh:
                    tree = ast.parse(fh.read(), filename=fp)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            pkg_imports.add(alias.name.split('.')[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and getattr(node, 'level', 0) == 0:
                            pkg_imports.add(node.module.split('.')[0])

    # Note: Cross-workspace packages are NOT excluded from external_imports.
    # Every package importing another workspace package must declare that dependency.
    external_imports = pkg_imports - STDLIB - pkg_internal_modules - {'setuptools'}
    errors = []
    for mod in sorted(external_imports):
        expected_dep = IMPORT_TO_PKG_MAP.get(mod, mod)
        if expected_dep not in declared and mod not in declared:
            errors.append(f"Package '{pkg}' imports '{mod}' but does not declare '{expected_dep}' in package.xml")
    return errors

pkgs = [d for d in os.listdir(src_dir) if os.path.isdir(os.path.join(src_dir, d))]
undeclared_errors = []

for pkg in sorted(pkgs):
    pkg_path = os.path.join(src_dir, pkg)
    xml_path = os.path.join(pkg_path, 'package.xml')
    if not os.path.exists(xml_path):
        continue
    errs = get_package_imports_and_declared(pkg, pkg_path, xml_path)
    undeclared_errors.extend(errs)

if undeclared_errors:
    print("FAIL: Python AST import audit found undeclared dependencies in package manifests:", file=sys.stderr)
    for err in undeclared_errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)

# In-line negative regression test: prove that omitting a cross-workspace dependency fails
safety_path = os.path.join(src_dir, 'ubuntu_tank_safety')
safety_xml = os.path.join(safety_path, 'package.xml')
neg_errors = get_package_imports_and_declared('ubuntu_tank_safety', safety_path, safety_xml, omit_declared={'ros_robot_controller_msgs'})
expected_neg_msg = "Package 'ubuntu_tank_safety' imports 'ros_robot_controller_msgs' but does not declare 'ros_robot_controller_msgs' in package.xml"
if not any(expected_neg_msg in e for e in neg_errors):
    print("FAIL: In-line AST audit self-test failed to reject omitted cross-workspace dependency!", file=sys.stderr)
    sys.exit(1)

print("PASS: 100% of direct runtime Python imports (including cross-workspace packages) are formally declared in package manifests.")
print("PASS: In-line AST audit self-test confirmed detection and rejection of omitted cross-workspace dependency.")
ASTCHECK

echo "[3/3] Direct package dependency audit, controller allowlist, and perception exclusion gate..."
python3 - <<DEPCHECK
import os
import ast
import sys
import xml.etree.ElementTree as ET

src_dir = "${SRC_DIR}"

CONTROLLER_ALLOWLIST = {
    # Direct ROS packages
    'rclpy', 'std_msgs', 'std_srvs', 'geometry_msgs', 'nav_msgs', 'sensor_msgs',
    'ros_robot_controller_msgs', 'ros_robot_controller', 'controller',
    'ubuntu_tank_safety', 'ubuntu_tank_supervisor', 'ubuntu_tank_teleop',
    'ubuntu_tank_bringup',
    # Tooling / generators / test
    'ament_cmake', 'ament_copyright', 'ament_flake8', 'ament_lint_auto',
    'ament_lint_common', 'ament_pep257', 'python3-pytest', 'launch',
    'launch_ros', 'rosidl_default_generators', 'rosidl_default_runtime',
    # System dependencies
    'python3-serial', 'python3-yaml', 'setuptools'
}

FORBIDDEN_PACKAGES = {
    'cv_bridge', 'image_transport', 'camera_info_manager', 'depthimage_to_laserscan',
    'laser_geometry', 'slam_toolbox', 'cartographer', 'rtabmap', 'pcl_ros',
    'pcl_conversions', 'opencv2', 'vision_opencv', 'nav2_common', 'nav2_msgs',
    'nav2_core', 'nav2_bringup', 'nav2_costmap_2d', 'nav2_planner', 'nav2_controller',
    'nav2_bt_navigator', 'robot_localization', 'torch', 'torchvision', 'mediapipe',
    'ultralytics', 'yolov5', 'pygame'
}

FORBIDDEN_SENSOR_TYPES = {
    'Image', 'CompressedImage', 'LaserScan', 'PointCloud', 'PointCloud2',
    'CameraInfo', 'ChannelFloat32', 'Range', 'NavSatFix', 'Illuminance'
}

dep_tags = {'depend', 'build_depend', 'build_export_depend', 'exec_depend', 'test_depend'}

all_declared_deps = set()
violations = []

# 1. Audit all declared dependencies against allowlist and forbidden list
for dirpath, _, filenames in os.walk(src_dir):
    if 'package.xml' in filenames:
        pkg_xml_path = os.path.join(dirpath, 'package.xml')
        try:
            tree = ET.parse(pkg_xml_path)
            root_el = tree.getroot()
            pkg_name = root_el.findtext('name', default='unknown')
            for child in root_el:
                if child.tag in dep_tags and child.text:
                    dep = child.text.strip()
                    all_declared_deps.add(dep)
                    if dep in FORBIDDEN_PACKAGES:
                        violations.append(f"Package '{pkg_name}' declares forbidden perception/AI package: '{dep}'")
                    if dep not in CONTROLLER_ALLOWLIST:
                        violations.append(f"Package '{pkg_name}' declares package outside controller allowlist: '{dep}'")
        except Exception as e:
            violations.append(f"Failed to parse {pkg_xml_path}: {e}")

# 2. AST inspection: ensure sensor_msgs imports only non-perception types (Imu, Joy, JointState)
sensor_type_violations = []
for root, _, files in os.walk(src_dir):
    for fn in files:
        if fn.endswith('.py'):
            fp = os.path.join(root, fn)
            with open(fp, 'r', encoding='utf-8') as fh:
                tree = ast.parse(fh.read(), filename=fp)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and 'sensor_msgs' in node.module:
                        for alias in node.names:
                            if alias.name in FORBIDDEN_SENSOR_TYPES:
                                sensor_type_violations.append(f"{fp}: imports forbidden perception type '{alias.name}'")
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name) and node.value.id == 'sensor_msgs':
                        if node.attr in FORBIDDEN_SENSOR_TYPES:
                            sensor_type_violations.append(f"{fp}: accesses forbidden perception attribute '{node.attr}'")

violations.extend(sensor_type_violations)

if violations:
    print("FAIL: Dependency closure or perception exclusion gate violated:", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    sys.exit(1)

sorted_deps = sorted(all_declared_deps)
print(f"PASS: Direct ROS package dependency declarations verified ({len(sorted_deps)} distinct dependencies strictly within controller allowlist):")
print(f"      {', '.join(sorted_deps)}")
print(f"PASS: Direct AST inspection confirmed zero camera/LiDAR/perception types imported from sensor_msgs.")
DEPCHECK

# Supplemental text pattern scan
FORBIDDEN_PATTERNS=(
  "import cv2"
  "from cv2"
  "OpenCV"
  "yolov5"
  "import torch"
  "from torch"
  "mediapipe"
  "LaserScan"
  "sensor_msgs/msg/LaserScan"
  "sensor_msgs/msg/Image"
  "sensor_msgs/msg/CompressedImage"
  "sensor_msgs/msg/PointCloud2"
  "sensor_msgs/msg/CameraInfo"
  "nav2_common"
  "nav2_msgs"
  "robot_localization"
)

VIOLATIONS=0
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  MATCHES=$(grep -rn "${pattern}" "${SRC_DIR}" || true)
  if [ -n "${MATCHES}" ]; then
    echo "FAIL: Forbidden pattern '${pattern}' found in controller source text:" >&2
    echo "${MATCHES}" >&2
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
done

if [ "${VIOLATIONS}" -gt 0 ]; then
  echo "FAIL: Found ${VIOLATIONS} forbidden dependency violations in runtime closure." >&2
  exit 1
fi
echo "PASS: No perception, LiDAR, camera, Nav2, or AI tokens in source text."

echo "============================================================"
echo "Source Boundary Gate PASSED successfully!"
echo "============================================================"
