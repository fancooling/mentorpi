#!/usr/bin/env bash
# test_dependency_closure.sh - Automated Dependency Closure and Lockfile Verification Gate
# Implements Milestone 2 dependency closure validation and negative regression tests.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TANK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="${TANK_DIR}/versions.lock"
INSTALL_SCRIPT="${TANK_DIR}/scripts/install_ros2.sh"

echo "============================================================"
echo "Running Dependency Closure and Lockfile Verification Gate"
echo "============================================================"

# Stage 1: Official verify-lock invocation
echo ""
echo "[1/3] Running Authoritative verify-lock on versions.lock..."
bash "${INSTALL_SCRIPT}" verify-lock

# Stage 2: Direct dependency manifest coverage audit
echo ""
echo "[2/3] Auditing package.xml declarations against locked closure..."
python3 - "${TANK_DIR}" <<'PYAUDIT'
import os
import sys
import xml.etree.ElementTree as ET

tank_dir = sys.argv[1]
lock_path = os.path.join(tank_dir, "versions.lock")
src_dir = os.path.join(tank_dir, "src")

def parse_lock(filepath):
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass

    data = {"packages": [], "excluded_perception": []}
    current_section = None
    current_item = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            raw_line = line.rstrip()
            line_str = raw_line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if line_str == "packages:":
                current_section = "packages"
                continue
            elif line_str == "excluded_perception:":
                current_section = "excluded_perception"
                continue
            if current_section in ("packages", "excluded_perception"):
                if line_str.startswith("- name:"):
                    current_item = {"name": line_str.split(":", 1)[1].strip().strip("'\"")}
                    data[current_section].append(current_item)
                elif current_item is not None and ":" in line_str:
                    k, v = line_str.split(":", 1)
                    current_item[k.strip()] = v.strip().strip("'\"")
    return data

lock = parse_lock(lock_path)
locked_pkgs = {p["name"]: p for p in lock.get("packages", [])}
forbidden_pkgs = {item["name"] for item in lock.get("excluded_perception", [])}

DEP_MAP = {
    "rclpy": "ros-lyrical-rclpy",
    "std_msgs": "ros-lyrical-std-msgs",
    "std_srvs": "ros-lyrical-std-srvs",
    "geometry_msgs": "ros-lyrical-geometry-msgs",
    "nav_msgs": "ros-lyrical-nav-msgs",
    "sensor_msgs": "ros-lyrical-sensor-msgs",
    "launch": "ros-lyrical-launch",
    "launch_ros": "ros-lyrical-launch-ros",
    "rosidl_default_runtime": "ros-lyrical-rosidl-default-runtime",
    "rosidl_default_generators": "ros-lyrical-rosidl-default-generators",
    "ament_cmake": "ros-lyrical-ament-cmake",
    "ament_lint_auto": "ros-lyrical-ament-lint-auto",
    "ament_lint_common": "ros-lyrical-ament-lint-common",
    "ament_copyright": "ros-lyrical-ament-copyright",
    "ament_flake8": "ros-lyrical-ament-flake8",
    "ament_pep257": "ros-lyrical-ament-pep257",
    "python3-serial": "python3-serial",
    "python3-yaml": "python3-yaml",
    "python3-pytest": "python3-pytest",
    "setuptools": "python3-setuptools",
}

internal_pkgs = {"ros_robot_controller", "ros_robot_controller_msgs", "controller", "ubuntu_tank_safety", "ubuntu_tank_supervisor", "ubuntu_tank_teleop"}

declared_deps = set()
tags_to_scan = {"depend", "build_depend", "buildtool_depend", "build_export_depend", "buildtool_export_depend", "exec_depend", "test_depend"}
for root, _, files in os.walk(src_dir):
    if "package.xml" in files:
        tree = ET.parse(os.path.join(root, "package.xml"))
        for el in tree.getroot():
            if el.tag in tags_to_scan:
                if el.text:
                    declared_deps.add(el.text.strip())

external_deps = declared_deps - internal_pkgs
missing = []
for dep in sorted(external_deps):
    apt_name = DEP_MAP.get(dep, dep)
    if apt_name not in locked_pkgs:
        missing.append(f"Declared dependency '{dep}' (apt package: '{apt_name}') not found in locked packages")

if missing:
    print(f"FAIL: {len(missing)} declared dependencies are missing from lockfile:", file=sys.stderr)
    for m in missing:
        print(f"  - {m}", file=sys.stderr)
    sys.exit(1)

print(f"PASS: 100% of workspace declared external dependencies ({len(external_deps)}) across all tags are locked in versions.lock.")
PYAUDIT

# Stage 3: Negative regression test suite (tamper detection)
echo ""
echo "[3/3] Running Negative Regression Tests (Tamper Detection)..."

