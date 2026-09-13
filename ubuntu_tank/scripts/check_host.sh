#!/usr/bin/env bash
# check_host.sh - Read-only host, architecture, EEPROM, and device preflight checks
# Implements Milestone 2 preflight and mutual exclusion gates.
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: ./scripts/check_host.sh [options]

Performs read-only host preflight and mutual exclusion checks:
  1. Hardware Platform: Raspberry Pi 5
  2. Operating System: Ubuntu 26.04 LTS (Resolute)
  3. Architecture: ARM64 / aarch64
  4. Bootloader EEPROM: Raspberry Pi 5 firmware date check (min 2024-05-17)
  5. Locales: UTF-8 locale configured
  6. Disk Space: Minimum 5GB free on root filesystem
  7. Time Synchronization: NTP / systemd-timesyncd status
  8. Kernel: Reports running kernel release
  9. Serial Device: Validates /dev/rrc character device and checks for lock contention
 10. Mutual Exclusion: Rejects factory/sidecar/replacement Docker containers,
     factory boot units, and conflicting ROS/device owner processes.

Options:
  -h, --help        Show this help message
  --strict          Fail on any warning (default)
  --no-strict       Allow warnings to pass with status 0
  --json            Emit machine-readable JSON status summary
  --quiet           Suppress progress messages, emit only errors

Environment Overrides (for non-mutating test harness only):
  UBUNTU_TANK_MOCK_TARGET=1        Simulate clean Raspberry Pi 5 ARM64 Ubuntu 26.04
  UBUNTU_TANK_MOCK_MODEL=<model>   Override hardware model
  UBUNTU_TANK_MOCK_ARCH=<arch>     Override architecture detection
  UBUNTU_TANK_MOCK_OS_VER=<ver>    Override OS version detection
  UBUNTU_TANK_MOCK_EEPROM=<date>   Override EEPROM date
  UBUNTU_TANK_MOCK_DOCKER_PS=<str> Override Docker container list
  UBUNTU_TANK_MOCK_DOCKER_FAIL=1   Simulate unreadable Docker daemon
  UBUNTU_TANK_MOCK_SERIAL_HOLDER=<pid> Simulate process holding serial device
HELP
}

