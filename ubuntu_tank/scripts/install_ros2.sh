#!/usr/bin/env bash
# install_ros2.sh - Ubuntu and ROS repository/package setup script
# Implements Milestone 2: prepare-host, verify-lock, install-ros, and install-deps.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TANK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="${LOCK_FILE:-${TANK_DIR}/versions.lock}"
CHECK_HOST_SCRIPT="${SCRIPT_DIR}/check_host.sh"

# shellcheck source=./check_host.sh
if [ -f "${CHECK_HOST_SCRIPT}" ]; then
  . "${CHECK_HOST_SCRIPT}"
fi

DEPLOY_LOCK_FD=9

assert_no_mutation_overrides() {
  local dry_run="$1"
  local cmd_name="$2"

  if [ "${dry_run}" = "false" ]; then
    local violations=()
    local env_vars
    env_vars="$(env | grep -E '^UBUNTU_TANK_MOCK_' | cut -d= -f1 || true)"
    for var in ${env_vars}; do
      violations+=("${var}")
    done
    if [ -n "${UBUNTU_TANK_LOCK_DIR:-}" ]; then
      violations+=("UBUNTU_TANK_LOCK_DIR")
    fi
    if [ -n "${LOCK_FILE:-}" ] && [ "${LOCK_FILE}" != "${TANK_DIR}/versions.lock" ]; then
      violations+=("LOCK_FILE (${LOCK_FILE})")
    fi

    if [ ${#violations[@]} -gt 0 ]; then
      echo "SECURITY ERROR: Test override variables (${violations[*]}) cannot authorize live host mutations for '${cmd_name}'." >&2
      echo "Live host mutation requires a real target environment with canonical configuration." >&2
      return 1
    fi

    # Force canonical lockfile for live mutation
    LOCK_FILE="${TANK_DIR}/versions.lock"
  fi
  return 0
}

acquire_deployment_lock() {
  local dry_run="${1:-false}"
  local lock_dir
  local lock_file

  if [ "${dry_run}" = "true" ]; then
    lock_dir="${UBUNTU_TANK_LOCK_DIR:-${TANK_DIR}/.work/lock}"
    lock_file="${lock_dir}/deploy.lock"
  else
    lock_dir="/run/lock/ubuntu_tank"
    lock_file="${lock_dir}/deploy.lock"
  fi

  mkdir -p "${lock_dir}"
  if [ "${dry_run}" = "false" ]; then
    chmod 0755 "${lock_dir}"
  fi

  eval "exec ${DEPLOY_LOCK_FD}>\"${lock_file}\""
  if ! flock -n ${DEPLOY_LOCK_FD}; then
    echo "FAIL: Could not acquire deployment lock at ${lock_file}. Another operation is in progress." >&2
    return 1
  fi

  if [ "${dry_run}" = "false" ]; then
    chmod 0600 "${lock_file}"
  fi

  trap 'release_deployment_lock' EXIT INT TERM
}

release_deployment_lock() {
  flock -u ${DEPLOY_LOCK_FD} 2>/dev/null || true
  eval "exec ${DEPLOY_LOCK_FD}>&-" 2>/dev/null || true
}

# Require a target-generated, complete transitive package lock before any live
# ROS or workspace dependency installation. Hardware-free checks can validate
# the direct-package lock, but only the clean ARM64 target solver can establish
# the authoritative transitive artifact set.
require_complete_package_lock() {
  local closure_status
  closure_status="$(python3 - "${LOCK_FILE}" <<'PYCLOSURESTATUS'
import sys

status = ""
in_meta = False
with open(sys.argv[1], "r", encoding="utf-8") as lock_file:
    for raw_line in lock_file:
        stripped = raw_line.strip()
        if stripped == "meta:":
            in_meta = True
            continue
        if in_meta and raw_line and not raw_line[0].isspace():
            break
        if in_meta and stripped.startswith("closure_status:"):
            status = stripped.split(":", 1)[1].strip().strip("'\"")
            break

print(status)
PYCLOSURESTATUS
)"

  if [ "${closure_status}" != "complete" ]; then
    echo "FAIL: versions.lock transitive closure is '${closure_status:-unspecified}', not 'complete'." >&2
    echo "Capture and review the full clean-target ARM64 apt transaction, then lock every downloaded package before live installation." >&2
    return 1
  fi
}

usage() {
  cat <<'HELP'
Usage: ./scripts/install_ros2.sh <subcommand> [options]

Subcommands:
  prepare-host     Upgrade base Ubuntu packages, configure locales, and record host baseline
  verify-lock      Validate dependency lock integrity against versions.lock (pure data check)
  verify-closure   Validate candidate packages and solver closure against versions.lock
  install-ros      Install pinned ROS 2 Lyrical packages, SROS2, EEPROM, and build tools
  install-deps     Resolve and verify locked rosdep dependencies for workspace packages

Subcommand Options:
  -h, --help       Show help for the given subcommand
  --dry-run        Print actions without making system modifications

Environment Overrides (for test harness only):
  UBUNTU_TANK_MOCK_TARGET=1   Simulate target environment (FORBIDDEN with live mutations)
HELP
}

# Resolve reboot check file path (respecting mock overrides in test harness)
resolve_reboot_check_file() {
  local reboot_file="/run/reboot-required"
  if [ -n "${UBUNTU_TANK_MOCK_REBOOT_FILE:-}" ]; then
    reboot_file="${UBUNTU_TANK_MOCK_REBOOT_FILE}"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    reboot_file="/tmp/nonexistent_ubuntu_tank_reboot_required"
  fi
  echo "${reboot_file}"
}

# Check for required reboot
check_reboot_pending() {
  local reboot_check_file
  reboot_check_file="$(resolve_reboot_check_file)"
  if [ -f "${reboot_check_file}" ]; then
    echo "FAIL: System reboot is required (${reboot_check_file} exists)." >&2
    echo "Please reboot the host and rerun preflight before continuing." >&2
    return 2
  fi
  return 0
}

# Recover partial apt state
recover_apt_state() {
  local SUDO=""
  if [ "$(id -u)" -ne 0 ] && [ -z "${UBUNTU_TANK_MOCK_NO_SUDO:-}" ]; then
    SUDO="sudo"
  fi
  echo "--> Running package state recovery (dpkg --configure -a, apt-get --fix-broken install)..."
  ${SUDO} dpkg --configure -a
  DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get --fix-broken install -y
}

# Comprehensive baseline capture (Deb822 and legacy sources, secure tmp, root-owned)
record_host_baseline() {
  local dest_file="$1"
  local title="$2"
  local reboot_pending="${3:-false}"

  local tmp_file
  tmp_file="$(mktemp /tmp/tank_baseline.XXXXXX)"
  chmod 0600 "${tmp_file}"

  {
    echo "# ${title}"
    echo "timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo "kernel: $(uname -r)"
    echo "architecture: $(dpkg --print-architecture 2>/dev/null || uname -m)"
    echo "reboot_pending: ${reboot_pending}"
    echo "os_release:"
    if [ -f /etc/os-release ]; then
      sed 's/^/  /' /etc/os-release
    fi
    echo "apt_sources:"
    for sf in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
      if [ -f "${sf}" ]; then
        echo "  --- file: ${sf} ---"
        sed 's/^/    /' "${sf}"
      fi
    done
    echo "installed_packages:"
    if command -v dpkg-query >/dev/null 2>&1; then
      dpkg-query -W -f='  ${Package}: ${Version} (${Architecture})\n'
    fi
  } > "${tmp_file}"

  mkdir -p "$(dirname "${dest_file}")"
  mv -f "${tmp_file}" "${dest_file}"
  chmod 0644 "${dest_file}"
  chown root:root "${dest_file}" 2>/dev/null || true
}

# Comprehensive accepted baseline validation
validate_host_baseline() {
  local target_file=""
  local dry_run="false"

  if [ "$#" -ge 1 ]; then
    if [ "$1" = "true" ] || [ "$1" = "false" ]; then
      dry_run="$1"
      target_file="${2:-}"
    else
      target_file="$1"
      dry_run="${2:-false}"
    fi
  fi

  local baseline_file="${target_file}"
  if [ -z "${baseline_file}" ]; then
    if [ "${dry_run}" = "true" ]; then
      baseline_file="${UBUNTU_TANK_MOCK_BASELINE_FILE:-${TANK_DIR}/.work/deployment/host-baseline.txt}"
      if [ ! -f "${baseline_file}" ] && [ -f "/var/opt/ubuntu_tank/deployment/host-baseline.txt" ]; then
        baseline_file="/var/opt/ubuntu_tank/deployment/host-baseline.txt"
      fi
    else
      baseline_file="/var/opt/ubuntu_tank/deployment/host-baseline.txt"
    fi
  fi

  if [ ! -f "${baseline_file}" ]; then
    echo "FAIL: Accepted host baseline not found at ${baseline_file}." >&2
    echo "Run './deploy.sh prepare-host' first to establish the baseline." >&2
    return 1
  fi

  if [ "${dry_run}" = "false" ]; then
    local owner_uid owner_gid perms mode_val
    owner_uid="$(stat -c %u "${baseline_file}" 2>/dev/null || echo 1)"
    if [ "${owner_uid}" -ne 0 ]; then
      echo "FAIL: Baseline file ${baseline_file} is not owned by root (UID ${owner_uid}). Refusing untrusted baseline." >&2
      return 1
    fi
    owner_gid="$(stat -c %g "${baseline_file}" 2>/dev/null || echo 1)"
    perms="$(stat -c %a "${baseline_file}" 2>/dev/null || echo "777")"
    mode_val=$(( 8#${perms} ))
    if (( (mode_val & 0002) != 0 )); then
      echo "FAIL: Baseline file ${baseline_file} has unsafe world-writable permissions (${perms})." >&2
      return 1
    fi
    if (( (mode_val & 0020) != 0 )) && [ "${owner_gid}" -ne 0 ]; then
      echo "FAIL: Baseline file ${baseline_file} has unsafe group-writable permissions (${perms}) with non-root group (GID ${owner_gid})." >&2
      return 1
    fi
  fi

  if ! grep -q "^# MentorPi Host Baseline - Accepted Ubuntu Baseline Snapshot" "${baseline_file}"; then
    echo "FAIL: Baseline file at ${baseline_file} is missing accepted header." >&2
    return 1
  fi

  for section in "timestamp:" "kernel:" "architecture:" "os_release:" "apt_sources:" "installed_packages:"; do
    if ! grep -q "^${section}" "${baseline_file}"; then
      echo "FAIL: Baseline file at ${baseline_file} is missing required section '${section}'." >&2
      return 1
    fi
  done

  local recorded_arch
  recorded_arch="$(grep -E "^architecture: " "${baseline_file}" | awk '{print $2}' || echo "")"
  local current_arch
  current_arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
  if [ -n "${recorded_arch}" ] && [ "${recorded_arch}" != "${current_arch}" ]; then
    echo "FAIL: Current architecture (${current_arch}) differs from baseline (${recorded_arch})." >&2
    return 1
  fi

  local recorded_kernel
  recorded_kernel="$(grep -E "^kernel: " "${baseline_file}" | awk '{print $2}' || echo "")"
  local running_kernel
  running_kernel="$(uname -r)"
  if [ -n "${recorded_kernel}" ] && [ "${recorded_kernel}" != "${running_kernel}" ]; then
    echo "FAIL: Running kernel (${running_kernel}) differs from recorded baseline kernel (${recorded_kernel})." >&2
    echo "If the host was rebooted after a kernel upgrade, rerun './deploy.sh prepare-host' to accept the new kernel baseline." >&2
    return 2
  fi

  local recorded_os_ver
  recorded_os_ver="$(grep -E "VERSION_ID=" "${baseline_file}" | head -n1 | cut -d= -f2 | tr -d '\"' || echo "")"
  if [ -f /etc/os-release ]; then
    local current_os_ver
    current_os_ver="$(grep -E "^VERSION_ID=" /etc/os-release | head -n1 | cut -d= -f2 | tr -d '\"' || echo "")"
    if [ -n "${recorded_os_ver}" ] && [ "${recorded_os_ver}" != "${current_os_ver}" ]; then
      echo "FAIL: OS release version (${current_os_ver}) differs from baseline (${recorded_os_ver})." >&2
      return 1
    fi
  fi

  return 0
}

# Verify lockfile integrity
cmd_verify_lock() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help|help)
        echo "Usage: ./deploy.sh verify-lock [-h|--help]"
        return 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        return 1
        ;;
    esac
  done

  echo "============================================================"
  echo "Verifying Dependency Lockfile Integrity (versions.lock)"
  echo "============================================================"

  if [ ! -f "${LOCK_FILE}" ]; then
    echo "FAIL: versions.lock not found at: ${LOCK_FILE}" >&2
    return 1
  fi

  python3 - "${LOCK_FILE}" "${TANK_DIR}" <<'PYVERIFY'
import os
import sys
import re
import xml.etree.ElementTree as ET

lock_path = sys.argv[1]
tank_dir = sys.argv[2]
src_dir = os.path.join(tank_dir, "src")

# Pure Python standard-library YAML parser (eliminates bootstrap cycle on clean hosts)
def parse_lock(filepath):
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass

    data = {"meta": {}, "ros_apt_source": {}, "rosdep_index": {}, "rosdep_sources": {}, "packages": [], "excluded_perception": []}
    current_section = None
    current_item = None
    current_sub = None

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            raw_line = line.rstrip()
            line_str = raw_line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            top_match = re.match(r"^([a-z_]+):$", line_str)
            if top_match and not line.startswith(" ") and not line.startswith("\t"):
                current_section = top_match.group(1)
                current_item = None
                current_sub = None
                if current_section not in data:
                    data[current_section] = {}
                continue

            if current_section in ("meta", "ros_apt_source", "rosdep_index"):
                kv = re.match(r"^\s*([a-z_]+):\s*(.*)$", line_str)
                if kv:
                    k, v = kv.group(1), kv.group(2).strip().strip("'\"")
                    data[current_section][k] = v

            elif current_section == "rosdep_sources":
                m_rev = re.match(r"^\s*revision:\s*(.*)$", line_str)
                if m_rev:
                    data["rosdep_sources"]["revision"] = m_rev.group(1).strip().strip("'\"")
                    continue
                m_sub = re.match(r"^\s*([a-z0-9_]+):$", line_str)
                if m_sub:
                    current_sub = m_sub.group(1)
                    data["rosdep_sources"][current_sub] = {}
                    continue
                if current_sub:
                    m_kv = re.match(r"^\s*([a-z0-9_]+):\s*(.*)$", line_str)
                    if m_kv:
                        data["rosdep_sources"][current_sub][m_kv.group(1)] = m_kv.group(2).strip().strip("'\"")

            elif current_section in ("packages", "excluded_perception"):
                new_item_match = re.match(r"^-\s+name:\s*(.*)$", line_str)
                if new_item_match:
                    current_item = {"name": new_item_match.group(1).strip().strip("'\"")}
                    data[current_section].append(current_item)
                elif current_item is not None:
                    sub_list = re.match(r"^-\s+(.*)$", line_str)
                    if sub_list:
                        for k, v in list(current_item.items()):
                            if isinstance(v, list):
                                v.append(sub_list.group(1).strip().strip("'\""))
                                break
                    else:
                        kv = re.match(r"^([a-z_]+):\s*(.*)$", line_str)
                        if kv:
                            k = kv.group(1)
                            v = kv.group(2).strip()
                            if not v:
                                current_item[k] = []
                            else:
                                current_item[k] = v.strip("'\"")

    return data

try:
    lock = parse_lock(lock_path)
except Exception as e:
    print(f"FAIL: Failed to parse versions.lock: {e}", file=sys.stderr)
    sys.exit(1)

errors = []

# 1. Validate meta section
meta = lock.get("meta", {})
if str(meta.get("format_version")) != "1.0.0":
    errors.append(f"Invalid format_version: expected '1.0.0', got '{meta.get('format_version')}'")
if meta.get("status") != "locked":
    errors.append(f"Lockfile status is not 'locked': '{meta.get('status')}'")
if meta.get("target_os") != "Ubuntu 26.04 LTS":
    errors.append(f"Invalid target_os: expected 'Ubuntu 26.04 LTS', got '{meta.get('target_os')}'")
if meta.get("target_os_codename") != "resolute":
    errors.append(f"Invalid target_os_codename: expected 'resolute', got '{meta.get('target_os_codename')}'")
if meta.get("target_arch") != "arm64":
    errors.append(f"Invalid target_arch: expected 'arm64', got '{meta.get('target_arch')}'")
if meta.get("ros_distro") != "lyrical":
    errors.append(f"Invalid ros_distro: expected 'lyrical', got '{meta.get('ros_distro')}'")
if meta.get("controller_mode") != "controller-only":
    errors.append(f"Invalid controller_mode: expected 'controller-only', got '{meta.get('controller_mode')}'")
closure_status = meta.get("closure_status")
if closure_status not in ("direct-only", "complete"):
    errors.append(
        "Invalid closure_status: expected 'direct-only' or 'complete', "
        f"got '{closure_status}'"
    )

# 2. Validate ros_apt_source
apt_source = lock.get("ros_apt_source", {})
for field in ["package_name", "version", "architecture", "url", "sha256"]:
    if not apt_source.get(field):
        errors.append(f"ros_apt_source missing required field: {field}")

apt_url = apt_source.get("url", "")
if not apt_url.startswith("https://"):
    errors.append(f"ros_apt_source URL must use HTTPS: {apt_url}")
if "latest" in apt_url.lower():
    errors.append(f"ros_apt_source URL must be pinned to exact version, cannot contain 'latest': {apt_url}")

apt_sha = apt_source.get("sha256", "")
if not re.match(r"^[0-9a-f]{64}$", apt_sha):
    errors.append(f"ros_apt_source sha256 must be a valid 64-char hex string: '{apt_sha}'")

# 3. Validate rosdep_sources / rosdep_index
rosdep_sources = lock.get("rosdep_sources", {})
if not rosdep_sources:
    rosdep_meta = lock.get("rosdep_index", {})
    if not rosdep_meta.get("revision"):
        errors.append("rosdep_sources / rosdep_index missing revision")
    if not rosdep_meta.get("source_url", "").startswith("https://"):
        errors.append(f"rosdep_index source_url must use HTTPS: {rosdep_meta.get('source_url')}")
    rosdep_sha = rosdep_meta.get("index_sha256", "")
    if not re.match(r"^[0-9a-f]{64}$", rosdep_sha):
        errors.append(f"rosdep_index index_sha256 must be a valid 64-char hex string: '{rosdep_sha}'")
else:
    if not rosdep_sources.get("revision"):
        errors.append("rosdep_sources missing revision")
    for key in ["index_v4", "base", "python", "ruby"]:
        entry = rosdep_sources.get(key, {})
        if not entry:
            errors.append(f"rosdep_sources missing required source entry: '{key}'")
            continue
        url = entry.get("url", "")
        if not url.startswith("https://"):
            errors.append(f"rosdep_sources '{key}' url must use HTTPS: '{url}'")
        if "latest" in url.lower() or "master" in url.lower():
            errors.append(f"rosdep_sources '{key}' url must be pinned to exact commit, cannot use latest/master: '{url}'")
        sha = entry.get("sha256", "")
        if not re.match(r"^[0-9a-f]{64}$", sha):
            errors.append(f"rosdep_sources '{key}' sha256 must be a valid 64-char hex string: '{sha}'")

# 4. Validate packages
pkgs = lock.get("packages", [])
if not pkgs:
    errors.append("No packages declared in versions.lock")

pkg_names = set()
for i, pkg in enumerate(pkgs):
    name = pkg.get("name")
    if not name:
        errors.append(f"Package index {i} missing name")
        continue
    if name in pkg_names:
        errors.append(f"Duplicate package in versions.lock: {name}")
    pkg_names.add(name)

    ver = str(pkg.get("version", ""))
    if not ver or "latest" in ver.lower() or "*" in ver or ">" in ver or "<" in ver or "~" in ver:
        errors.append(f"Package '{name}' has invalid or unpinned version: '{ver}'")

    arch = pkg.get("architecture")
    if arch not in ["arm64", "all"]:
        errors.append(f"Package '{name}' has invalid architecture '{arch}' (must be 'arm64' or 'all')")

    sha = pkg.get("sha256", "")
    if not re.match(r"^[0-9a-f]{64}$", sha):
        errors.append(f"Package '{name}' sha256 must be 64-char hex string, got '{sha}'")

    repo = pkg.get("repository")
    if not repo:
        errors.append(f"Package '{name}' missing repository identifier")

# 5. Validate excluded_perception
forbidden_declared = set()
for item in lock.get("excluded_perception", []):
    fn = item.get("name")
    if not fn:
        errors.append("excluded_perception entry missing name")
        continue
    if item.get("status") != "forbidden":
        errors.append(f"excluded_perception entry '{fn}' status must be 'forbidden'")
    forbidden_declared.add(fn)

required_forbidden = {
    "sensor_msgs/Image", "sensor_msgs/CompressedImage", "sensor_msgs/LaserScan",
    "sensor_msgs/PointCloud2", "sensor_msgs/CameraInfo", "cv_bridge",
    "image_transport", "laser_geometry", "slam_toolbox", "cartographer",
    "rtabmap", "nav2_*", "robot_localization", "torch", "torchvision",
    "mediapipe", "opencv2", "pygame"
}

missing_forbidden = required_forbidden - forbidden_declared
if missing_forbidden:
    errors.append(f"Missing required perception exclusions in lockfile: {sorted(missing_forbidden)}")

for fn in forbidden_declared:
    if fn in pkg_names:
        errors.append(f"CRITICAL: Forbidden perception package '{fn}' found in locked packages!")

# 6. Verify direct declared dependencies across ALL tags in src/*/package.xml
pkg_xml_deps = set()
dep_tags = {"depend", "build_depend", "buildtool_depend", "build_export_depend", "buildtool_export_depend", "exec_depend", "test_depend"}
for root, _, files in os.walk(src_dir):
    if "package.xml" in files:
        xml_p = os.path.join(root, "package.xml")
        try:
            tree = ET.parse(xml_p)
            for el in tree.getroot():
                if el.tag in dep_tags and el.text:
                    pkg_xml_deps.add(el.text.strip())
        except Exception as e:
            errors.append(f"Failed to parse {xml_p}: {e}")

DEP_TO_APT_MAP = {
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

internal_workspace_pkgs = {"ros_robot_controller", "ros_robot_controller_msgs", "controller", "ubuntu_tank_safety", "ubuntu_tank_supervisor", "ubuntu_tank_teleop"}

for dep in sorted(pkg_xml_deps):
    if dep in internal_workspace_pkgs:
        continue
    expected_apt = DEP_TO_APT_MAP.get(dep, dep)
    if expected_apt not in pkg_names:
        errors.append(f"Declared dependency '{dep}' (apt package: '{expected_apt}') is missing from versions.lock")

if errors:
    print(f"FAIL: versions.lock validation failed with {len(errors)} error(s):", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)

rosdep_rev = rosdep_sources.get('revision') or lock.get('rosdep_index', {}).get('revision')
print(f"PASS: versions.lock verified successfully.")
print(f"  Target: {meta.get('target_os')} ({meta.get('target_os_codename')}) {meta.get('target_arch')} - ROS 2 {meta.get('ros_distro')}")
print(f"  Transitive closure status: {closure_status}")
print(f"  Locked Packages: {len(pkg_names)} packages pinned with SHA256 checksums")
print(f"  Pinned ros2-apt-source: {apt_source.get('url')} (v{apt_source.get('version')})")
print(f"  Pinned rosdep sources revision: {rosdep_rev}")
PYVERIFY
}

# Verify candidate packages / solver plan against versions.lock
cmd_verify_closure() {
  local candidate_manifest=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help|help)
        cat <<'CLOSUREHELP'
Usage: ./scripts/install_ros2.sh verify-closure --candidates <manifest_file>

Verifies that target solver candidate packages match versions.lock:
  - Requires an explicit target solver candidate manifest
  - Enforces exact package-set equality between candidates and versions.lock
  - Rejects missing locked packages and unlocked/unrecorded transitive packages
  - Compares exact name, version, architecture, and repository equality
  - Cryptographically verifies SHA-256 checksum for every candidate artifact
CLOSUREHELP
        return 0
        ;;
      --candidates)
        shift
        candidate_manifest="${1:-}"
        shift || true
        ;;
      *)
        echo "Unknown option for verify-closure: $1" >&2
        return 1
        ;;
    esac
  done

  echo "============================================================"
  echo "Verifying Exact APT Solver Closure and Checksums"
  echo "============================================================"

  if [ -z "${candidate_manifest}" ]; then
    echo "FAIL: verify-closure requires a candidate solver manifest via --candidates <manifest_file>" >&2
    return 1
  fi

  if [ ! -f "${candidate_manifest}" ]; then
    echo "FAIL: Candidate manifest file not found: ${candidate_manifest}" >&2
    return 1
  fi

  if [ ! -f "${LOCK_FILE}" ]; then
    echo "FAIL: versions.lock not found at: ${LOCK_FILE}" >&2
    return 1
  fi

  python3 - "${LOCK_FILE}" "${candidate_manifest}" <<'PYCLOSURE'
