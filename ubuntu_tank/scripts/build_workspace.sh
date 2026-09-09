#!/usr/bin/env bash
# build_workspace.sh - rosdep resolution and colcon build script for ROS 2 Lyrical
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UBUNTU_TANK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${UBUNTU_TANK_DIR}/src"

usage() {
  cat <<'EOF'
Usage: ./scripts/build_workspace.sh [options]

Resolves locked dependencies with rosdep and builds workspace packages natively
with colcon for ROS 2 Lyrical.

Options:
  -h, --help               Display this help text and exit
  --clean                  Remove build/, install/, and log/ directories before building
  --dry-run                Validate workspace and print build plan without mutating filesystem
  --merge-install          Build using colcon --merge-install layout
  --packages <pkg1,pkg2>   Comma-separated list of packages to build (default: all)
  --install-base <dir>     Directory for colcon --install-base (default: ubuntu_tank/install)
  --build-base <dir>       Directory for colcon --build-base (default: ubuntu_tank/build)
EOF
}

# 1. Check for legacy environment variables
if [ -n "${MACHINE_TYPE:-}" ]; then
  echo "ERROR: Legacy environment variable MACHINE_TYPE='${MACHINE_TYPE}' detected." >&2
  echo "Milestone 3 requires machine_type to be passed as a ROS parameter, not an environment variable." >&2
  exit 1
fi

CLEAN=false
DRY_RUN=false
MERGE_INSTALL=false
SELECTED_PACKAGES=""
INSTALL_BASE="${UBUNTU_TANK_DIR}/install"
BUILD_BASE="${UBUNTU_TANK_DIR}/build"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --clean)
      CLEAN=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --merge-install)
      MERGE_INSTALL=true
      shift
      ;;
    --packages)
      SELECTED_PACKAGES="${2:-}"
      shift 2 || { echo "ERROR: --packages requires an argument" >&2; exit 1; }
      ;;
    --install-base)
      INSTALL_BASE="${2:-}"
      shift 2 || { echo "ERROR: --install-base requires an argument" >&2; exit 1; }
      ;;
    --build-base)
      BUILD_BASE="${2:-}"
      shift 2 || { echo "ERROR: --build-base requires an argument" >&2; exit 1; }
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

echo "============================================================"
echo "Ubuntu Tank Colcon Build (ROS 2 Lyrical)"
echo "============================================================"

# 2. Check workspace source packages
if [ ! -d "${SRC_DIR}" ]; then
  echo "ERROR: Source directory missing: ${SRC_DIR}" >&2
  exit 1
fi