# Normalize the EEPROM CURRENT field emitted by rpi-eeprom-update. Current
# releases append a parenthesized Unix epoch to the human-readable timestamp;
# mocks and older releases may provide an ISO date or a bare epoch instead.
normalize_eeprom_release() {
  local value="$1"
  if [[ "${value}" =~ \(([0-9]+)\)[[:space:]]*$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif [[ "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "${value}"
  else
    date -d "${value}" +%Y-%m-%d 2>/dev/null || printf '%s\n' "${value}"
  fi
}

# -----------------------------------------------------------------------------
# Mutual Exclusion Checks (Callable independently before install/activate/arm)
# -----------------------------------------------------------------------------
check_mutual_exclusion() {
  local errors=0

  # 1. Docker container check
  if [ -n "${UBUNTU_TANK_MOCK_DOCKER_FAIL:-}" ]; then
    echo "FAIL [Mutual Exclusion]: Docker is installed but daemon/containers cannot be inspected. Failing closed." >&2
    errors=$((errors + 1))
  elif [ -n "${UBUNTU_TANK_MOCK_DOCKER_PS:-}" ]; then
    local containers="${UBUNTU_TANK_MOCK_DOCKER_PS}"
    if [ "${containers}" = "none" ] || [ "${containers}" = "EMPTY" ]; then
      containers=""
    fi
  elif command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
      echo "FAIL [Mutual Exclusion]: Docker command is available but docker info/daemon cannot be inspected. Failing closed." >&2
      errors=$((errors + 1))
      local containers=""
    else
      local containers
      if ! containers="$(docker ps -a --format '{{.Names}}' 2>&1)"; then
        echo "FAIL [Mutual Exclusion]: Docker command is available but 'docker ps -a' failed to enumerate containers: ${containers}. Failing closed." >&2
        errors=$((errors + 1))
        containers=""
      fi
    fi
  else
    local containers=""
  fi

  if [ -n "${containers:-}" ]; then
    local forbidden_patterns=("MentorPi" "MentorPiFan" "mentorpi" "tank_runtime" "runtime-core")
    for pattern in "${forbidden_patterns[@]}"; do
      if echo "${containers}" | grep -qi "${pattern}"; then
        echo "FAIL [Mutual Exclusion]: Conflicting Docker container found matching '${pattern}'" >&2
        echo "     Active containers: $(echo "${containers}" | tr '\n' ' ')" >&2
        errors=$((errors + 1))
      fi
    done
  fi

  # 2. Factory boot units check
  if [ -z "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    local factory_units=("mentorpi.service" "mentorpi-start.service" "mentorpi-fan.service" "hiwonder-chassis.service")
    for unit in "${factory_units[@]}"; do
      if [ -f "/etc/systemd/system/${unit}" ] || [ -f "/lib/systemd/system/${unit}" ]; then
        echo "FAIL [Mutual Exclusion]: Conflicting factory systemd unit exists: ${unit}" >&2
        errors=$((errors + 1))
      fi
      if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "${unit}" 2>/dev/null; then
          echo "FAIL [Mutual Exclusion]: Conflicting factory systemd unit is active: ${unit}" >&2
          errors=$((errors + 1))
        fi
      fi
    done
  fi

  # 3. Conflicting ROS / Hardware owner process check
  if [ -z "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    local ancestor_pids=" $$ "
    local cur_p="$$"
    while [ -n "${cur_p}" ] && [ "${cur_p}" -gt 1 ] 2>/dev/null; do
      local p
      p="$(ps -o ppid= -p "${cur_p}" 2>/dev/null | tr -d ' ' || echo 1)"
      if [ -z "${p}" ] || [ "${p}" -le 1 ]; then
        break
      fi
      ancestor_pids="${ancestor_pids}${p} "
      cur_p="${p}"
    done

    local candidate_pids
    candidate_pids="$(pgrep -f "ros_robot_controller_node|odom_publisher_node|roscore|rosmaster|hiwonder|/opt/mentorpi" || true)"
    local conflicting_procs=""
    for cpid in ${candidate_pids}; do
      if echo "${ancestor_pids}" | grep -q " ${cpid} "; then
        continue
      fi
      local cmdline
      cmdline="$(tr '\000' ' ' <"/proc/${cpid}/cmdline" 2>/dev/null || ps -o args= -p "${cpid}" 2>/dev/null || echo "")"
      if echo "${cmdline}" | grep -qE "(check_host|deploy\.sh|install_ros2|test_install_workflow|pytest|unittest)"; then
        continue
      fi
      conflicting_procs="${conflicting_procs}${cpid} "
    done
    conflicting_procs="$(echo "${conflicting_procs}" | tr -s ' ' | sed 's/^ //;s/ $//')"
    if [ -n "${conflicting_procs}" ]; then
      echo "FAIL [Mutual Exclusion]: Conflicting ROS/hardware owner process running: PIDs: ${conflicting_procs}" >&2
      errors=$((errors + 1))
    fi
  fi

  # 4. Device holder contention check
  local serial_dev="${UBUNTU_TANK_MOCK_SERIAL_DEV:-/dev/rrc}"
  if [ -n "${UBUNTU_TANK_MOCK_SERIAL_HOLDER:-}" ]; then
    echo "FAIL [Mutual Exclusion]: Conflicting process PID(s) ${UBUNTU_TANK_MOCK_SERIAL_HOLDER} hold ${serial_dev} open." >&2
    errors=$((errors + 1))
  elif [ -e "${serial_dev}" ]; then
    local real_dev
    real_dev="$(readlink -f "${serial_dev}" || echo "${serial_dev}")"
    if [ -c "${real_dev}" ]; then
      if ! command -v fuser >/dev/null 2>&1; then
        echo "FAIL [Mutual Exclusion]: ${serial_dev} character device is present but 'fuser' command is not available to verify exclusivity. Failing closed." >&2
        errors=$((errors + 1))
      else
        local holder
        holder="$(fuser "${real_dev}" 2>/dev/null || true)"
        if [ -n "${holder}" ]; then
          echo "FAIL [Mutual Exclusion]: Conflicting process PID(s) ${holder} currently hold ${real_dev} open." >&2
          errors=$((errors + 1))
        fi
      fi
    else
      echo "FAIL [Mutual Exclusion]: ${serial_dev} exists but is not a character device (${real_dev}). Failing closed." >&2
      errors=$((errors + 1))
    fi
  fi

  if [ "${errors}" -gt 0 ]; then
    return 1
  fi
  return 0
}

# -----------------------------------------------------------------------------
# Host Preflight Checks
# -----------------------------------------------------------------------------
check_host() {
  local strict="${1:-true}"
  local json_out="${2:-false}"
  local quiet="${3:-false}"
  local errors=0
  local warnings=0

  log_msg() {
    if [ "${quiet}" = "false" ] && [ "${json_out}" = "false" ]; then
      echo "$@"
    fi
  }

  log_msg "============================================================"
  log_msg "MentorPi Native Tank Controller - Host Preflight Check"
  log_msg "Target: Clean Ubuntu 26.04 LTS (Resolute) on Raspberry Pi 5 ARM64"
  log_msg "============================================================"

  # 1. Hardware Model Check (Raspberry Pi 5)
  local model_status="fail"
  local model_msg=""
  local model="unknown"
  if [ -n "${UBUNTU_TANK_MOCK_MODEL:-}" ]; then
    model="${UBUNTU_TANK_MOCK_MODEL}"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    model="Raspberry Pi 5 Model B Rev 1.0"
  elif [ -f /proc/device-tree/model ]; then
    model="$(tr -d '\000' </proc/device-tree/model)"
  elif [ -f /sys/firmware/devicetree/base/model ]; then
    model="$(tr -d '\000' </sys/firmware/devicetree/base/model)"
  fi

  if echo "${model}" | grep -qi "Raspberry Pi 5"; then
    model_status="pass"
    model_msg="${model}"
    log_msg "Checking hardware model (Raspberry Pi 5)... OK (${model})"
  else
    model_status="fail"
    model_msg="Target hardware is not a Raspberry Pi 5 (Detected: '${model}')"
    log_msg "Checking hardware model (Raspberry Pi 5)... FAIL (${model})"
    echo "  ${model_msg}" >&2
    errors=$((errors + 1))
  fi

  # 2. Architecture Check
  local arch_status="fail"
  local arch_msg=""
  local arch
  local uname_m
  if [ -n "${UBUNTU_TANK_MOCK_ARCH:-}" ]; then
    arch="${UBUNTU_TANK_MOCK_ARCH}"
    uname_m="${UBUNTU_TANK_MOCK_ARCH}"
    [ "${uname_m}" = "arm64" ] && uname_m="aarch64"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    arch="arm64"
    uname_m="aarch64"
  else
    arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
    uname_m="$(uname -m)"
  fi

  if [ "${arch}" = "arm64" ] && [ "${uname_m}" = "aarch64" ]; then
    arch_status="pass"
    arch_msg="${arch} / ${uname_m}"
    log_msg "Checking architecture (arm64 / aarch64)... OK (${arch_msg})"
  else
    arch_status="fail"
    arch_msg="Expected dpkg arm64 / kernel aarch64, got dpkg '${arch}' / kernel '${uname_m}'"
    log_msg "Checking architecture (arm64 / aarch64)... FAIL"
    echo "  ${arch_msg}" >&2
    errors=$((errors + 1))
  fi

  # 3. Operating System Check
  local os_status="fail"
  local os_msg=""
  local os_id="unknown"
  local os_ver="unknown"
  local os_codename="unknown"
  if [ -n "${UBUNTU_TANK_MOCK_OS_VER:-}" ]; then
    os_id="ubuntu"
    os_ver="${UBUNTU_TANK_MOCK_OS_VER}"
    os_codename="resolute"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    os_id="ubuntu"
    os_ver="26.04"
    os_codename="resolute"
  elif [ -f /etc/os-release ]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    os_id="${ID:-unknown}"
    os_ver="${VERSION_ID:-unknown}"
    os_codename="${VERSION_CODENAME:-unknown}"
  fi

  if [ "${os_id}" = "ubuntu" ] && [ "${os_ver}" = "26.04" ]; then
    os_status="pass"
    os_msg="Ubuntu ${os_ver} (${os_codename})"
    log_msg "Checking operating system (Ubuntu 26.04 LTS)... OK (${os_msg})"
  else
    os_status="fail"
    os_msg="Expected Ubuntu 26.04 (Resolute), got ID='${os_id}', VERSION_ID='${os_ver}', CODENAME='${os_codename}'"
    log_msg "Checking operating system (Ubuntu 26.04 LTS)... FAIL"
    echo "  ${os_msg}" >&2
    errors=$((errors + 1))
  fi

  # 4. Bootloader EEPROM Firmware Date Check (>= 2024-05-17)
  local eeprom_status="fail"
  local eeprom_msg=""
  local min_eeprom="2024-05-17"
  local min_epoch=1715904000
  local eeprom_raw=""

  if [ -n "${UBUNTU_TANK_MOCK_EEPROM:-}" ]; then
    eeprom_raw="${UBUNTU_TANK_MOCK_EEPROM}"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    eeprom_raw="2024-05-17"
  elif command -v rpi-eeprom-update >/dev/null 2>&1; then
    local eeprom_out
    eeprom_out="$(rpi-eeprom-update 2>&1 || true)"
    local line
    line="$(echo "${eeprom_out}" | grep -i "CURRENT:" | sed 's/.*CURRENT:[[:space:]]*//' | head -n1)"
    eeprom_raw="${line}"
  fi

  if [ -n "${eeprom_raw}" ]; then
    eeprom_raw="$(normalize_eeprom_release "${eeprom_raw}")"
  fi

  if [ -z "${eeprom_raw}" ]; then
    eeprom_status="fail"
    eeprom_msg="rpi-eeprom-update not found or failed to read bootloader firmware date"
    log_msg "Checking Raspberry Pi EEPROM firmware date... FAIL"
    echo "  ${eeprom_msg}" >&2
    echo "  Ensure Raspberry Pi 5 EEPROM is updated per https://ubuntu.com/tutorials/how-to-install-ubuntu-on-your-raspberry-pi" >&2
    errors=$((errors + 1))
  else
    local is_old=false
    local is_valid=false
    if [[ "${eeprom_raw}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
      is_valid=true
      if [[ "${eeprom_raw}" < "${min_eeprom}" ]]; then
        is_old=true
      fi
    elif [[ "${eeprom_raw}" =~ ^[0-9]+$ ]]; then
      is_valid=true
      if [ "${eeprom_raw}" -lt "${min_epoch}" ]; then
        is_old=true
      fi
    fi

    if [ "${is_valid}" = "false" ]; then
      eeprom_status="fail"
      eeprom_msg="Malformed or unparseable EEPROM firmware release date: '${eeprom_raw}'"
      log_msg "Checking Raspberry Pi EEPROM firmware date... FAIL (${eeprom_raw})"
      echo "  ${eeprom_msg}" >&2
      errors=$((errors + 1))
    elif [ "${is_old}" = "true" ]; then
      eeprom_status="fail"
      eeprom_msg="Firmware release '${eeprom_raw}' is older than required minimum (${min_eeprom})"
      log_msg "Checking Raspberry Pi EEPROM firmware date... FAIL (${eeprom_raw})"
      echo "  ${eeprom_msg}" >&2
      echo "  Update bootloader per https://ubuntu.com/tutorials/how-to-install-ubuntu-on-your-raspberry-pi" >&2
      errors=$((errors + 1))
    else
      eeprom_status="pass"
      eeprom_msg="${eeprom_raw} >= ${min_eeprom}"
      log_msg "Checking Raspberry Pi EEPROM firmware date... OK (${eeprom_msg})"
    fi
  fi

  # 5. Locale Check
  local locale_status="fail"
  local locale_msg=""
  local current_locale="${LC_ALL:-${LANG:-}}"
  if [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    current_locale="en_US.UTF-8"
  fi

  if echo "${current_locale}" | grep -qi "utf" || (command -v locale >/dev/null 2>&1 && locale -a 2>/dev/null | grep -qiE "en_US\.utf-?8|C\.utf-?8"); then
    locale_status="pass"
    locale_msg="${current_locale:-UTF-8 available}"
    log_msg "Checking system locale configuration... OK (${locale_msg})"
  else
    locale_status="fail"
    locale_msg="UTF-8 locale not set (Current: '${current_locale}'). Run './deploy.sh prepare-host'."
    log_msg "Checking system locale configuration... WARNING"
    echo "  ${locale_msg}" >&2
    warnings=$((warnings + 1))
  fi

  # 6. Disk Space Check (Minimum 5GB free)
  local disk_status="fail"
  local disk_msg=""
  local free_mb=0
  if [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    free_mb=10240
  else
    free_mb="$(df -BM / 2>/dev/null | awk 'NR==2 {gsub(/M/,""); print $4}' || echo 0)"
  fi

  if [ "${free_mb}" -ge 5120 ]; then
    disk_status="pass"
    disk_msg="${free_mb} MB free (>= 5120 MB required)"
    log_msg "Checking free disk space on root filesystem... OK (${disk_msg})"
  else
    disk_status="fail"
    disk_msg="Insufficient disk space: ${free_mb} MB available (minimum 5120 MB required)"
    log_msg "Checking free disk space on root filesystem... FAIL"
    echo "  ${disk_msg}" >&2
    errors=$((errors + 1))
  fi

  # 7. Time Synchronization Check
  local time_status="fail"
  local time_msg=""
  local ntp_sync="unknown"
  if [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    ntp_sync="yes"
  elif command -v timedatectl >/dev/null 2>&1; then
    ntp_sync="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "unknown")"
  fi

  if [ "${ntp_sync}" = "yes" ]; then
    time_status="pass"
    time_msg="NTP synchronized"
    log_msg "Checking system time synchronization... OK (${time_msg})"
  else
    time_status="warn"
    time_msg="Time synchronization status is '${ntp_sync}'"
    log_msg "Checking system time synchronization... WARNING"
    echo "  ${time_msg}" >&2
    warnings=$((warnings + 1))
  fi

  # 8. Kernel Release Report
  local kernel_rel
  if [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    kernel_rel="6.8.0-1008-raspi"
  else
    kernel_rel="$(uname -r)"
  fi
  log_msg "Kernel release: ${kernel_rel}"

  # 9. STM32 Serial Hardware & Contention Check
  local serial_status="pass"
  local serial_msg=""
  if [ -n "${UBUNTU_TANK_MOCK_SERIAL_HOLDER:-}" ]; then
    serial_status="fail"
    serial_msg="Process PID(s) ${UBUNTU_TANK_MOCK_SERIAL_HOLDER} hold /dev/rrc open"
    log_msg "Checking STM32 serial interface (/dev/rrc)... FAIL"
    echo "  ${serial_msg}" >&2
    errors=$((errors + 1))
  elif [ -e "/dev/rrc" ]; then
    local real_dev
    real_dev="$(readlink -f /dev/rrc || echo "/dev/rrc")"
    if [ -c "${real_dev}" ]; then
      if ! command -v fuser >/dev/null 2>&1; then
        serial_status="fail"
        serial_msg="/dev/rrc character device present but 'fuser' is not available to verify exclusivity"
        log_msg "Checking STM32 serial interface (/dev/rrc)... FAIL"
        echo "  ${serial_msg}" >&2
        echo "  Install 'psmisc' to verify serial device exclusivity." >&2
        errors=$((errors + 1))
      else
        local holder
        holder="$(fuser "${real_dev}" 2>/dev/null || true)"
        if [ -n "${holder}" ]; then
          serial_status="fail"
          serial_msg="Process PID(s) ${holder} currently hold ${real_dev} open"
          log_msg "Checking STM32 serial interface (/dev/rrc)... FAIL"
          echo "  ${serial_msg}" >&2
          errors=$((errors + 1))
        else
          serial_status="pass"
          serial_msg="Character device ${real_dev} ready with zero contention"
          log_msg "Checking STM32 serial interface (/dev/rrc)... OK (${real_dev})"
        fi
      fi
    else
      serial_status="fail"
      serial_msg="/dev/rrc exists but is not a character device (${real_dev})"
      log_msg "Checking STM32 serial interface (/dev/rrc)... FAIL"
      echo "  ${serial_msg}" >&2
      errors=$((errors + 1))
    fi
  else
    serial_status="info"
    serial_msg="/dev/rrc symlink not present yet (configured in Milestone 5)"
    log_msg "Checking STM32 serial interface (/dev/rrc)... INFO: /dev/rrc symlink not present yet."
  fi

  # 10. Mutual Exclusion Gate
  local mutex_status="fail"
  local mutex_msg=""
  log_msg -n "Checking mutual exclusion (containers, units, conflicting ROS)... "
  if check_mutual_exclusion; then
    mutex_status="pass"
    mutex_msg="No conflicting containers, units, or processes"
    log_msg "OK"
  else
    mutex_status="fail"
    mutex_msg="Mutual exclusion checks failed"
    log_msg "FAIL"
    errors=$((errors + 1))
  fi

  # 11. Pending Reboot Check
  local reboot_req=false
  local reboot_check_file="/run/reboot-required"
  if [ -n "${UBUNTU_TANK_MOCK_REBOOT_FILE:-}" ]; then
    reboot_check_file="${UBUNTU_TANK_MOCK_REBOOT_FILE}"
  elif [ -n "${UBUNTU_TANK_MOCK_TARGET:-}" ]; then
    reboot_check_file="/tmp/nonexistent_ubuntu_tank_reboot_required"
  fi
  if [ -f "${reboot_check_file}" ]; then
    reboot_req=true
    log_msg "WARNING: ${reboot_check_file} exists! A system reboot is required."
    warnings=$((warnings + 1))
  fi

  log_msg "------------------------------------------------------------"
  log_msg "Preflight Summary: ${errors} error(s), ${warnings} warning(s)"
  log_msg "------------------------------------------------------------"

  # JSON Output mode
  if [ "${json_out}" = "true" ]; then
    local overall_status="FAIL"
    if [ "${errors}" -eq 0 ] && { [ "${strict}" = "false" ] || [ "${warnings}" -eq 0 ]; }; then
      overall_status="PASS"
    fi
    python3 - \
      "${model_status}" "${model_msg}" \
      "${arch_status}" "${arch_msg}" \
      "${os_status}" "${os_msg}" \
      "${eeprom_status}" "${eeprom_msg}" \
      "${locale_status}" "${locale_msg}" \
      "${disk_status}" "${disk_msg}" \
      "${time_status}" "${time_msg}" \
      "${serial_status}" "${serial_msg}" \
      "${mutex_status}" "${mutex_msg}" \
      "${reboot_req}" "${errors}" "${warnings}" "${overall_status}" <<'PYJSON'
import json
import sys

values = sys.argv[1:]
names = ("model", "architecture", "os", "eeprom", "locale", "disk", "time_sync", "serial", "mutual_exclusion")
payload = {}
for index, name in enumerate(names):
    payload[name] = {
        "status": values[index * 2],
        "message": values[index * 2 + 1],
    }
payload["reboot_required"] = values[18] == "true"
payload["errors"] = int(values[19])
payload["warnings"] = int(values[20])
payload["overall"] = values[21]
json.dump(payload, sys.stdout, indent=2)
sys.stdout.write("\n")
PYJSON
  fi

  if [ "${errors}" -gt 0 ]; then
    [ "${quiet}" = "false" ] && [ "${json_out}" = "false" ] && echo "FAIL: Host preflight checks failed with ${errors} error(s)." >&2
    return 1
  fi

  if [ "${strict}" = "true" ] && [ "${warnings}" -gt 0 ]; then
    [ "${quiet}" = "false" ] && [ "${json_out}" = "false" ] && echo "FAIL: Host preflight strict check failed with ${warnings} warning(s)." >&2
    if [ "${reboot_req}" = "true" ]; then
      return 2
    fi
    return 1
  fi

  [ "${quiet}" = "false" ] && [ "${json_out}" = "false" ] && echo "PASS: Host preflight verification complete and accepted."
  return 0
}

# -----------------------------------------------------------------------------
# CLI Entrypoint
# -----------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  STRICT="true"
  JSON="false"
  QUIET="false"

  while [ $# -gt 0 ]; do
    case "$1" in
      -h | --help | help)
        usage
        exit 0
        ;;
      --no-strict)
        STRICT="false"
        shift
        ;;
      --strict)
        STRICT="true"
        shift
        ;;
      --json)
        JSON="true"
        shift
        ;;
      --quiet)
        QUIET="true"
        shift
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  check_host "${STRICT}" "${JSON}" "${QUIET}"
fi