run_negative_test() {
  local test_name="$1"
  local mutator_code="$2"
  local expected_err="$3"

  local tmp_lock
  tmp_lock="$(mktemp "${TANK_DIR}/versions.lock.tmp.XXXXXX")"
  python3 -c "${mutator_code}" "${LOCK_FILE}" "${tmp_lock}"

  set +e
  local output
  output="$(LOCK_FILE="${tmp_lock}" bash "${INSTALL_SCRIPT}" verify-lock 2>&1)"
  local exit_code=$?
  set -e
  rm -f "${tmp_lock}"

  if [ ${exit_code} -eq 0 ]; then
    echo "FAIL: Negative test '${test_name}' unexpectedly passed!" >&2
    exit 1
  fi

  if ! echo "${output}" | grep -qi "${expected_err}"; then
    echo "FAIL: Negative test '${test_name}' failed with unexpected error message:" >&2
    echo "${output}" >&2
    echo "Expected snippet: '${expected_err}'" >&2
    exit 1
  fi

  echo "PASS: [Negative] '${test_name}' successfully rejected with: '${expected_err}'"
}

# Negative Test 1: Unpinned version
run_negative_test "Reject unpinned version containing 'latest'" 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); d["packages"][0]["version"] = "latest"; yaml.dump(d, open(sys.argv[2], "w"))' "unpinned version"

# Negative Test 2: Forbidden perception package injection
run_negative_test "Reject forbidden perception package in locked closure" 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); d["packages"].append({"name": "cv_bridge", "version": "1.0.0", "architecture": "arm64", "repository": "ros2", "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}); yaml.dump(d, open(sys.argv[2], "w"))' "Forbidden perception package"

# Negative Test 3: Invalid SHA256 checksum
run_negative_test "Reject invalid non-hex / truncated SHA256 checksum" 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); d["packages"][0]["sha256"] = "invalid_short_hash"; yaml.dump(d, open(sys.argv[2], "w"))' "sha256 must be 64-char hex string"

# Negative Test 4: Omitted required dependency
run_negative_test "Reject lockfile with omitted declared dependency" 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); d["packages"] = [p for p in d["packages"] if p["name"] != "ros-lyrical-rclpy"]; yaml.dump(d, open(sys.argv[2], "w"))' "missing from versions.lock"

# Negative Test 5: apt source URL containing latest
run_negative_test "Reject ros2-apt-source URL containing 'latest'" 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); d["ros_apt_source"]["url"] = "https://repo.ros2.org/ubuntu/ros2-apt-source/latest.deb"; yaml.dump(d, open(sys.argv[2], "w"))' "cannot contain 'latest'"

run_negative_closure_test() {
  local test_name="$1"
  local manifest_content="$2"
  local expected_err="$3"

  local tmp_cand
  tmp_cand="$(mktemp "${TANK_DIR}/candidates.tmp.XXXXXX")"
  echo "${manifest_content}" >"${tmp_cand}"

  set +e
  local output
  output="$(bash "${INSTALL_SCRIPT}" verify-closure --candidates "${tmp_cand}" 2>&1)"
  local exit_code=$?
  set -e
  rm -f "${tmp_cand}"

  if [ ${exit_code} -eq 0 ]; then
    echo "FAIL: Negative closure test '${test_name}' unexpectedly passed!" >&2
    exit 1
  fi

  if ! echo "${output}" | grep -qi "${expected_err}"; then
    echo "FAIL: Negative closure test '${test_name}' failed with unexpected error message:" >&2
    echo "${output}" >&2
    echo "Expected snippet: '${expected_err}'" >&2
    exit 1
  fi

  echo "PASS: [Negative] '${test_name}' successfully rejected with: '${expected_err}'"
}

# Negative Test 6: Valid-looking 64-char hash that is incorrect
run_negative_closure_test \
  "Reject candidate package with valid-looking but incorrect 64-character hash" \
  "ros-lyrical-rclpy 7.1.0-1resolute arm64 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
  "Checksum mismatch for package"

# Negative Test 7: Added / unlocked transitive package in candidate solver plan
run_negative_closure_test \
  "Reject added / unlocked transitive package in candidate closure" \
  "unlocked-transitive-lib 1.0.0-1 arm64 a275b9b819874e745a928e83e39c429fa4d607159285c4ef3ebcf75afa732ee3" \
  "Unlocked transitive package"

# Negative Test 8: Missing candidate manifest argument
set +e
output="$(bash "${INSTALL_SCRIPT}" verify-closure 2>&1)"
exit_code=$?
set -e
if [ ${exit_code} -eq 0 ]; then
  echo "FAIL: verify-closure without --candidates unexpectedly succeeded!" >&2
  exit 1
fi
if ! echo "${output}" | grep -q "verify-closure requires a candidate solver manifest"; then
  echo "FAIL: verify-closure without --candidates failed with unexpected error: ${output}" >&2
  exit 1
fi
echo "PASS: [Negative] 'Reject missing candidate manifest' successfully rejected with: 'verify-closure requires a candidate solver manifest'"

echo ""
echo "============================================================"
echo "Dependency Closure and Lockfile Verification Gate PASSED!"
echo "============================================================"
