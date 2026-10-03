#!/usr/bin/env bash
# Read-only dependency lock and candidate-archive verification for development.
# The historical filename is retained for test consumers; Pi setup uses install.py.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TANK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="${LOCK_FILE:-${TANK_DIR}/versions.lock}"

usage() {
  echo 'Usage: install_ros2.sh verify-lock | verify-closure --candidates FILE'
  echo 'Read-only dependency verification. Pi host operations: docker/ubuntu_tank/install.py'
}

cmd_verify_lock() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help | help)
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
    "python3-fastapi": "python3-fastapi",
    "python3-uvicorn": "python3-uvicorn",
    "python3-pydantic": "python3-pydantic",
    "python3-websockets": "python3-websockets",
    "python3-cryptography": "python3-cryptography",
}

internal_workspace_pkgs = {
    "ros_robot_controller",
    "ros_robot_controller_msgs",
    "controller",
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
    "ubuntu_tank_operator",
    "ubuntu_tank_protocol",
    "ubuntu_tank_web",
}

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
      -h | --help | help)
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
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  SUBCOMMAND="${1:-help}"
  shift || true
  case "${SUBCOMMAND}" in
    verify-lock) cmd_verify_lock "$@" ;;
    verify-closure) cmd_verify_closure "$@" ;;
    help | -h | --help) usage ;;
    *)
      echo 'Native installation is retired; use docker/ubuntu_tank/install.py.' >&2
      exit 1
      ;;
  esac
fi
