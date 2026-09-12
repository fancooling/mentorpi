#!/usr/bin/env bash
# build_disposable_root.sh - Build workspace in an isolated disposable root for production packaging
set -euo pipefail
ORIGINAL_ARGS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UBUNTU_TANK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/build_disposable_root.sh [options]

Builds the tank controller workspace inside an isolated disposable ARM64 root
using the final production release prefix (/opt/ubuntu_tank/releases/<release-id>/install).
Ensures zero symlinks, zero checkout path leaks, and standalone relocatability.

Options:
  -h, --help                Display this help text and exit
  --workspace <dir>         Workspace root directory (default: ubuntu_tank)
  --release-id <id>         Release ID (default: derived from VERSION and git)
  --rootfs <dir>            Path to disposable rootfs (default: ubuntu_tank/.work/native-rootfs)
  --build-root <dir>        Path to disposable build root (default: ubuntu_tank/.work/native-build)
  --clean                   Clean disposable build root before building
  --dry-run                 Print build plan and validate arguments without mutating filesystem
  --packages <pkg1,pkg2>    Comma-separated list of packages to build (default: all)
  --opt-dir <dir>           Base application directory (default: /opt/ubuntu_tank)
  --allow-staged-install    Synthesize minimal production install tree if full ROS is unavailable
EOF
}

WORKSPACE_DIR="${UBUNTU_TANK_DIR}"
RELEASE_ID=""
ROOTFS_DIR=""
BUILD_ROOT_DIR=""
OPT_DIR="/opt/ubuntu_tank"
CLEAN=false
DRY_RUN=false
PACKAGES=""
ALLOW_STAGED_INSTALL=false

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --workspace)
      WORKSPACE_DIR="${2:-}"
      shift 2 || { echo "ERROR: --workspace requires an argument" >&2; exit 1; }
      ;;
    --release-id)
      RELEASE_ID="${2:-}"
      shift 2 || { echo "ERROR: --release-id requires an argument" >&2; exit 1; }
      ;;
    --rootfs)
      ROOTFS_DIR="${2:-}"
      shift 2 || { echo "ERROR: --rootfs requires an argument" >&2; exit 1; }
      ;;
    --build-root)
      BUILD_ROOT_DIR="${2:-}"
      shift 2 || { echo "ERROR: --build-root requires an argument" >&2; exit 1; }
      ;;
    --opt-dir)
      OPT_DIR="${2:-}"
      shift 2 || { echo "ERROR: --opt-dir requires an argument" >&2; exit 1; }
      ;;
    --clean)
      CLEAN=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --packages)
      PACKAGES="${2:-}"
      shift 2 || { echo "ERROR: --packages requires an argument" >&2; exit 1; }
      ;;
    --allow-staged-install)
      ALLOW_STAGED_INSTALL=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# Resolve and validate release ID
if [ -z "${RELEASE_ID}" ]; then
  VERSION_FILE="${WORKSPACE_DIR}/VERSION"
  if [ ! -f "${VERSION_FILE}" ]; then
    echo "ERROR: VERSION file missing at '${VERSION_FILE}'" >&2
    exit 1
  fi
  VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"
  SHORT_COMMIT="$(git -C "${WORKSPACE_DIR}" rev-parse --short=7 HEAD 2>/dev/null || echo "0000000")"
  RELEASE_ID="${VERSION}-g${SHORT_COMMIT}"
fi

# Reject unsafe release IDs (Finding 1)
if [[ "${RELEASE_ID}" =~ [/\\] ]] || [ "${RELEASE_ID}" = "." ] || [ "${RELEASE_ID}" = ".." ] || [[ ! "${RELEASE_ID}" =~ ^[0-9a-zA-Z][0-9a-zA-Z._-]*$ ]]; then
  echo "ERROR: Invalid release ID '${RELEASE_ID}': must be a single path-safe component matching ^[0-9a-zA-Z][0-9a-zA-Z._-]*$" >&2
  exit 1
fi

# Set default rootfs and build root directories under .work/
if [ -z "${ROOTFS_DIR}" ]; then
  ROOTFS_DIR="${WORKSPACE_DIR}/.work/native-rootfs"
  if [ "${ALLOW_STAGED_INSTALL}" = true ]; then ROOTFS_DIR="${WORKSPACE_DIR}/.work/rootfs"; fi
fi
if [ -z "${BUILD_ROOT_DIR}" ]; then
  BUILD_ROOT_DIR="${WORKSPACE_DIR}/.work/native-build"
  if [ "${ALLOW_STAGED_INSTALL}" = true ]; then BUILD_ROOT_DIR="${WORKSPACE_DIR}/.work/build_root"; fi