import sys, re, os

lock_path = sys.argv[1]
candidate_path = sys.argv[2]

def parse_lock(filepath):
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass

    data = {"packages": []}
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
            elif re.match(r"^[a-z_]+:$", line_str) and not line.startswith(" ") and not line.startswith("\t"):
                current_section = line_str[:-1]
                continue
            if current_section == "packages":
                m = re.match(r"^-\s+name:\s*(.*)$", line_str)
                if m:
                    current_item = {"name": m.group(1).strip().strip("'\"")}
                    data["packages"].append(current_item)
                elif current_item is not None:
                    kv = re.match(r"^([a-z_]+):\s*(.*)$", line_str)
                    if kv:
                        current_item[kv.group(1)] = kv.group(2).strip().strip("'\"")
    return data

lock = parse_lock(lock_path)
locked_pkgs = {p["name"]: p for p in lock.get("packages", [])}

if not os.path.isfile(candidate_path):
    print(f"FAIL: Candidate manifest not found: {candidate_path}", file=sys.stderr)
    sys.exit(1)

candidates = {}
errors = []

with open(candidate_path, "r", encoding="utf-8") as f:
    for line_num, line in enumerate(f, 1):
        line_str = line.strip()
        if not line_str or line_str.startswith("#"):
            continue

        parts = line_str.split()
        if len(parts) == 5:
            c_name, c_ver, c_arch, c_repo, c_sha = parts
            candidates[c_name] = {
                "name": c_name,
                "version": c_ver,
                "architecture": c_arch,
                "repository": c_repo,
                "sha256": c_sha,
            }
        elif len(parts) >= 2:
            c_name = parts[0].strip(":,=")
            c_sha = ""
            c_ver = ""
            c_arch = ""
            c_repo = ""
            for p in parts[1:]:
                if re.match(r"^[0-9a-f]{64}$", p):
                    c_sha = p
                elif p in ("arm64", "all", "amd64"):
                    c_arch = p
                elif p in ("ros2", "ubuntu-resolute", "ubuntu", "main", "universe"):
                    c_repo = p
                elif re.match(r"^[0-9]", p):
                    c_ver = p
            candidates[c_name] = {
                "name": c_name,
                "version": c_ver,
                "architecture": c_arch,
                "repository": c_repo,
                "sha256": c_sha,
            }
        else:
            errors.append(f"Line {line_num}: Unparseable candidate line: '{line_str}'")

