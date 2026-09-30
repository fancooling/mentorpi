#!/usr/bin/env bash
# Build, verify, transfer and install a paired release on a provisioned Pi 5.
# Uses python from PATH locally and system Python remotely. SSH honors the
# user's config; sudo may prompt. Stops on failure, retaining artifacts without
# rollback. Never starts or arms the controller or runs motor acceptance tests.
set -euo pipefail

# Parse the complete workflow before any long-running command. This prevents
# edits to this file during a build from mixing old and new script statements.
main() {
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd -- "$script_dir/../.." && pwd)"
  python="python"

  # Describe arguments and the operational boundary without contacting Docker/SSH.
  usage() {
    cat <<'EOF'
Usage: docker/ubuntu_tank/deploy.sh HOST [OPTIONS]

Build, smoke-test, export and deploy to an already provisioned Pi 5.
HOST is a hostname or alias from ~/.ssh/config. Finishes stopped and disarmed.

  --remote-dir PATH  Remote parent (default: ~/mentorpi-releases).
                     Relative paths are relative to the SSH user's home.
  --build-dir PATH   Reuse a completed build; repeat its smoke test.
  --output PATH      New local build directory (default: unique .work directory).
  --builder NAME     ARM64 Buildx builder (default: mentorpi-c3).
  --emulator PATH    Trusted ARM64 emulator; otherwise copy from local builder.
  -h, --help        Show help.

Requires Docker and a configured Python development environment on PATH locally,
and configured Docker, sudo,
controller/web settings, TLS and signed SROS2 keys on the Pi. Each run retains
its files in a unique directory. SSH failure may leave remote work running;
check Pi status before retrying. Physical acceptance is separate.
Output is also saved as deploy-<run ID>.log in the build directory on exit.
EOF
  }

  # Reject invalid local inputs before build or remote mutation.
  die() {
    printf '%s\n' "$*" >&2
    exit 2
  }

  # Quote a single argument for the remote POSIX shell (including embedded quotes).
  quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

  trap 'printf "%s\n" "Workflow failed; artifacts retained. Check Pi status before retrying; remote work may still be running." >&2' ERR
  trap 'printf "%s\n" "Interrupted; check Pi status before retrying." >&2; exit 130' INT TERM

  pi_host=""
  remote_parent="mentorpi-releases"
  builder="mentorpi-c3"
  build_dir=""
  output=""
  emulator=""
  while (($#)); do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      --remote-dir | --builder | --build-dir | --output | --emulator)
        (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
        case "$1" in
          --remote-dir) remote_parent="$2" ;;
          --builder) builder="$2" ;;
          --build-dir) build_dir="$2" ;;
          --output) output="$2" ;;
          --emulator) emulator="$2" ;;
        esac
        shift 2
        ;;
      -*) die "Unknown option: $1" ;;
      *)
        [[ -z "$pi_host" ]] || die "Only one SSH host is allowed"
        pi_host="$1"
        shift
        ;;
    esac
  done
  [[ "$pi_host" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || die "Provide an SSH hostname or alias"
  [[ "$remote_parent" != *$'\n'* ]] || die "Remote directory must not contain newlines"
  [[ -z "$build_dir" || -z "$output" ]] || die "Use either --build-dir or --output"
  command -v "$python" >/dev/null || die "Activate your Python development environment or put python on PATH"
  for command in docker ssh tar tee; do command -v "$command" >/dev/null || die "Missing command: $command"; done
  if [[ -n "$emulator" ]]; then
    emulator="$(realpath -e -- "$emulator")"
    [[ -f "$emulator" ]] || die "Emulator must be an existing file"
  fi
  run_name="release-$(date -u +%Y%m%dT%H%M%S%N)-$$"
  if [[ -n "$build_dir" ]]; then
    output="$(realpath -e -- "$build_dir")"
    [[ -f "$output/release.json" ]] || die "Build directory must contain release.json"
  else
    output="$(realpath -m -- "${output:-$repo_root/ubuntu_tank/.work/$run_name}")"
    [[ ! -e "$output" ]] || die "Output directory must be new"
  fi

  # build.py requires a nonexistent output directory. Spool beside it until exit,
  # then save the complete log there even if the build or deployment failed.
  if [[ "$output" == "$repo_root" || "$output" == "$repo_root/"* ]]; then
    [[ "$output" == "$repo_root/ubuntu_tank/.work/"* ]] || die "Repository output must be under ubuntu_tank/.work"
  fi
  mkdir -p -- "$(dirname -- "$output")"
  pending_log="$(mktemp "$(dirname -- "$output")/.deploy-log.XXXXXXXX")"
  log_file="$output/deploy-$run_name.log"

  # Drain tee before publishing the log and retain the workflow's failure status.
  # shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
  finish_log() {
    local result=$? log_result=0
    trap - ERR
    exec 1>&3 2>&4
    wait "$logger_pid" || log_result=$?
    if mkdir -p -- "$output" && mv -- "$pending_log" "$log_file"; then
      printf 'Deployment log: %s\n' "$log_file"
    else
      printf 'Could not save deployment log; temporary log: %s\n' "$pending_log" >&2
      log_result=1
    fi
    if ((result == 0)); then result=$log_result; fi
    exit "$result"
  }

  exec 3>&1 4>&2
  exec > >(tee -- "$pending_log") 2>&1
  logger_pid=$!
  trap finish_log EXIT
  printf 'Deployment log (saved on exit): %s\n' "$log_file"

  # Retain a snapshot of the complete host bundle, including the renamed Pi CLI.
  bundle_dir="$repo_root/ubuntu_tank/.work/host-bundles/$run_name"
  mkdir -p -- "$bundle_dir"
  cp -- "$script_dir"/{install.py,image_identity.py,tls_setup.py,compose.yaml,ubuntu-tank-container.service} "$bundle_dir/"
  if [[ -z "$build_dir" ]]; then
    "$python" "$script_dir/build.py" --builder "$builder" --output "$output"
  fi

  smoke_args=()
  if [[ -z "$emulator" && "$(uname -m)" != aarch64 && "$(uname -m)" != arm64 ]]; then
    inspection="$(docker buildx inspect "$builder" --bootstrap)"
    printf '%s\n' "$inspection"
    node="$(awk '/^Nodes:/{nodes=1; next} nodes && /^Name:/{print $2; exit}' <<<"$inspection")"
    [[ -n "$node" ]] || die "No local builder node found; supply --emulator"
    emulator="$repo_root/ubuntu_tank/.work/emulators/$run_name/buildkit-qemu-aarch64"
    mkdir -p -- "$(dirname -- "$emulator")"
    docker cp "buildx_buildkit_$node:/usr/bin/buildkit-qemu-aarch64" "$emulator"
  fi
  if [[ -n "$emulator" ]]; then smoke_args+=(--emulator "$emulator"); fi
  "$python" "$script_dir/smoke.py" "$output/release.json" "${smoke_args[@]}"

  transport="$output/$run_name"
  mkdir -- "$transport"
  cp -r -- "$bundle_dir" "$transport/host-tools"
  cp -- "$output/release.json" "$transport/release.json"
  "$python" "$script_dir/export_images.py" "$transport/release.json" "$transport/images.tar"
  tar -cf "$transport/transfer.tar" -C "$transport" release.json images.tar host-tools

  # Resolve the parent on the Pi without expanding user input as shell code.
  resolve='from pathlib import Path; import sys; p=Path(sys.argv[1]).expanduser(); p=p if p.is_absolute() else Path.home()/p; print(p.resolve())'
  resolve_command="/usr/bin/python3 -c $(quote "$resolve") $(quote "$remote_parent")"
  # Arguments are intentionally quoted locally for the remote shell.
  # shellcheck disable=SC2029
  parent="$(ssh "$pi_host" "$resolve_command")"
  [[ "$parent" == /* && "$parent" != *$'\n'* ]] || die "SSH returned an invalid remote directory"
  remote="$parent/$run_name"
  printf 'Transferring release to %s:%s\n' "$pi_host" "$remote"
  transfer_command="set -eu; umask 077; mkdir -p -- $(quote "$parent"); mkdir -- $(quote "$remote"); tar -xf - -C $(quote "$remote")"
  # shellcheck disable=SC2029
  ssh "$pi_host" "$transfer_command" <"$transport/transfer.tar"

  # Stop through the transferred CLI so upgrading hosts with old deploy.py works.
  # prepare-host installs install.py and refreshes the systemd boot/stop commands.
  remote_commands="set -eu
cd -- $(quote "$remote")
test \"\$(uname -m)\" = aarch64
sudo docker image load -i images.tar
sudo /usr/bin/python3 host-tools/install.py stop
sudo /usr/bin/python3 host-tools/install.py prepare-host
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py stage $(quote "$remote/release.json")
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py deploy $(quote "$remote/release.json")
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py target-test
sudo /usr/bin/python3 /opt/ubuntu_tank-container/install.py status"
  printf '%s\n' 'Updating the Pi; existing services will stop. Sudo may request a password.'
  ssh -t "$pi_host" "$remote_commands"
  printf 'Stopped integration passed. Release files: %s:%s\n' "$pi_host" "$remote"
  printf '%s\n' 'Controller remains stopped, disarmed and ownerless. Physical acceptance is separate.'

}

# Keep invocation and exit in one parsed command, including during file edits.
{
  main "$@"
  exit
}
