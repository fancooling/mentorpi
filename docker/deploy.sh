#!/usr/bin/env bash
set -Eeuo pipefail

# Build and deploy only the observer-sidecar image. This script intentionally
# never installs host rules, accesses hardware, or manages the factory MentorPi
# container.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
CUSTOMIZATION_DIR="$SCRIPT_DIR/customization"
IMAGE_NAME="mentorpi-fan:latest"
CONTAINER_NAME="MentorPiFan"
REMOTE_PROJECT_DIR="mentorpi-fan/docker"
EXPECTED_IMAGE_ID=""

usage() {
    cat <<EOF
MentorPi Fan deployment tool

Usage:
  $(basename "$0") local                 Build and start locally
  $(basename "$0") remote <user@host>    Build, transfer, and start on the vendor Pi
  $(basename "$0") test                  Build and validate the ARM64 image
  $(basename "$0") help                  Show this help

The remote command requires the factory MentorPi container to be running. It
deploys only MentorPiFan and does not alter host drivers or factory services.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

validate_compose_policy() {
    local rendered services

    rendered="$(compose config)"
    services="$(compose config --services)"
    [[ "$services" == "mentorpi-fan" ]] \
        || fail "Compose must define only the mentorpi-fan service"

    local required_line
    for required_line in \
        "name: mentorpi-fan" \
        "container_name: MentorPiFan" \
        "image: mentorpi-fan:latest" \
        "init: true" \
        "network_mode: host" \
        "pids_limit: 64" \
        "platform: linux/arm64" \
        "pull_policy: never" \
        "read_only: true" \
        "user: nginx" \
        "- ALL" \
        "- no-new-privileges:true" \
        "- /tmp:rw,noexec,nosuid,nodev,size=16m"; do
        grep -Fq -- "$required_line" <<<"$rendered" \
            || fail "Compose isolation requirement missing: $required_line"
    done

    if grep -Eq '^[[:space:]]+(privileged|devices|volumes|cap_add|ports|ipc|pid):' <<<"$rendered"; then
        fail "Compose must not define privilege, devices, volumes, added capabilities, port mappings, or host IPC/PID"
    fi
}

validate_image() {
    local architecture entrypoint hardware_access role runtime_user

    EXPECTED_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME")"
    architecture="$(docker image inspect --format '{{.Architecture}}' "$IMAGE_NAME")"
    runtime_user="$(docker image inspect --format '{{.Config.User}}' "$IMAGE_NAME")"
    entrypoint="$(docker image inspect --format '{{json .Config.Entrypoint}}' "$IMAGE_NAME")"
    role="$(docker image inspect --format '{{index .Config.Labels "io.mentorpi.role"}}' "$IMAGE_NAME")"
    hardware_access="$(docker image inspect --format '{{index .Config.Labels "io.mentorpi.hardware-access"}}' "$IMAGE_NAME")"

    [[ "$EXPECTED_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || fail "Could not resolve an immutable image ID for $IMAGE_NAME"
    [[ "$architecture" == "arm64" ]] \
        || fail "$IMAGE_NAME architecture is $architecture; expected arm64"
    [[ "$runtime_user" == "nginx" ]] \
        || fail "$IMAGE_NAME does not use the nginx runtime user"
    [[ "$entrypoint" == '["nginx"]' ]] \
        || fail "$IMAGE_NAME has an unexpected entrypoint: $entrypoint"
    [[ "$role" == "observer-sidecar" && "$hardware_access" == "none" ]] \
        || fail "$IMAGE_NAME is missing the observer-only provenance labels"

    docker run --rm --platform linux/arm64 --entrypoint sh "$IMAGE_NAME" -c '
        set -eu
        test ! -e /opt/ros
        test -f /usr/share/nginx/html/app.js
        test "$(grep -c "state.socket.send" /usr/share/nginx/html/app.js)" -eq 2
        if grep -Eq "op[[:space:]]*:[[:space:]]*\"(advertise|publish|call_service|service_request|advertise_service|unadvertise_service|send_action_goal|cancel_action_goal)\"" /usr/share/nginx/html/*.js; then
            echo "Disallowed outbound rosbridge operation found" >&2
            exit 1
        fi
        if grep -Eq "/cmd_vel|/controller/cmd_vel|/app/cmd_vel|set_motor|set_servo|set_pwm|motor_controller|servo_controller" /usr/share/nginx/html/*.js; then
            echo "Disallowed actuator reference found" >&2
            exit 1
        fi
        if grep -Eq "proxy_pass[^;]*9090" /etc/nginx/nginx.conf; then
            echo "Nginx must not proxy the factory rosbridge" >&2
            exit 1
        fi
        nginx -t
    '
}

build_image() {
    echo "Building ARM64 image $IMAGE_NAME..."
    compose build
}

prepare_image() {
    echo "Validating sidecar-only Compose policy..."
    validate_compose_policy
    build_image
    echo "Validating observer image policy..."
    validate_image
}

wait_for_local_health() {
    local attempt status

    for attempt in {1..30}; do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
        case "$status" in
            healthy)
                echo "MentorPiFan is healthy at http://127.0.0.1:8081/."
                return 0
                ;;
            unhealthy|exited|dead)
                docker logs "$CONTAINER_NAME" || true
                echo "$CONTAINER_NAME entered state: $status" >&2
                return 1
                ;;
        esac
        sleep 1
    done

    docker logs "$CONTAINER_NAME" || true
    echo "Timed out waiting for $CONTAINER_NAME to become healthy" >&2
    return 1
}

verify_local_runtime() {
    local actual expected expected_image_id="${1:-$EXPECTED_IMAGE_ID}"

    actual="$(docker inspect --format '{{.Image}}|{{.HostConfig.Privileged}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Init}}|{{.HostConfig.PidsLimit}}|{{.Config.User}}|{{json .HostConfig.Binds}}|{{json .HostConfig.Devices}}|{{json .HostConfig.CapAdd}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER_NAME")"
    expected="$expected_image_id|false|true|host|true|64|nginx|null|null|null|[\"ALL\"]|[\"no-new-privileges:true\"]|mentorpi-fan"
    [[ "$actual" == "$expected" ]] || {
        echo "Runtime isolation mismatch." >&2
        echo "Expected: $expected" >&2
        echo "Actual:   $actual" >&2
        return 1
    }
}

cleanup_failed_local() {
    local previous_image_id="$1"
    local previous_container_id="$2"
    local current_container_id

    current_container_id="$(docker inspect --format '{{.Id}}' "$CONTAINER_NAME" 2>/dev/null || true)"
    if [[ "$previous_container_id" =~ ^[0-9a-f]{64}$ && "$current_container_id" == "$previous_container_id" ]]; then
        echo "The previous MentorPiFan container was not replaced and remains running." >&2
    else
        echo "Removing the failed sidecar rollout..." >&2
        compose down || true
    fi

    if [[ "$previous_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Restoring the previous sidecar image tag $previous_image_id..." >&2
        docker tag "$previous_image_id" "$IMAGE_NAME"
        echo "Restore a compatible prior Compose/config revision before restarting it." >&2
    else
        echo "No previous sidecar image was available; failed container removed." >&2
    fi
}

deploy_local() {
    local previous_container_id previous_image_id

    [[ $# -eq 0 ]] || fail "The local command accepts no options"
    require_command docker
    prepare_image
    previous_image_id="$(docker inspect --format '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)"
    previous_container_id="$(docker inspect --format '{{.Id}}' "$CONTAINER_NAME" 2>/dev/null || true)"

    echo "Starting the local observer sidecar..."
    if ! compose up -d --no-build \
        || ! wait_for_local_health \
        || ! verify_local_runtime; then
        cleanup_failed_local "$previous_image_id" "$previous_container_id"
        fail "Local MentorPiFan deployment failed"
    fi
}

validate_target() {
    local target="$1"

    [[ "$target" != -* ]] || fail "SSH target must not begin with '-'"
    [[ "$target" != *:* ]] || fail "Use an SSH host or IPv4 address without a remote path"
    [[ "$target" != *[[:space:]]* ]] || fail "SSH target must not contain whitespace"
}

verify_factory_remote() {
    local target="$1"
    local architecture

    echo "Checking remote Docker, Compose, gzip, architecture, and factory container..."
    ssh -- "$target" '
        set -eu
        command -v gzip >/dev/null
        command -v docker >/dev/null
        docker version >/dev/null
        docker compose version >/dev/null
    ' || fail "Remote host needs gzip, Docker Engine access, and Docker Compose v2"

    architecture="$(ssh -- "$target" uname -m)"
    case "$architecture" in
        aarch64|arm64)
            ;;
        *)
            fail "Remote host architecture is $architecture; expected ARM64"
            ;;
    esac

    ssh -- "$target" 'test "$(docker inspect --format "{{.State.Running}}" MentorPi 2>/dev/null)" = true' \
        || fail "Factory container MentorPi is not running on $target"
}

copy_runtime_files() {
    local target="$1"

    echo "Copying sidecar Compose and build files..."
    ssh -- "$target" 'mkdir -p "$HOME/mentorpi-fan/docker/customization/web"'
    scp -- "$COMPOSE_FILE" "$target:$REMOTE_PROJECT_DIR/docker-compose.yml"
    scp -- \
        "$CUSTOMIZATION_DIR/Dockerfile" \
        "$CUSTOMIZATION_DIR/README.md" \
        "$CUSTOMIZATION_DIR/nginx.conf" \
        "$target:$REMOTE_PROJECT_DIR/customization/"
    scp -- "$CUSTOMIZATION_DIR/web/"* "$target:$REMOTE_PROJECT_DIR/customization/web/"
}

wait_for_remote_health() {
    local target="$1"

    ssh -- "$target" '
        set -eu
        attempt=0
        while [ "$attempt" -lt 30 ]; do
            status=$(docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" MentorPiFan 2>/dev/null || true)
            case "$status" in
                healthy)
                    echo "MentorPiFan is healthy at http://127.0.0.1:8081/."
                    exit 0
                    ;;
                unhealthy|exited|dead)
                    docker logs MentorPiFan || true
                    echo "MentorPiFan entered state: $status" >&2
                    exit 1
                    ;;
            esac
            attempt=$((attempt + 1))
            sleep 1
        done

        docker logs MentorPiFan || true
        echo "Timed out waiting for MentorPiFan to become healthy" >&2
        exit 1
    '
}

verify_remote_runtime() {
    local target="$1"
    local expected_image_id="$2"
    local actual expected

    actual="$(ssh -- "$target" 'docker inspect --format "{{.Image}}|{{.HostConfig.Privileged}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Init}}|{{.HostConfig.PidsLimit}}|{{.Config.User}}|{{json .HostConfig.Binds}}|{{json .HostConfig.Devices}}|{{json .HostConfig.CapAdd}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{index .Config.Labels \"com.docker.compose.project\"}}" MentorPiFan')"
    expected="$expected_image_id|false|true|host|true|64|nginx|null|null|null|[\"ALL\"]|[\"no-new-privileges:true\"]|mentorpi-fan"
    [[ "$actual" == "$expected" ]] || {
        echo "Remote runtime isolation mismatch." >&2
        echo "Expected: $expected" >&2
        echo "Actual:   $actual" >&2
        return 1
    }
}

restore_remote_tag() {
    local target="$1"
    local previous_image_id="$2"

    if [[ "$previous_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Restoring previous remote image tag $previous_image_id..." >&2
        ssh -- "$target" "docker tag '$previous_image_id' '$IMAGE_NAME'"
    else
        ssh -- "$target" "docker image rm '$IMAGE_NAME'" >/dev/null 2>&1 || true
    fi
}

cleanup_failed_remote() {
    local target="$1"
    local previous_image_id="$2"
    local previous_container_id="$3"
    local current_container_id

    current_container_id="$(ssh -- "$target" 'docker inspect --format "{{.Id}}" MentorPiFan 2>/dev/null || true')"
    if [[ "$previous_container_id" =~ ^[0-9a-f]{64}$ && "$current_container_id" == "$previous_container_id" ]]; then
        echo "The previous remote MentorPiFan container was not replaced and remains running." >&2
    else
        echo "Removing the failed remote sidecar rollout..." >&2
        ssh -- "$target" 'cd "$HOME/mentorpi-fan/docker" && docker compose -f docker-compose.yml down' || true
    fi
    restore_remote_tag "$target" "$previous_image_id"
    if [[ -n "$previous_image_id" ]]; then
        echo "Restore a compatible prior Compose/config revision before restarting it." >&2
    else
        echo "No previous remote sidecar image was available; failed container removed." >&2
    fi
}

start_remote_sidecar() {
    local target="$1"
    local previous_image_id="$2"
    local previous_container_id="$3"

    if ! ssh -- "$target" 'cd "$HOME/mentorpi-fan/docker" && docker compose -f docker-compose.yml up -d --no-build' \
        || ! wait_for_remote_health "$target" \
        || ! verify_remote_runtime "$target" "$EXPECTED_IMAGE_ID"; then
        cleanup_failed_remote "$target" "$previous_image_id" "$previous_container_id"
        return 1
    fi

    ssh -- "$target" 'cd "$HOME/mentorpi-fan/docker" && docker compose -f docker-compose.yml ps'
}

deploy_remote() {
    local target="${1:-}"
    local loaded_image_id previous_container_id previous_image_id
    [[ -n "$target" ]] || fail "Remote target is required, for example pi@mentorpi.local"
    shift
    [[ $# -eq 0 ]] || fail "The remote command accepts only one user@host target"
    validate_target "$target"

    require_command docker
    require_command ssh
    require_command scp
    require_command gzip

    ssh -q -o ConnectTimeout=8 -- "$target" exit \
        || fail "Cannot connect to $target over SSH"
    verify_factory_remote "$target"
    prepare_image
    previous_image_id="$(ssh -- "$target" 'docker inspect --format "{{.Image}}" MentorPiFan 2>/dev/null || true')"
    previous_container_id="$(ssh -- "$target" 'docker inspect --format "{{.Id}}" MentorPiFan 2>/dev/null || true')"
    if [[ ! "$previous_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        previous_image_id=""
    fi

    echo "Streaming $IMAGE_NAME to $target..."
    if ! docker save "$IMAGE_NAME" | gzip -c | ssh -- "$target" 'gzip -dc | docker load'; then
        restore_remote_tag "$target" "$previous_image_id" || true
        fail "Image transfer to $target failed"
    fi

    if ! loaded_image_id="$(ssh -- "$target" "docker image inspect --format '{{.Id}}' '$IMAGE_NAME'")"; then
        restore_remote_tag "$target" "$previous_image_id" || true
        fail "Could not inspect the transferred image on $target"
    fi
    if [[ "$loaded_image_id" != "$EXPECTED_IMAGE_ID" ]]; then
        restore_remote_tag "$target" "$previous_image_id" || true
        fail "Remote image ID $loaded_image_id does not match local $EXPECTED_IMAGE_ID"
    fi
    if ! copy_runtime_files "$target"; then
        restore_remote_tag "$target" "$previous_image_id" || true
        fail "Could not copy sidecar runtime files to $target"
    fi

    echo "Starting only MentorPiFan on $target..."
    start_remote_sidecar "$target" "$previous_image_id" "$previous_container_id" \
        || fail "Remote MentorPiFan deployment failed"
    echo "Deployment complete. Open http://${target#*@}:8081/."
}

test_image() {
    [[ $# -eq 0 ]] || fail "The test command accepts no options"
    require_command docker

    prepare_image
    echo "All MentorPi Fan checks passed for $EXPECTED_IMAGE_ID."
}

case "${1:-help}" in
    local)
        shift
        deploy_local "$@"
        ;;
    remote)
        shift
        deploy_remote "$@"
        ;;
    test)
        shift
        test_image "$@"
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        usage >&2
        fail "Unknown command: $1"
        ;;
esac