# 1. Exact package-set equality check
locked_set = set(locked_pkgs.keys())
cand_set = set(candidates.keys())

missing_locked = locked_set - cand_set
if missing_locked:
    errors.append(f"Candidate manifest is missing required locked package(s): {sorted(missing_locked)}")

unlocked_transitive = cand_set - locked_set
if unlocked_transitive:
    errors.append(f"Unlocked transitive package(s) found in candidate closure: {sorted(unlocked_transitive)}. All packages must be explicitly locked in versions.lock.")

# 2. Detailed field comparisons for each package
for name in sorted(cand_set & locked_set):
    cand = candidates[name]
    locked = locked_pkgs[name]

    c_ver = cand.get("version", "")
    l_ver = locked.get("version", "")
    if not c_ver:
        errors.append(f"Package '{name}' is missing version in candidate manifest")
    elif c_ver != l_ver:
        errors.append(f"Version mismatch for package '{name}': expected '{l_ver}', candidate has '{c_ver}'")

    c_arch = cand.get("architecture", "")
    l_arch = locked.get("architecture", "")
    if not c_arch:
        errors.append(f"Package '{name}' is missing architecture in candidate manifest")
    elif c_arch != l_arch:
        errors.append(f"Architecture mismatch for package '{name}': expected '{l_arch}', candidate has '{c_arch}'")

    c_repo = cand.get("repository", "")
    l_repo = locked.get("repository", "")
    if not c_repo:
        errors.append(f"Package '{name}' is missing repository in candidate manifest")
    elif c_repo != l_repo:
        errors.append(f"Repository mismatch for package '{name}': expected '{l_repo}', candidate has '{c_repo}'")

    c_sha = cand.get("sha256", "")
    l_sha = locked.get("sha256", "")
    if not c_sha or not re.match(r"^[0-9a-f]{64}$", c_sha):
        errors.append(f"Package '{name}' is missing valid 64-character SHA-256 in candidate manifest: '{c_sha}'")
    elif c_sha != l_sha:
        errors.append(f"Checksum mismatch for package '{name}': expected '{l_sha}', candidate has '{c_sha}'")