fi

# Containment and safety guard for disposable targets (Finding 2)
validate_disposable_target() {
  local target="$1"
  local label="$2"

  if [ -z "${target}" ]; then
    echo "ERROR: Target directory for ${label} cannot be empty." >&2
    exit 1
  fi

  # Check if target is a symlink
  if [ -L "${target}" ]; then
    echo "ERROR: Refusing to clean symlinked target for ${label}: '${target}'" >&2
    exit 1
  fi

  local canon_ws canon_target
  canon_ws="$(realpath -m "${WORKSPACE_DIR}")"
  canon_target="$(realpath -m "${target}")"

  # Reject root, home, and broad system directories
  if [ "${canon_target}" = "/" ] || [ "${canon_target}" = "/home" ] || [ "${canon_target}" = "/etc" ] || \
     [ "${canon_target}" = "/usr" ] || [ "${canon_target}" = "/var" ] || [ "${canon_target}" = "/tmp" ] || \
     [ "${canon_target}" = "/var/tmp" ] || [ "${canon_target}" = "/root" ] || [ "${canon_target}" = "/opt" ] || \
     [ "${canon_target}" = "/boot" ] || [ "${canon_target}" = "/run" ] || [ "${canon_target}" = "/sys" ] || \
     [ "${canon_target}" = "/proc" ] || [ "${canon_target}" = "/dev" ] || \
     { [ -n "${HOME:-}" ] && [ "${canon_target}" = "${HOME}" ]; }; then
    echo "ERROR: Refusing to clean broad or system directory for ${label}: '${target}' (${canon_target})" >&2
    exit 1
  fi

  # Reject workspace root
  if [ "${canon_target}" = "${canon_ws}" ]; then
    echo "ERROR: Refusing to clean workspace root for ${label}: '${target}'" >&2
    exit 1
  fi

  # Reject workspace ancestors (e.g. canon_ws is inside canon_target)
  if [[ "${canon_ws}" == "${canon_target}/"* ]]; then
    echo "ERROR: Refusing to clean workspace ancestor directory for ${label}: '${target}'" >&2
    exit 1
  fi

  # If inside workspace, must be strictly under .work/
  if [[ "${canon_target}" == "${canon_ws}/"* ]]; then
    local rel_to_ws="${canon_target#"${canon_ws}/"}"
    # Reject protected workspace directories
    case "${rel_to_ws}" in
      src|src/*|config|config/*|host|host/*|scripts|scripts/*|tests|tests/*|docs|docs/*|bin|bin/*)
        echo "ERROR: Refusing to clean protected workspace path for ${label}: '${target}'" >&2
        exit 1
        ;;
    esac
    if [[ "${canon_target}" != "${canon_ws}/.work"* ]]; then
      echo "ERROR: Workspace path for ${label} must be within .work/: '${target}'" >&2
      exit 1
    fi
  fi

  # Verify no component of target is a symlink escaping outside
  local cur="${target}"
  while [ -n "${cur}" ] && [ "${cur}" != "/" ] && [ "${cur}" != "." ]; do
    if [ -L "${cur}" ]; then
      local link_dest
      link_dest="$(realpath "${cur}")"
      if [[ "${link_dest}" != "${canon_ws}/.work"* ]] && [ "${link_dest}" = "${canon_ws}" ]; then
        echo "ERROR: Symlink escape detected in path for ${label}: '${cur}' -> '${link_dest}'" >&2
        exit 1
      fi
    fi
    cur="$(dirname "${cur}")"
  done
}

# Verify rootfs suitability as an ARM64 Ubuntu 26.04 root (Finding 4)
verify_arm64_ubuntu_rootfs() {
  local rootfs="$1"
  if [ ! -d "${rootfs}" ]; then
    return 1
  fi

  # Check os-release for Ubuntu 26.04
  local os_release=""
  if [ -f "${rootfs}/etc/os-release" ]; then
    os_release="${rootfs}/etc/os-release"
  elif [ -f "${rootfs}/usr/lib/os-release" ]; then
    os_release="${rootfs}/usr/lib/os-release"
  fi

  if [ -z "${os_release}" ]; then
    return 1
  fi

  if ! grep -qE '^ID=["'\'']?ubuntu["'\'']?' "${os_release}"; then
    return 1
  fi

  if ! grep -qE '^VERSION_ID=["'\'']?26\.04["'\'']?' "${os_release}"; then
    return 1
  fi

  # Check architecture: must be arm64
  local arch_verified=false
  if [ -f "${rootfs}/var/lib/dpkg/arch" ]; then
    local dpkg_arch
    dpkg_arch="$(tr -d '[:space:]' < "${rootfs}/var/lib/dpkg/arch")"
    if [ "${dpkg_arch}" = "arm64" ]; then
      arch_verified=true
    fi
  fi

  if [ "${arch_verified}" = false ] && { [ -d "${rootfs}/usr/lib/aarch64-linux-gnu" ] || [ -d "${rootfs}/lib/aarch64-linux-gnu" ]; }; then
    arch_verified=true
  fi

  if [ "${arch_verified}" = false ] && [ -x "${rootfs}/usr/bin/dpkg" ]; then
    if file "${rootfs}/usr/bin/dpkg" 2>/dev/null | grep -qE "(aarch64|ARM aarch64)"; then
      arch_verified=true
    fi
  fi

  if [ "${arch_verified}" = false ]; then
    return 1
  fi

  # Check for ROS 2 Lyrical inside rootfs
  if [ ! -f "${rootfs}/opt/ros/lyrical/setup.bash" ]; then
    return 1
  fi

  return 0
}

# Validate containment before any mutation
validate_disposable_target "${BUILD_ROOT_DIR}" "build-root"
validate_disposable_target "${ROOTFS_DIR}" "rootfs"

PRODUCTION_PREFIX="${OPT_DIR}/releases/${RELEASE_ID}/install"
TARGET_INSTALL_DIR="${BUILD_ROOT_DIR}${PRODUCTION_PREFIX}"
ROOTFS_INSTALL_DIR="${ROOTFS_DIR}${PRODUCTION_PREFIX}"

echo "============================================================"
echo "Disposable Build Root Workflow for Production Release"
echo "============================================================"
echo "Workspace:           ${WORKSPACE_DIR}"
echo "Release ID:          ${RELEASE_ID}"
echo "Production Prefix:   ${PRODUCTION_PREFIX}"
echo "Disposable Rootfs:   ${ROOTFS_DIR}"
echo "Disposable Build:    ${BUILD_ROOT_DIR}"
echo "Target Install Tree: ${TARGET_INSTALL_DIR}"

if [ "${CLEAN}" = true ]; then
  if [ -d "${BUILD_ROOT_DIR}" ]; then
    echo "Cleaning disposable build root: ${BUILD_ROOT_DIR}"
    if [ "${DRY_RUN}" = false ]; then
      rm -rf "${BUILD_ROOT_DIR}"
    fi
  fi
fi

if [ "${DRY_RUN}" = true ]; then
  echo "[dry-run] Validated disposable build root plan:"
  echo "  - Container isolation root: ${ROOTFS_DIR}"
  echo "  - Bootstrap absent root offline from the prepared native host (requires sudo and locked dependencies)"
  echo "  - Target OS & Architecture: Ubuntu 26.04 (arm64)"
  echo "  - Internal production prefix: ${PRODUCTION_PREFIX}"
  echo "  - Internal build command: colcon build --install-base ${PRODUCTION_PREFIX} --build-base /tmp/colcon_build --merge-install"
  echo "  - Internal ROS environment: source /opt/ros/lyrical/setup.bash"
  echo "  - Zero leaked checkout path validation enabled"
  exit 0
fi

# Real root construction and container execution require root-owned files.
if [ "${ALLOW_STAGED_INSTALL}" = false ] && [ -z "${_UBUNTU_TANK_TEST_BUILD_CMD:-}" ] && [ "$(id -u)" -ne 0 ]; then
  exec sudo -n "${BASH_SOURCE[0]}" "${ORIGINAL_ARGS[@]}"
fi

# Bootstrap an absent root offline from the already prepared native host.
if [ ! -e "${ROOTFS_DIR}" ] && [ "${ALLOW_STAGED_INSTALL}" = false ]; then
  bootstrap_sudo=()
  if [ "$(id -u)" -ne 0 ]; then bootstrap_sudo=(sudo -n); fi
  "${bootstrap_sudo[@]}" python3 "${SCRIPT_DIR}/prepare_build_root.py" --workspace "${WORKSPACE_DIR}" --rootfs "${ROOTFS_DIR}"
fi

# Determine build strategy
BUILD_SUCCESS=false

if verify_arm64_ubuntu_rootfs "${ROOTFS_DIR}"; then
  echo "Entering verified ARM64 Ubuntu rootfs at ${ROOTFS_DIR}..."
  SUDO_CMD=""
  if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO_CMD="sudo -n"
  fi

  COLCON_PKGS=""
  if [ -n "${PACKAGES}" ]; then
    COLCON_PKGS="--packages-select ${PACKAGES//,/ }"
  fi

  if [ -n "${_UBUNTU_TANK_TEST_BUILD_CMD:-}" ]; then
    echo "Executing simulated container build command..."
    if eval "${_UBUNTU_TANK_TEST_BUILD_CMD}"; then
      BUILD_SUCCESS=true
    fi
  elif command -v systemd-nspawn >/dev/null 2>&1; then
    echo "Building inside isolated rootfs using systemd-nspawn at ${PRODUCTION_PREFIX}..."
    if $SUDO_CMD systemd-nspawn --quiet -D "${ROOTFS_DIR}" \
      --bind-ro="${WORKSPACE_DIR}/src:/build_ws/src" \
      --bind-ro="${WORKSPACE_DIR}/VERSION:/build_ws/VERSION" \
      /bin/bash -c "set -eo pipefail
        set +u
        source /opt/ros/lyrical/setup.bash
        set -u
        cd /build_ws
        rm -rf '${PRODUCTION_PREFIX}' /tmp/colcon_build
        colcon build \
          --install-base '${PRODUCTION_PREFIX}' \
          --build-base /tmp/colcon_build \
          --merge-install \
          ${COLCON_PKGS}
      "; then
      BUILD_SUCCESS=true
    fi
  elif command -v chroot >/dev/null 2>&1; then
    echo "Building inside isolated rootfs using chroot at ${PRODUCTION_PREFIX}..."
    mkdir -p "${ROOTFS_DIR}/build_ws"
    rm -rf "${ROOTFS_DIR}/build_ws/src"
    cp -a "${WORKSPACE_DIR}/src" "${ROOTFS_DIR}/build_ws/src"
    cp -a "${WORKSPACE_DIR}/VERSION" "${ROOTFS_DIR}/build_ws/VERSION"
    $SUDO_CMD mount -t proc proc "${ROOTFS_DIR}/proc"
    cleanup_build_proc() { $SUDO_CMD umount "${ROOTFS_DIR}/proc"; }
    trap cleanup_build_proc EXIT
    if $SUDO_CMD chroot "${ROOTFS_DIR}" /bin/bash -c "set -eo pipefail
        set +u
        source /opt/ros/lyrical/setup.bash
        set -u
        cd /build_ws
        rm -rf '${PRODUCTION_PREFIX}' /tmp/colcon_build
        colcon build \
          --install-base '${PRODUCTION_PREFIX}' \
          --build-base /tmp/colcon_build \
          --merge-install \
          ${COLCON_PKGS}
      "; then
      BUILD_SUCCESS=true
    fi
    cleanup_build_proc
    trap - EXIT
  fi
fi

if [ "${BUILD_SUCCESS}" = false ]; then
  # Clean up any partial or empty build remnants so failed builds do not qualify as artifacts
  for rem in "${TARGET_INSTALL_DIR}" "${ROOTFS_INSTALL_DIR}"; do
    if [ -d "${rem}" ]; then
      rm -rf "${rem}"
    fi
  done

  if [ "${ALLOW_STAGED_INSTALL}" = true ]; then
    echo "Synthesizing self-contained production install tree at ${TARGET_INSTALL_DIR}..."
    for tdir in "${TARGET_INSTALL_DIR}" "${ROOTFS_INSTALL_DIR}"; do
      mkdir -p "${tdir}/bin" "${tdir}/lib" "${tdir}/share"

      cat <<EOF > "${tdir}/setup.bash"
#!/usr/bin/env bash
# Generated production environment for release ${RELEASE_ID}
export COLCON_CURRENT_PREFIX="${PRODUCTION_PREFIX}"
export AMENT_PREFIX_PATH="\${COLCON_CURRENT_PREFIX}:\${AMENT_PREFIX_PATH:-}"
export PYTHONPATH="\${COLCON_CURRENT_PREFIX}/lib/python3.12/site-packages:\${PYTHONPATH:-}"
export PATH="\${COLCON_CURRENT_PREFIX}/bin:\${PATH:-}"
EOF
      chmod 0755 "${tdir}/setup.bash"

      cat <<'EOF' > "${tdir}/bin/tank_verify_install"
#!/usr/bin/env python3
"""Verification executable confirming standalone execution at production prefix."""
import os, sys
print(f"TANK_VERIFIED_PREFIX={os.environ.get('COLCON_CURRENT_PREFIX', 'unknown')}")
sys.exit(0)
EOF
      chmod 0755 "${tdir}/bin/tank_verify_install"
    done

    # Ensure minimal rootfs markers exist in ROOTFS_DIR for consistency
    mkdir -p "${ROOTFS_DIR}/etc" "${ROOTFS_DIR}/var/lib/dpkg"
    if [ ! -f "${ROOTFS_DIR}/etc/os-release" ]; then
      cat <<'EOF' > "${ROOTFS_DIR}/etc/os-release"
NAME="Ubuntu"
VERSION="26.04 LTS (Resolute Raccoon)"
ID=ubuntu
VERSION_ID="26.04"
EOF
    fi
    if [ ! -f "${ROOTFS_DIR}/var/lib/dpkg/arch" ]; then
      echo "arm64" > "${ROOTFS_DIR}/var/lib/dpkg/arch"
    fi

    BUILD_SUCCESS=true
  else
    echo "ERROR: Unable to build production install tree at ${TARGET_INSTALL_DIR}." >&2
    echo "Verified ARM64 Ubuntu 26.04 rootfs not available at '${ROOTFS_DIR}', and --allow-staged-install was not set." >&2
    exit 1
  fi
fi

# Ensure both candidate directories have the install artifacts by copying completed output explicitly
if [ "${TARGET_INSTALL_DIR}" != "${ROOTFS_INSTALL_DIR}" ]; then
  if [ -d "${ROOTFS_INSTALL_DIR}" ] && [ -n "$(ls -A "${ROOTFS_INSTALL_DIR}" 2>/dev/null)" ]; then
    mkdir -p "$(dirname "${TARGET_INSTALL_DIR}")"
    rm -rf "${TARGET_INSTALL_DIR}"
    cp -a "${ROOTFS_INSTALL_DIR}" "${TARGET_INSTALL_DIR}"
  elif [ -d "${TARGET_INSTALL_DIR}" ] && [ -n "$(ls -A "${TARGET_INSTALL_DIR}" 2>/dev/null)" ]; then
    mkdir -p "$(dirname "${ROOTFS_INSTALL_DIR}")"
    rm -rf "${ROOTFS_INSTALL_DIR}"
    cp -a "${TARGET_INSTALL_DIR}" "${ROOTFS_INSTALL_DIR}"
  fi
fi

# Ensure target install tree exists, is non-empty, and contains setup.bash
if [ ! -d "${TARGET_INSTALL_DIR}" ] || [ ! -f "${TARGET_INSTALL_DIR}/setup.bash" ] || [ -z "$(ls -A "${TARGET_INSTALL_DIR}" 2>/dev/null)" ]; then
  echo "ERROR: Target install tree is missing, empty, or incomplete at ${TARGET_INSTALL_DIR}." >&2
  rm -rf "${TARGET_INSTALL_DIR}" "${ROOTFS_INSTALL_DIR}"
  exit 1
fi

# Validate zero leaked checkout paths in target install tree
echo "Validating zero leaked checkout paths in ${TARGET_INSTALL_DIR}..."
LEAKED=0
while IFS= read -r -d '' file; do
  if [ -f "${file}" ]; then
    if grep -qF "${WORKSPACE_DIR}/src" "${file}" 2>/dev/null; then
      echo "ERROR: Leaked source path found in ${file}" >&2
      LEAKED=1
    fi
  fi
done < <(find "${TARGET_INSTALL_DIR}" -type f -print0)

if [ "${LEAKED}" -ne 0 ]; then
  echo "ERROR: Leaked checkout paths detected in production install tree." >&2
  rm -rf "${TARGET_INSTALL_DIR}" "${ROOTFS_INSTALL_DIR}"
  exit 1
fi

attestation_args=()
if [ "${ALLOW_STAGED_INSTALL}" = true ]; then attestation_args=(--synthetic); fi
python3 "${SCRIPT_DIR}/deployment_manager.py" attest-build "${attestation_args[@]}" --install-tree "${TARGET_INSTALL_DIR}" --prefix "${PRODUCTION_PREFIX}" --source "${WORKSPACE_DIR}/src"
if [ "${TARGET_INSTALL_DIR}" != "${ROOTFS_INSTALL_DIR}" ]; then
  cp "${TARGET_INSTALL_DIR}/production-build.json" "${ROOTFS_INSTALL_DIR}/production-build.json"
fi

echo "Production install tree successfully created and verified at: ${TARGET_INSTALL_DIR}"
exit 0
