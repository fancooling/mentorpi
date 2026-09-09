#!/usr/bin/env bash
# test_source_boundary.sh - Verify source provenance, fallback constraints, AST imports, and dependency closure
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UBUNTU_TANK_DIR="${WORKSPACE_ROOT}/ubuntu_tank"
SRC_DIR="${UBUNTU_TANK_DIR}/src"
MANIFEST="${UBUNTU_TANK_DIR}/source-manifest.txt"

echo "============================================================"
echo "Running Ubuntu Tank Source Boundary & Provenance Gate"
echo "============================================================"

if [ ! -f "${MANIFEST}" ]; then
  echo "FAIL: Source manifest missing: ${MANIFEST}" >&2
  exit 1
fi

echo "[1/5] Checking manifest coverage, provenance semantics, and file hashes against Git repository..."
python3 - <<PYCHECK
import os
import sys
import hashlib
import subprocess

root = "${WORKSPACE_ROOT}"
manifest_path = "${MANIFEST}"
tank_dir = "${UBUNTU_TANK_DIR}"

entries = {}
dest_seen = set()
current_entry = {}
errors = []

# Parse manifest
with open(manifest_path, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            if current_entry and 'destination' in current_entry:
                dest = current_entry['destination']
                if dest in dest_seen:
                    errors.append(f"Duplicate destination entry in manifest: {dest}")
                dest_seen.add(dest)
                entries[dest] = current_entry
                current_entry = {}
            continue
        if ':' in line:
            k, v = line.split(':', 1)
            current_entry[k.strip()] = v.strip()

if current_entry and 'destination' in current_entry:
    dest = current_entry['destination']
    if dest in dest_seen:
        errors.append(f"Duplicate destination entry in manifest: {dest}")
    dest_seen.add(dest)
    entries[dest] = current_entry

# Query authoritative Git tracking store
try:
    git_tracked_out = subprocess.check_output(['git', 'ls-files', 'ubuntu_tank'], cwd=root, text=True)
    git_tracked_files = [line.strip() for line in git_tracked_out.splitlines() if line.strip()]
except Exception as e:
    errors.append(f"Failed to query git ls-files: {e}")
    git_tracked_files = []

manifest_rel = os.path.relpath(manifest_path, root)
payload_tracked_files = [f for f in git_tracked_files if f != manifest_rel]

# Check 1: Every Git-tracked payload file must have a manifest entry
for rel in payload_tracked_files:
    if rel not in entries:
        errors.append(f"Tracked file missing from source manifest: {rel}")

# Check 1b: Every manifested file must be tracked in Git
for dest in entries.keys():
    if dest not in payload_tracked_files:
        errors.append(f"Manifested file is not tracked by git: {dest}")

# Check 1c: Check physical files on disk
ignored_subdirs = {'build', 'install', 'log', 'dist', '.work', '__pycache__'}
physical_files = []
for dirpath, dirnames, filenames in os.walk(tank_dir):
    dirnames[:] = [d for d in dirnames if d not in ignored_subdirs]
    for fn in filenames:
        if fn.endswith(('.pyc', '.pyo', '.tar.zst', '.sock')):
            continue
        if fn == 'source-manifest.txt':
            continue
        p = os.path.join(dirpath, fn)
        rel = os.path.relpath(p, root)
        physical_files.append(rel)

for rel in physical_files:
    if rel not in entries:
        errors.append(f"Unmanifested physical file found in workspace: {rel}")

# Check 2: Every manifested entry must exist, match hashes, and satisfy provenance rules
for dest, meta in entries.items():
    abs_dest = os.path.join(root, dest)
    if not os.path.exists(abs_dest):
        errors.append(f"Manifested destination does not exist on disk: {dest}")
        continue

    with open(abs_dest, 'rb') as f:
        actual_dest_sha = hashlib.sha256(f.read()).hexdigest()
    if actual_dest_sha != meta.get('destination_sha256'):
        errors.append(f"Destination hash mismatch for {dest}: expected {meta.get('destination_sha256')}, got {actual_dest_sha}")

    origin = meta.get('origin')
    if origin == 'mentorpi/src':
        src_rel = meta.get('source_path')
        commit = meta.get('source_commit')
        if not src_rel:
            errors.append(f"Missing source_path for copied entry {dest}")
            continue
        if not commit:
            errors.append(f"Missing source_commit for copied entry {dest}")
            continue

        try:
            cmd = ['git', 'cat-file', '-p', f"{commit}:{src_rel}"]
            git_blob = subprocess.check_output(cmd, cwd=root, stderr=subprocess.PIPE)
            actual_src_sha = hashlib.sha256(git_blob).hexdigest()
        except subprocess.CalledProcessError as e:
            errors.append(f"Failed to retrieve upstream file from git at {commit}:{src_rel}: {e}")
            continue

        if actual_src_sha != meta.get('source_sha256'):
            errors.append(f"Upstream git object hash mismatch for {src_rel} at {commit}: expected {meta.get('source_sha256')}, got {actual_src_sha}")

        status = meta.get('status')
        if status == 'identical':
            if actual_dest_sha != actual_src_sha:
                errors.append(f"Entry {dest} marked identical but content differs from git baseline {src_rel}")
        elif status == 'adapted':
            if not meta.get('adaptations'):
                errors.append(f"Entry {dest} marked adapted but has empty adaptations description")
        else:
            errors.append(f"Entry {dest} has invalid status for mentorpi/src origin: '{status}'")

    elif origin == 'authored':
        if 'source_commit' in meta:
            errors.append(f"Authored entry {dest} must not declare source_commit")
        if 'source_path' in meta:
            errors.append(f"Authored entry {dest} must not declare source_path")
        if not meta.get('rationale'):
            errors.append(f"Authored entry {dest} is missing rationale")
        if meta.get('status') != 'new':
            errors.append(f"Authored entry {dest} status must be 'new', got '{meta.get('status')}'")

    elif origin == 'fallback':
        required_fields = ['fallback_path', 'fallback_category', 'rationale', 'hash', 'decision']
        for rf in required_fields:
            if not meta.get(rf):
                errors.append(f"Fallback entry {dest} is missing required field: {rf}")
    else:
        errors.append(f"Unknown origin '{origin}' for entry {dest}")

if errors:
    print(f"FAILED with {len(errors)} provenance errors:", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)

print(f"PASS: Verified {len(entries)} payload files with provenance out of {len(git_tracked_files)} Git-tracked files (source-manifest.txt self-excluded).")
PYCHECK

echo "[2/5] Checking required directory layout assertion against Git repository..."
python3 - <<LAYOUTCHECK
import os
import sys
import subprocess

root = "${WORKSPACE_ROOT}"
tank_dir = "${UBUNTU_TANK_DIR}"
required_dirs = ['config', 'config/sros2', 'host', 'scripts', 'src', 'tests', 'docs']
required_files = [
    'config/controller.yaml',
    'config/sros2/README.md',
    'host/99-mentorpi-rrc.rules',
    'host/mentorpi-tank.service',
    'host/mentorpi-tank.env',
    'scripts/install_ros2.sh',
    'scripts/build_workspace.sh',
    'scripts/check_host.sh',
    'scripts/recover_activation.sh',
    'scripts/verify_runtime.sh',
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
    'source-manifest.txt'
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

echo "[3/5] Checking fallback constraints..."
if grep -q -E "^origin:[[:space:]]*fallback" "${MANIFEST}"; then
  echo "Fallback entries detected in manifest. Validating justifications..."
  echo "FAIL: Fallbacks to /mnt/rpi-rootfs are not authorized for Milestone 1" >&2
  exit 1
else
  echo "PASS: Zero fallbacks required; mentorpi/src is complete for Milestone 1."
fi

echo "[4/5] AST-based Python import audit against declared package manifests..."
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
    'subprocess', 'tempfile', 'hashlib', 'ast', 'xml'
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

echo "[5/5] Direct package dependency audit, controller allowlist, and perception exclusion gate..."
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