if errors:
    print(f"FAIL: Candidate closure verification failed with {len(errors)} error(s):", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(1)

print(f"PASS: Exact solver closure verified: all {len(candidates)} packages match locked versions, architectures, repositories, and SHA-256 hashes.")
PYCLOSURE
}

## Prepare host environment
cmd_prepare_host() {
  local orig_args=("$@")
  local dry_run=false
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help|help)
        cat <<'PREPHELP'
Usage: ./deploy.sh prepare-host [--dry-run]

Prepares a clean Ubuntu 26.04 Raspberry Pi 5:
  1. Acquires the root-owned deployment lock (/run/lock/ubuntu_tank/deploy.lock)
  2. Runs strict preflight checks
  3. Records pre-upgrade host baseline snapshot
  4. Upgrades system packages from official Ubuntu repositories
  5. Configures UTF-8 locale and universe repository
  6. Records post-upgrade accepted host baseline snapshot
  7. Checks if a kernel/system reboot is required
PREPHELP
        return 0
        ;;
      --dry-run)
        dry_run=true
        shift
        ;;
      *)
        echo "Unknown option for prepare-host: $1" >&2
        return 1
        ;;
    esac
  done

  # Safety Invariant: Mock environment can NEVER authorize live host mutation
  assert_no_mutation_overrides "${dry_run}" "prepare-host"

  # Root / Sudo check and privilege elevation up-front for live mutations
  if [ "${dry_run}" = "false" ] && [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "FAIL: prepare-host requires root or sudo access." >&2
      return 1
    fi
    exec sudo "$0" "prepare-host" "${orig_args[@]}"
  fi

  # Acquire deployment lock
  acquire_deployment_lock "${dry_run}"

  echo "============================================================"
  echo "MentorPi Native Tank Controller - Prepare Host"
  echo "============================================================"

  # Full preflight check (strict)
  if [ "${dry_run}" = "false" ]; then
    if ! check_host "true" "false" "false"; then
      echo "FAIL: Strict host preflight check failed. Cannot prepare host." >&2
      return 1
    fi
  else
    echo "[DRY-RUN] Preflight validation simulated."
  fi

  if [ "${dry_run}" = "true" ]; then
    echo "[DRY-RUN] Would record pre-upgrade host baseline in /var/opt/ubuntu_tank/deployment/host-baseline-pre.txt."
    echo "[DRY-RUN] Would run: sudo dpkg --configure -a"
    echo "[DRY-RUN] Would run: sudo apt-get --fix-broken install -y"
    echo "[DRY-RUN] Would run: sudo apt-get update"
    echo "[DRY-RUN] Would run: sudo apt-get upgrade -y"
    echo "[DRY-RUN] Would run: sudo apt-get install -y locales software-properties-common curl"
    echo "[DRY-RUN] Would run: sudo locale-gen en_US en_US.UTF-8"
    echo "[DRY-RUN] Would run: sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8"
    echo "[DRY-RUN] Would run: sudo add-apt-repository -y universe"
    echo "[DRY-RUN] Would record post-upgrade host baseline in /var/opt/ubuntu_tank/deployment/host-baseline-post.txt."
    local reboot_check_file
    reboot_check_file="$(resolve_reboot_check_file)"
    if [ -f "${reboot_check_file}" ]; then
      echo "[DRY-RUN] Host reboot required (${reboot_check_file} exists)."
      return 2
    fi
    echo "[DRY-RUN] Would check ${reboot_check_file}."
    echo "PASS: Dry-run prepare-host completed successfully."
    return 0
  fi

  local baseline_dir="/var/opt/ubuntu_tank/deployment"
  mkdir -p "${baseline_dir}"
  chmod 0755 "${baseline_dir}"

  local pre_baseline="${baseline_dir}/host-baseline-pre.txt"
  local post_baseline="${baseline_dir}/host-baseline-post.txt"
  local accepted_baseline="${baseline_dir}/host-baseline.txt"

  echo "--> Recording pre-upgrade host baseline..."
  record_host_baseline "${pre_baseline}" "MentorPi Host Baseline - Pre-Upgrade Snapshot" "false"
  echo "Pre-upgrade baseline recorded at: ${pre_baseline}"

  echo "--> Repairing partial package state if present..."
  recover_apt_state

  echo "--> Updating base Ubuntu repositories..."
  apt-get update

  echo "--> Upgrading base Ubuntu system packages..."
  DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

  echo "--> Installing base system prerequisites..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y locales software-properties-common curl

  echo "--> Configuring system locale (en_US.UTF-8)..."
  locale-gen en_US en_US.UTF-8
  update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8

  echo "--> Enabling Ubuntu universe repository..."
  add-apt-repository -y universe

  local reboot_check_file
  reboot_check_file="$(resolve_reboot_check_file)"
  if [ -f "${reboot_check_file}" ]; then
    echo "--> Recording post-upgrade host baseline (pending reboot)..."
    record_host_baseline "${post_baseline}" "MentorPi Host Baseline - Post-Upgrade Snapshot (Reboot Required)" "true"
    echo "Post-upgrade baseline recorded at: ${post_baseline}"
    echo "============================================================"
    echo "ATTENTION: Host kernel or base libraries upgraded."
    echo "${reboot_check_file} exists."
    echo "System reboot is required before proceeding with ROS installation."
    echo "Please reboot: 'sudo reboot'"
    echo "After reboot, rerun './deploy.sh prepare-host' to accept the upgraded kernel baseline."
    echo "============================================================"
    return 2
  fi

  echo "--> Recording post-upgrade accepted host baseline..."
  record_host_baseline "${post_baseline}" "MentorPi Host Baseline - Accepted Ubuntu Baseline Snapshot" "false"
  cp -f "${post_baseline}" "${accepted_baseline}"
  chmod 0644 "${accepted_baseline}"
  chown root:root "${accepted_baseline}" 2>/dev/null || true
  echo "Accepted host baseline recorded at: ${accepted_baseline}"

  echo "PASS: Host preparation completed successfully."
  return 0
}