PKGS=()
for d in "${SRC_DIR}"/*; do
  if [ -d "${d}" ] && [ -f "${d}/package.xml" ]; then
    PKGS+=("$(basename "${d}")")
  fi
done

if [ "${#PKGS[@]}" -eq 0 ]; then
  echo "ERROR: No ROS packages found with package.xml in ${SRC_DIR}" >&2
  exit 1
fi
echo "Found ${#PKGS[@]} workspace packages: ${PKGS[*]}"

validate_clean_target() {
  local target="$1"
  local label="$2"

  if [ -z "${target}" ]; then
    echo "ERROR: Clean target for ${label} cannot be empty." >&2
    exit 1
  fi

  local canon_tank canon_target
  canon_tank="$(realpath -m "${UBUNTU_TANK_DIR}")"
  canon_target="$(realpath -m "${target}")"

  # Reject root, home, or broad system directories
  if [ "${canon_target}" = "/" ] || [ "${canon_target}" = "/home" ] || [ "${canon_target}" = "/etc" ] || \
     [ "${canon_target}" = "/usr" ] || [ "${canon_target}" = "/var" ] || [ "${canon_target}" = "/tmp" ] || \
     { [ -n "${HOME:-}" ] && [ "${canon_target}" = "${HOME}" ]; }; then
    echo "ERROR: Refusing to clean broad or system directory for ${label}: '${target}'" >&2
    exit 1
  fi

  if [[ "${canon_target}" != "${canon_tank}/"* ]]; then
    echo "ERROR: Refusing to clean ${label} '${target}' outside workspace '${canon_tank}'." >&2
    exit 1
  fi

  # Cleanup is intentionally narrower than the build-output CLI. Only the
  # conventional build directories or an explicitly disposable .work subtree
  # are workspace-owned deletion targets. Merely being below ubuntu_tank is not
  # sufficient: root files such as README.md and versions.lock must be protected.
  local expected_target=""
  case "${label}" in
    build-base)
      expected_target="${canon_tank}/build"
      ;;
    install-base)
      expected_target="${canon_tank}/install"
      ;;
    log-dir)
      expected_target="${canon_tank}/log"
      ;;
    *)
      echo "ERROR: Unknown clean-target class: ${label}" >&2
      exit 1
      ;;
  esac

  if [ "${canon_target}" != "${expected_target}" ] && \
     [[ "${canon_target}" != "${canon_tank}/.work/"* ]]; then
    echo "ERROR: Refusing to clean non-disposable workspace path for ${label}: '${target}'" >&2
    echo "Cleanup targets must be the standard ${label} directory or a descendant of ${canon_tank}/.work/." >&2
    exit 1
  fi
}

# 3. Handle --clean
if [ "${CLEAN}" = true ]; then
  validate_clean_target "${BUILD_BASE}" "build-base"
  validate_clean_target "${INSTALL_BASE}" "install-base"
  validate_clean_target "${UBUNTU_TANK_DIR}/log" "log-dir"

  if [ "${DRY_RUN}" = true ]; then
    echo "[dry-run] Validated clean targets: ${BUILD_BASE} ${INSTALL_BASE} ${UBUNTU_TANK_DIR}/log"
  else
    echo "Cleaning build directories..."
    rm -rf "${BUILD_BASE}" "${INSTALL_BASE}" "${UBUNTU_TANK_DIR}/log"
  fi
fi

# 4. Source ROS 2 Lyrical environment
ROS_SETUP="${ROS_DISTRO_PATH:-/opt/ros/lyrical}/setup.bash"
if [ -f "${ROS_SETUP}" ]; then
  echo "Sourcing ROS environment: ${ROS_SETUP}"
  # shellcheck source=/dev/null
  set +u
  source "${ROS_SETUP}"
  set -u
else
  if [ "${DRY_RUN}" = false ]; then
    echo "ERROR: ROS 2 setup script not found at ${ROS_SETUP}" >&2
    echo "Please run './deploy.sh install-ros' and './deploy.sh install-deps' on the target Pi 5." >&2
    exit 1
  else
    echo "[dry-run] Notice: ROS 2 setup not found at ${ROS_SETUP} (expected during workstation test/dry-run)"
  fi
fi

# 5. Build colcon command array
COLCON_CMD=("colcon" "build" "--base-paths" "${SRC_DIR}")

if [ "${MERGE_INSTALL}" = true ]; then
  COLCON_CMD+=("--merge-install")
else
  COLCON_CMD+=("--symlink-install")
fi

COLCON_CMD+=("--build-base" "${BUILD_BASE}" "--install-base" "${INSTALL_BASE}")

if [ -n "${SELECTED_PACKAGES}" ]; then
  IFS=',' read -r -a PKG_ARRAY <<< "${SELECTED_PACKAGES}"
  COLCON_CMD+=("--packages-select" "${PKG_ARRAY[@]}")
fi

echo "Planned build command: ${COLCON_CMD[*]}"

if [ "${DRY_RUN}" = true ]; then
  echo "[dry-run] Workspace and arguments verified successfully. Dry-run complete."
  exit 0
fi

# 6. Execute build
if ! command -v colcon >/dev/null 2>&1; then
  echo "ERROR: 'colcon' command not found in PATH." >&2
  echo "Ensure ros-dev-tools / python3-colcon-common-extensions are installed." >&2
  exit 1
fi

echo "Executing: ${COLCON_CMD[*]}"
exec "${COLCON_CMD[@]}"
