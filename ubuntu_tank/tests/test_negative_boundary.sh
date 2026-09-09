#!/usr/bin/env bash
# ubuntu_tank/tests/test_negative_boundary.sh
# Negative regression test verifying that test_source_boundary.sh catches and rejects
# omitted cross-workspace and external package dependencies.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "============================================================"
echo "Running Negative Boundary Regression Tests"
echo "============================================================"

TMP_DIR="$(mktemp -d /tmp/mentorpi_neg_test_XXXXXX)"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

create_test_clone() {
  local target="$1"
  git clone --shared "${WORKSPACE_ROOT}" "${target}" >/dev/null 2>&1
  # Synchronize working tree files into clone (to support testing with uncommitted working-tree edits)
  cp -a "${WORKSPACE_ROOT}/ubuntu_tank" "${target}/"
  git -C "${target}" add ubuntu_tank >/dev/null 2>&1
}

echo "[Test 1/2] Verifying omitted cross-workspace dependency rejection..."
CLONE_DIR1="${TMP_DIR}/repo1"
create_test_clone "${CLONE_DIR1}"

# Case 1: Remove cross-workspace dependency <depend>ros_robot_controller_msgs</depend> from ubuntu_tank_safety
python3 - <<PYCASE1
import os
import hashlib

repo_dir = "${CLONE_DIR1}"
pkg_xml = os.path.join(repo_dir, "ubuntu_tank/src/ubuntu_tank_safety/package.xml")
with open(pkg_xml, "r", encoding="utf-8") as f:
    content = f.read()

target = "<depend>ros_robot_controller_msgs</depend>\n"
if target not in content:
    raise RuntimeError("Target dependency line '<depend>ros_robot_controller_msgs</depend>' not found in package.xml")
new_content = content.replace(target, "")
with open(pkg_xml, "w", encoding="utf-8") as f:
    f.write(new_content)

# Update the hash in source-manifest.txt so Stage 1 passes and Stage 4 AST check is reached
new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
manifest_path = os.path.join(repo_dir, "ubuntu_tank/source-manifest.txt")
with open(manifest_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "destination: ubuntu_tank/src/ubuntu_tank_safety/package.xml" in line:
        for j in range(i, min(i + 10, len(lines))):
            if lines[j].startswith("destination_sha256:"):
                lines[j] = f"destination_sha256: {new_hash}\n"
                break
        break

with open(manifest_path, "w", encoding="utf-8") as f:
    f.writelines(lines)
PYCASE1

# Execute test_source_boundary.sh and assert it fails at Stage 4 with the expected error message
set +e
OUT1=$(bash "${CLONE_DIR1}/ubuntu_tank/tests/test_source_boundary.sh" 2>&1)
EXIT_CODE1=$?
set -e

if [ "${EXIT_CODE1}" -eq 0 ]; then
  echo "FAIL: Expected test_source_boundary.sh to fail on omitted cross-workspace dependency, but it passed!" >&2
  echo "${OUT1}" >&2
  exit 1
fi

if ! echo "${OUT1}" | grep -q "Package 'ubuntu_tank_safety' imports 'ros_robot_controller_msgs' but does not declare 'ros_robot_controller_msgs' in package.xml"; then
  echo "FAIL: test_source_boundary.sh failed, but did not report expected cross-workspace undeclared dependency message!" >&2
  echo "Output was:" >&2
  echo "${OUT1}" >&2
  exit 1
fi

echo "PASS: test_source_boundary.sh successfully rejected omitted cross-workspace dependency 'ros_robot_controller_msgs'."

echo "[Test 2/2] Verifying omitted external ROS dependency rejection..."
# Case 2: In a fresh clone, remove external dependency <depend>geometry_msgs</depend> from ubuntu_tank_teleop
CLONE_DIR2="${TMP_DIR}/repo2"
create_test_clone "${CLONE_DIR2}"

python3 - <<PYCASE2
import os
import hashlib

repo_dir = "${CLONE_DIR2}"
pkg_xml = os.path.join(repo_dir, "ubuntu_tank/src/ubuntu_tank_teleop/package.xml")
with open(pkg_xml, "r", encoding="utf-8") as f:
    content = f.read()

target = "<depend>geometry_msgs</depend>\n"
if target not in content:
    raise RuntimeError("Target dependency line '<depend>geometry_msgs</depend>' not found in package.xml")
new_content = content.replace(target, "")
with open(pkg_xml, "w", encoding="utf-8") as f:
    f.write(new_content)

new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
manifest_path = os.path.join(repo_dir, "ubuntu_tank/source-manifest.txt")
with open(manifest_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "destination: ubuntu_tank/src/ubuntu_tank_teleop/package.xml" in line:
        for j in range(i, min(i + 10, len(lines))):
            if lines[j].startswith("destination_sha256:"):
                lines[j] = f"destination_sha256: {new_hash}\n"
                break
        break

with open(manifest_path, "w", encoding="utf-8") as f:
    f.writelines(lines)
PYCASE2

set +e
OUT2=$(bash "${CLONE_DIR2}/ubuntu_tank/tests/test_source_boundary.sh" 2>&1)
EXIT_CODE2=$?
set -e

if [ "${EXIT_CODE2}" -eq 0 ]; then
  echo "FAIL: Expected test_source_boundary.sh to fail on omitted external dependency, but it passed!" >&2
  echo "${OUT2}" >&2
  exit 1
fi

if ! echo "${OUT2}" | grep -q "Package 'ubuntu_tank_teleop' imports 'geometry_msgs' but does not declare 'geometry_msgs' in package.xml"; then
  echo "FAIL: test_source_boundary.sh failed, but did not report expected external undeclared dependency message!" >&2
  echo "Output was:" >&2
  echo "${OUT2}" >&2
  exit 1
fi

echo "PASS: test_source_boundary.sh successfully rejected omitted external dependency 'geometry_msgs'."
echo "============================================================"
echo "All Negative Boundary Regression Tests PASSED successfully!"
echo "============================================================"