# Helper: Extract solver candidate manifest from downloaded deb archives
generate_candidate_manifest_from_archives() {
  local lock_file="$1"
  local output_manifest="$2"
  local archives_dir="${3:-/var/cache/apt/archives}"

  python3 - "${lock_file}" "${output_manifest}" "${archives_dir}" <<'PYARCHIVES'
import sys, os, subprocess, re, hashlib

lock_file = sys.argv[1]
output_manifest = sys.argv[2]
archives_dir = sys.argv[3]

def parse_lock(filepath):
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass
    pkgs = []
    current_section = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            raw_line = line.rstrip()
            line_str = raw_line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if line_str == "packages:":
                current_section = "packages"
                continue
            elif re.match(r"^[a-z_]+:$", line_str) and not line.startswith(" ") and not line.startswith("\t"):
                current_section = line_str[:-1]
                continue
            if current_section == "packages":
                if line_str.startswith("- name:"):
                    pkgs.append({"name": line_str.split(":", 1)[1].strip().strip("'\"")})
                elif pkgs and ":" in line_str:
                    k, v = line_str.split(":", 1)
                    pkgs[-1][k.strip()] = v.strip().strip("'\"")
    return {"packages": pkgs}

data = parse_lock(lock_file)
locked_map = {p["name"]: p for p in data.get("packages", [])}

def determine_repo(pkg_name, pkg_version):
    # Query apt metadata to independently determine the repository class. Never
    # infer origin from a package name or copy it from versions.lock.
    try:
        res = subprocess.run(
            ["apt-cache", "madison", pkg_name],
            capture_output=True, text=True, check=True
        )
        for line in res.stdout.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                m_ver = parts[1]
                m_origin = parts[2].lower()
                if not pkg_version or pkg_version == m_ver:
                    if any(s in m_origin for s in ("packages.ros.org", "repo.ros2.org", "ros2")):
                        return "ros2"
                    if any(s in m_origin for s in ("ubuntu.com", "ports.ubuntu", "archive.ubuntu")):
                        return "ubuntu-resolute"
    except Exception:
        pass

    # Query apt-cache policy as a secondary origin lookup.
    try:
        res = subprocess.run(
            ["apt-cache", "policy", pkg_name],
            capture_output=True, text=True, check=True
        )
        in_ver = False
        for line in res.stdout.splitlines():
            if pkg_version and pkg_version in line:
                in_ver = True
                continue
            if in_ver and ("500" in line or "100" in line):
                line_lower = line.lower()
                if any(s in line_lower for s in ("packages.ros.org", "repo.ros2.org", "ros2")):
                    return "ros2"
                if any(s in line_lower for s in ("ubuntu.com", "ports.ubuntu", "archive.ubuntu")):
                    return "ubuntu-resolute"
                break
    except Exception:
        pass

    return "unknown"

deb_by_name = {}
if os.path.exists(archives_dir):
    for f in sorted(os.listdir(archives_dir)):
        if not f.endswith(".deb"):
            continue
        deb_path = os.path.join(archives_dir, f)
        try:
            res = subprocess.run(
                ["dpkg-deb", "-f", deb_path, "Package", "Version", "Architecture"],
                capture_output=True, text=True, check=True
            )
            meta = {}
            unlabeled = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
                else:
                    unlabeled.append(line)
            p_name = meta.get("package", "")
            p_ver = meta.get("version", "")
            p_arch = meta.get("architecture", "")
            if not p_name and len(unlabeled) > 0:
                p_name = unlabeled[0]
            if not p_ver and len(unlabeled) > 1:
                p_ver = unlabeled[1]
            if not p_arch and len(unlabeled) > 2:
                p_arch = unlabeled[2]
        except Exception as exc:
            print(f"FAIL: Cannot inspect downloaded artifact '{deb_path}': {exc}", file=sys.stderr)
            sys.exit(1)
        if not p_name or not p_ver or not p_arch:
            print(f"FAIL: Downloaded artifact '{deb_path}' has incomplete package metadata", file=sys.stderr)
            sys.exit(1)
        if p_name in deb_by_name:
            previous_path = deb_by_name[p_name][0]
            print(
                f"FAIL: Duplicate downloaded package '{p_name}' in '{previous_path}' and '{deb_path}'",
                file=sys.stderr,
            )
            sys.exit(1)
        deb_by_name[p_name] = (deb_path, p_name, p_ver, p_arch)

all_pkg_names = set(deb_by_name.keys()) | set(locked_map.keys())

with open(output_manifest, "w", encoding="utf-8") as out:
    for name in sorted(all_pkg_names):
        if name in deb_by_name:
            deb_path, n, v, a = deb_by_name[name]
            with open(deb_path, "rb") as df:
                sha = hashlib.sha256(df.read()).hexdigest()
            repo = determine_repo(n, v)
            out.write(f"{n} {v} {a} {repo} {sha}\n")
        else:
            out.write(f"{name} MISSING-FROM-ARCHIVES none none none\n")
PYARCHIVES
}

# Install ROS and locked dependencies
cmd_install_ros() {
  local orig_args=("$@")
  local dry_run=false
  local candidate_manifest=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help|help)
        cat <<'ROSHELP'
Usage: ./deploy.sh install-ros [--dry-run] [--candidates <file>]

Installs ROS 2 Lyrical and locked dependencies:
  1. Validates test/mock override isolation (fails closed on test overrides for live operations)
  2. Elevates privileges up-front before acquiring deployment lock
  3. Acquires root-owned deployment lock (/run/lock/ubuntu_tank/deploy.lock)
  4. Runs strict preflight checks and verifies absence of /run/reboot-required
  5. Validates accepted host baseline snapshot (/var/opt/ubuntu_tank/deployment/host-baseline.txt)
  6. Validates versions.lock integrity
  7. Downloads and cryptographically verifies ros2-apt-source deb package
  8. Installs ros2-apt-source and aborts without package recovery on failure
  9. Enforces exact APT solver closure and downloaded .deb artifact SHA-256 verification
 10. Installs exact locked ROS 2 Lyrical packages, SROS2, and EEPROM tools

`--candidates` is a dry-run test input only. Live installation always derives
the candidate manifest from the isolated set of .deb artifacts downloaded by
the current apt transaction.
ROSHELP
        return 0
        ;;
      --dry-run)
        dry_run=true
        shift
        ;;
      --candidates)
        if [ $# -lt 2 ]; then
          echo "Error: --candidates requires a file argument" >&2
          return 1
        fi
        candidate_manifest="$2"
        shift 2
        ;;
      *)
        echo "Unknown option for install-ros: $1" >&2
        return 1
        ;;
    esac
  done

  # Safety Invariant: Mock environment cannot authorize live mutation
  assert_no_mutation_overrides "${dry_run}" "install-ros"

  if [ "${dry_run}" = "false" ] && [ -n "${candidate_manifest}" ]; then
    echo "FAIL: --candidates is test-only and cannot replace live artifact inspection." >&2
    return 1
  fi
  if [ "${dry_run}" = "false" ]; then
    require_complete_package_lock
  fi

  # Root / Sudo check and privilege elevation up-front for live mutations
  if [ "${dry_run}" = "false" ] && [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "FAIL: install-ros requires root or sudo access." >&2
      return 1
    fi
    exec sudo "$0" "install-ros" "${orig_args[@]}"
  fi

  # 1. Acquire deployment lock
  acquire_deployment_lock "${dry_run}"

  echo "============================================================"
  echo "MentorPi Native Tank Controller - Install ROS 2 Lyrical"
  echo "============================================================"

  # 2. Strict full host preflight
  if [ "${dry_run}" = "false" ]; then
    if ! check_host "true" "false" "false"; then
      echo "FAIL: Preflight checks failed prior to ROS installation." >&2
      return 1
    fi
  else
    echo "[DRY-RUN] Preflight validation simulated."
  fi

  # 3. Check pending reboot
  local reboot_check_file
  reboot_check_file="$(resolve_reboot_check_file)"
  if [ -f "${reboot_check_file}" ]; then
    echo "FAIL: Host reboot required before installing ROS (${reboot_check_file} exists)." >&2
    return 2
  fi

  # 4. Accepted baseline verification
  local baseline_file="/var/opt/ubuntu_tank/deployment/host-baseline.txt"
  if [ "${dry_run}" = "false" ]; then
    if [ ! -f "${baseline_file}" ]; then
      echo "FAIL: Accepted host baseline not found at ${baseline_file}." >&2
      echo "Run './deploy.sh prepare-host' first to establish the baseline." >&2
      return 1
    fi
    if ! validate_host_baseline "${baseline_file}"; then
      echo "FAIL: Accepted host baseline validation failed." >&2
      return 1
    fi
    local recorded_kernel
    recorded_kernel="$(grep -E "^kernel: " "${baseline_file}" | awk '{print $2}' || echo "")"
    local running_kernel
    running_kernel="$(uname -r)"
    if [ -n "${recorded_kernel}" ] && [ "${recorded_kernel}" != "${running_kernel}" ]; then
      echo "FAIL: Running kernel (${running_kernel}) differs from recorded baseline kernel (${recorded_kernel})." >&2
      echo "A host reboot is required to activate the upgraded kernel." >&2
      return 2
    fi
  fi

  # 5. Verify lockfile
  if ! cmd_verify_lock; then
    echo "FAIL: Lockfile verification failed. Aborting installation." >&2
    return 1
  fi

  # 6. Parse lock details
  local lock_data
  lock_data="$(python3 - "${LOCK_FILE}" <<'PYLOCK'
import sys, re

def parse_lock(filepath):
    try:
        import yaml
        with open(filepath) as f:
            d = yaml.safe_load(f)
            apt_s = d['ros_apt_source']
            pkgs = ' '.join([f"{p['name']}={p['version']}" for p in d['packages']])
            return f"{apt_s['url']}|{apt_s['sha256']}|{pkgs}"
    except ImportError:
        pass

    url = ""
    sha = ""
    pkgs = []
    current_section = None
    with open(filepath) as f:
        for line in f:
            line_str = line.strip()
            if line_str == "ros_apt_source:":
                current_section = "ros_apt_source"
            elif line_str == "packages:":
                current_section = "packages"
            elif current_section == "ros_apt_source":
                if line_str.startswith("url:"):
                    url = line_str.split(":", 1)[1].strip().strip("'\"")
                elif line_str.startswith("sha256:"):
                    sha = line_str.split(":", 1)[1].strip().strip("'\"")
            elif current_section == "packages":
                if line_str.startswith("- name:"):
                    n = line_str.split(":", 1)[1].strip().strip("'\"")
                elif line_str.startswith("version:"):
                    v = line_str.split(":", 1)[1].strip().strip("'\"")
                    pkgs.append(f"{n}={v}")
    return f"{url}|{sha}|{' '.join(pkgs)}"

print(parse_lock(sys.argv[1]))
PYLOCK
)"

  local pinned_url
  local pinned_sha
  local pkg_args
  pinned_url="$(echo "${lock_data}" | cut -d'|' -f1)"
  pinned_sha="$(echo "${lock_data}" | cut -d'|' -f2)"
  pkg_args="$(echo "${lock_data}" | cut -d'|' -f3)"

  if [ "${dry_run}" = "true" ]; then
    echo "[DRY-RUN] Verified lock parameters:"
    echo "  ROS APT Source URL:    ${pinned_url}"
    echo "  ROS APT Source SHA256: ${pinned_sha}"
    echo "  Locked packages:       ${pkg_args}"
    if [ -n "${candidate_manifest}" ]; then
      echo "--> Verifying solver closure against provided candidate manifest..."
      cmd_verify_closure --candidates "${candidate_manifest}"
    fi
    echo "[DRY-RUN] Would download ${pinned_url} and verify SHA256"
    echo "[DRY-RUN] Would install ros2-apt-source deb package"
    echo "[DRY-RUN] Would execute apt-get update"
    echo "[DRY-RUN] Would execute apt-get install --download-only to fetch candidate .deb artifacts"
    echo "[DRY-RUN] Would execute verify-closure asserting exact solver package-set equality, versions, architectures, repositories, and SHA-256 hashes against versions.lock"
    echo "[DRY-RUN] Would execute apt-get install --no-download for all locked packages"
    echo "PASS: Dry-run install-ros completed successfully."
    return 0
  fi

  local tmp_deb="/tmp/ros2-apt-source.deb"
  echo "--> Downloading pinned ros2-apt-source package..."
  if ! curl --fail --location --retry 3 --retry-delay 2 --output "${tmp_deb}" "${pinned_url}"; then
    echo "FAIL: Failed to download ros2-apt-source package from ${pinned_url}" >&2
    rm -f "${tmp_deb}"
    return 1
  fi

  echo "--> Verifying cryptographic checksum..."
  if ! printf '%s  %s\n' "${pinned_sha}" "${tmp_deb}" | sha256sum --check --strict; then
    echo "FAIL: Checksum mismatch for ros2-apt-source package!" >&2
    rm -f "${tmp_deb}"
    return 1
  fi

  echo "--> Installing ros2-apt-source repository package..."
  if ! dpkg -i "${tmp_deb}"; then
    echo "FAIL: ros2-apt-source installation failed; refusing network-enabled package recovery." >&2
    echo "Verified source package is preserved at ${tmp_deb}. Restore the accepted host baseline before retrying." >&2
    return 1
  fi
  rm -f "${tmp_deb}"

  echo "--> Updating package index from official ROS repository..."
  apt-get update

  local apt_archives_dir
  apt_archives_dir="$(mktemp -d /var/cache/apt/ubuntu_tank_archives.XXXXXX)"
  mkdir -p "${apt_archives_dir}/partial"
  chmod 0755 "${apt_archives_dir}" "${apt_archives_dir}/partial"
  local apt_cache_args=(-o "Dir::Cache::archives=${apt_archives_dir}")

  echo "--> Downloading candidate .deb packages into an isolated transaction cache..."
  # shellcheck disable=SC2086
  if ! DEBIAN_FRONTEND=noninteractive apt-get "${apt_cache_args[@]}" install -y --download-only --reinstall --no-install-recommends ${pkg_args}; then
    echo "FAIL: Isolated apt download failed; refusing network-enabled package recovery." >&2
    echo "Downloaded artifacts are preserved for inspection at ${apt_archives_dir}." >&2
    return 1
  fi

  local closure_manifest
  closure_manifest="$(mktemp /tmp/ubuntu_tank_closure.XXXXXX.txt)"
  generate_candidate_manifest_from_archives "${LOCK_FILE}" "${closure_manifest}" "${apt_archives_dir}"

  echo "--> Verifying exact solver closure and downloaded artifact hashes..."
  if ! cmd_verify_closure --candidates "${closure_manifest}"; then
    echo "FAIL: Solver closure verification failed. Aborting installation." >&2
    echo "Candidate manifest is preserved at ${closure_manifest}." >&2
    echo "Downloaded artifacts are preserved at ${apt_archives_dir}." >&2
    return 1
  fi
  rm -f "${closure_manifest}"

  echo "--> Installing verified locked ROS 2 Lyrical packages and build tools..."
  # shellcheck disable=SC2086
  if ! DEBIAN_FRONTEND=noninteractive apt-get "${apt_cache_args[@]}" install -y --no-download --no-install-recommends ${pkg_args}; then
    echo "FAIL: Verified no-download installation failed; refusing network-enabled package recovery." >&2
    echo "Downloaded artifacts are preserved for inspection at ${apt_archives_dir}." >&2
    return 1
  fi
  rm -rf "${apt_archives_dir}"

  echo "PASS: ROS 2 Lyrical and locked dependencies installed successfully."
  return 0
}

# Resolve and verify rosdep dependencies
cmd_install_deps() {
  local orig_args=("$@")
  local dry_run=false
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help|help)
        cat <<'DEPSHELP'
Usage: ./deploy.sh install-deps [--dry-run]

Resolves and verifies locked rosdep dependencies:
  1. Validates test/mock override isolation
  2. Elevates privileges up-front before acquiring deployment lock
  3. Acquires root-owned deployment lock (/run/lock/ubuntu_tank/deploy.lock)
  4. Runs strict preflight checks and verifies absence of /run/reboot-required
  5. Validates accepted host baseline snapshot
  6. Validates versions.lock integrity
  7. Downloads and cryptographically verifies pinned rosdep sources snapshot (index_v4, base, python, ruby)
  8. Configures rosdep to use verified local file snapshot sources
  9. Resolves workspace dependencies with authentic rosdep resolve and asserts 100% presence in versions.lock
 10. Requires every resolved package to already be installed at its locked version

Package installation belongs exclusively to `install-ros`, whose isolated apt
transaction is checked against the complete artifact lock before installation.
DEPSHELP
        return 0
        ;;
      --dry-run)
        dry_run=true
        shift
        ;;
      *)
        echo "Unknown option for install-deps: $1" >&2
        return 1
        ;;
    esac
  done

  # Safety Invariant: Mock environment cannot authorize live mutation
  assert_no_mutation_overrides "${dry_run}" "install-deps"
  if [ "${dry_run}" = "false" ]; then
    require_complete_package_lock
  fi

  # Root / Sudo check and privilege elevation up-front for live mutations
  if [ "${dry_run}" = "false" ] && [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "FAIL: install-deps requires root or sudo access." >&2
      return 1
    fi
    exec sudo "$0" "install-deps" "${orig_args[@]}"
  fi

  acquire_deployment_lock "${dry_run}"

  echo "============================================================"
  echo "MentorPi Native Tank Controller - Install Dependencies"
  echo "============================================================"

  if [ "${dry_run}" = "false" ]; then
    if ! check_host "true" "false" "false"; then
      echo "FAIL: Preflight checks failed." >&2
      return 1
    fi
  else
    echo "[DRY-RUN] Preflight validation simulated."
  fi

  # Check pending reboot
  local reboot_check_file
  reboot_check_file="$(resolve_reboot_check_file)"
  if [ -f "${reboot_check_file}" ]; then
    echo "FAIL: Host reboot required before installing dependencies (${reboot_check_file} exists)." >&2
    return 2
  fi

  # Validate accepted host baseline
  local baseline_file="/var/opt/ubuntu_tank/deployment/host-baseline.txt"
  if [ "${dry_run}" = "false" ]; then
    if [ ! -f "${baseline_file}" ]; then
      echo "FAIL: Accepted host baseline not found at ${baseline_file}." >&2
      return 1
    fi
    if ! validate_host_baseline "${baseline_file}"; then
      echo "FAIL: Accepted host baseline validation failed." >&2
      return 1
    fi
  fi

  if ! cmd_verify_lock; then
    echo "FAIL: Lockfile verification failed." >&2
    return 1
  fi

  # Parse rosdep sources metadata from versions.lock
  local rosdep_json
  rosdep_json="$(python3 - "${LOCK_FILE}" <<'PYROSDEP'
import sys, json, re

def parse_rosdep(filepath):
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            d = yaml.safe_load(f)
            return json.dumps(d.get('rosdep_sources', {}))
    except ImportError:
        pass

    # pure python parser for rosdep_sources
    sources = {}
    current_section = None
    current_source = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            raw_line = line.rstrip()
            line_str = raw_line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if line_str == "rosdep_sources:":
                current_section = "rosdep_sources"
                continue
            elif re.match(r"^[a-z_]+:$", line_str) and not line.startswith(" ") and not line.startswith("\t"):
                current_section = line_str[:-1]
                continue
            if current_section == "rosdep_sources":
                if line_str.startswith("revision:"):
                    sources["revision"] = line_str.split(":", 1)[1].strip().strip("'\"")
                elif line.startswith("  ") and not line.startswith("    ") and line_str.endswith(":"):
                    current_source = line_str[:-1]
                    sources[current_source] = {}
                elif current_source and line.startswith("    ") and ":" in line_str:
                    k, v = line_str.split(":", 1)
                    sources[current_source][k.strip()] = v.strip().strip("'\"")
    return json.dumps(sources)

print(parse_rosdep(sys.argv[1]))
PYROSDEP
)"

  if [ "${dry_run}" = "true" ]; then
    echo "[DRY-RUN] Verified rosdep snapshot sources:"
    echo "  ${rosdep_json}"
    echo "[DRY-RUN] Would fetch rosdep snapshot sources (index_v4, base, python, ruby) and verify SHA256 hashes"
    echo "[DRY-RUN] Would configure /etc/ros/rosdep/sources.list.d/10-ubuntu-tank.list to use verified snapshot files"
    echo "[DRY-RUN] Would execute: rosdep update with ROSDISTRO_INDEX_URL=file://..."
    echo "[DRY-RUN] Would resolve workspace dependencies using authentic rosdep resolve"
    echo "[DRY-RUN] Would assert 100% of resolved apt dependencies exist in versions.lock with zero unpinned or forbidden packages"
    echo "[DRY-RUN] Would verify resolved packages are already installed at locked versions"
    echo "PASS: Dry-run install-deps completed successfully."
    return 0
  fi

  if ! command -v rosdep >/dev/null 2>&1; then
    echo "FAIL: rosdep command not found. Run './deploy.sh install-ros' first." >&2
    return 1
  fi

  local rosdep_dir="/var/opt/ubuntu_tank/rosdep_sources"
  mkdir -p "${rosdep_dir}"
  chmod 0755 "${rosdep_dir}"

  # Download and verify each of the 4 snapshot sources
  python3 - "${rosdep_json}" "${rosdep_dir}" <<'PYDOWNLOAD'
import sys, json, urllib.request, hashlib, os

sources = json.loads(sys.argv[1])
out_dir = sys.argv[2]

file_map = {
    "index_v4": "index-v4.yaml",
    "base": "base.yaml",
    "python": "python.yaml",
    "ruby": "ruby.yaml"
}

for key, filename in file_map.items():
    entry = sources.get(key)
    if not entry:
        print(f"FAIL: Missing rosdep source configuration for '{key}'", file=sys.stderr)
        sys.exit(1)
    url = entry["url"]
    expected_sha = entry["sha256"]
    dest = os.path.join(out_dir, filename)

    print(f"--> Downloading rosdep source '{key}' from {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": "MentorPi-Install/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read()
    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != expected_sha:
        print(f"FAIL: SHA256 mismatch for '{key}': expected {expected_sha}, got {actual_sha}", file=sys.stderr)
        sys.exit(1)
    with open(dest, "wb") as f:
        f.write(content)
    os.chmod(dest, 0o644)
    print(f"--> Verified {key} -> {dest} ({actual_sha[:16]}...)")
PYDOWNLOAD

  # Configure rosdep to use strictly local snapshot files
  mkdir -p /etc/ros/rosdep/sources.list.d
  local local_sources_list="/etc/ros/rosdep/sources.list.d/10-ubuntu-tank.list"
  cat > "${local_sources_list}" <<EOF
# Pinned local snapshot sources for MentorPi native controller
yaml file://${rosdep_dir}/base.yaml
yaml file://${rosdep_dir}/python.yaml
yaml file://${rosdep_dir}/ruby.yaml
EOF
  chmod 0644 "${local_sources_list}"
  # Remove default unpinned list if present
  rm -f /etc/ros/rosdep/sources.list.d/20-default.list

  echo "--> Updating rosdep using verified snapshot..."
  local index_url
  index_url="$(python3 - "${LOCK_FILE}" <<'PYINDEX'
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        d = yaml.safe_load(f)
    print(d.get('rosdep_sources', {}).get('index_v4', {}).get('url', ''))
except Exception:
    pass
PYINDEX
)"
  export ROSDISTRO_INDEX_URL="${index_url:-https://raw.githubusercontent.com/ros/rosdistro/a9f673b32f2469b5b3655f62d53f176ef69b233a/index-v4.yaml}"
  rosdep update --rosdistro lyrical

  echo "--> Resolving workspace package dependencies..."
  cd "${TANK_DIR}"
  local resolved_keys
  resolved_keys="$(rosdep keys --from-paths src --ignore-src --rosdistro lyrical)"

  echo "--> Performing authentic rosdep resolution on required keys..."
  local resolved_pairs=()
  for k in ${resolved_keys}; do
    local res_out
    res_out="$(rosdep resolve "${k}" --rosdistro lyrical | grep -v '^#' | tr '\n' ' ' | xargs)"
    if [ -z "${res_out}" ]; then
      echo "FAIL: rosdep resolve failed for key '${k}'" >&2
      return 1
    fi
    for apt_name in ${res_out}; do
      resolved_pairs+=("${k}:${apt_name}")
    done
  done

  echo "--> Cross-checking resolved apt packages against versions.lock..."
  local install_candidates
  install_candidates="$(python3 - "${LOCK_FILE}" "${resolved_pairs[*]}" <<'PYRESOLVE'
import sys, re

lock_path = sys.argv[1]
pairs_str = sys.argv[2]
pairs = pairs_str.split() if pairs_str.strip() else []

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
            elif re.match(r"^[a-z_]+:$", line_str) and not line.startswith(" ") and not line.startswith("\t"):
                current_section = line_str[:-1]
                continue
            if current_section in ("packages", "excluded_perception"):
                m = re.match(r"^-\s+name:\s*(.*)$", line_str)
                if m:
                    current_item = {"name": m.group(1).strip().strip("'\"")}
                    data[current_section].append(current_item)
                elif current_item is not None:
                    kv = re.match(r"^([a-z_]+):\s*(.*)$", line_str)
                    if kv:
                        current_item[kv.group(1)] = kv.group(2).strip().strip("'\"")
    return data

d = parse_lock(lock_path)
locked_map = {p['name']: p.get('version', '') for p in d.get('packages', [])}
forbidden_names = {p['name'] for p in d.get('excluded_perception', [])}

errors = []
pkgs_to_install = {}

for pair in pairs:
    if ":" not in pair:
        continue
    rosdep_key, apt_pkg = pair.split(":", 1)

    if rosdep_key in forbidden_names or apt_pkg in forbidden_names:
        errors.append(f"Forbidden perception dependency resolved: rosdep key '{rosdep_key}' -> apt package '{apt_pkg}'")

    if apt_pkg not in locked_map:
        errors.append(f"Resolved apt package '{apt_pkg}' (from rosdep key '{rosdep_key}') is not in versions.lock")
    else:
        ver = locked_map[apt_pkg]
        pkgs_to_install[apt_pkg] = f"{apt_pkg}={ver}" if ver else apt_pkg

if errors:
    print("FAIL: Unlocked or forbidden dependencies found:", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(1)

print(' '.join(pkgs_to_install.values()))
PYRESOLVE
)"

  if [ -n "${install_candidates}" ]; then
    echo "--> Verifying resolved dependencies were installed by the verified install-ros transaction..."
    local candidate
    for candidate in ${install_candidates}; do
      local package_name="${candidate%%=*}"
      local expected_version="${candidate#*=}"
      local installed_version
      installed_version="$(dpkg-query -W -f='${Version}' "${package_name}" 2>/dev/null || true)"
      if [ "${installed_version}" != "${expected_version}" ]; then
        echo "FAIL: ${package_name} must already be installed at ${expected_version}; found '${installed_version:-not installed}'." >&2
        echo "Rerun './deploy.sh install-ros' after versions.lock contains a complete target transaction." >&2
        return 1
      fi
    done
  fi

  echo "--> Checking dpkg dependency consistency without downloading packages..."
  apt-get check --no-download

  echo "--> Verifying workspace dependency closure with rosdep check..."
  rosdep check --from-paths src --ignore-src --rosdistro lyrical

  echo "PASS: All workspace dependencies resolved and satisfied."
  return 0
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  # Main CLI dispatch
  SUBCOMMAND="${1:-help}"
  shift || true

  case "${SUBCOMMAND}" in
    verify-lock)
      cmd_verify_lock "$@"
      ;;
    verify-closure)
      cmd_verify_closure "$@"
      ;;
    prepare-host)
      cmd_prepare_host "$@"
      ;;
    install-ros)
      cmd_install_ros "$@"
      ;;
    install-deps)
      cmd_install_deps "$@"
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      echo "Unknown subcommand: ${SUBCOMMAND}" >&2
      usage
      exit 1
      ;;
  esac
fi
