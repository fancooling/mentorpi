"""
Constants and thresholds for MentorPi Pi 5 Web Control.

Implements Milestone 10 monotonic timing parameters, lease durations,
conservative speed limits, and telemetry freshness thresholds in accordance with
docs/MENTORPI_WEB_CONTROL_DESIGN.md.
"""

from typing import Final

# Protocol and schema identity
PROTOCOL_VERSION: Final[str] = "1.0.0"
API_VERSION: Final[str] = "v1"
SCHEMA_VERSION: Final[int] = 1
SUPPORTED_PROTOCOL_VERSIONS: Final[list[str]] = ["1.0.0"]

# Monotonic timing and lease parameters (seconds)
LEASE_DURATION_SEC: Final[float] = 0.150  # 150 ms maximum lease duration
CHALLENGE_INTERVAL_SEC: Final[float] = 0.050  # 50 ms challenge issue interval
LEASE_CHECK_INTERVAL_SEC: Final[float] = 0.020  # 20 ms check tick interval
MAX_CONTINUOUS_HOLD_SEC: Final[float] = 5.0  # 5.0 s continuous hold cap
IDLE_TIMEOUT_SEC: Final[float] = 30.0  # 30.0 s idle timeout while armed
FIRST_COMMAND_DEADLINE_SEC: Final[float] = (
    0.250  # 250 ms from arming to verified zero delivery
)
TARGET_ZERO_SUBMISSION_DEADLINE_SEC: Final[float] = (
    0.020  # 20 ms healthy zero delivery target
)

# Monotonic nanosecond equivalents for deterministic integer arithmetic
LEASE_DURATION_NS: Final[int] = int(LEASE_DURATION_SEC * 1e9)
CHALLENGE_INTERVAL_NS: Final[int] = int(CHALLENGE_INTERVAL_SEC * 1e9)
LEASE_CHECK_INTERVAL_NS: Final[int] = int(LEASE_CHECK_INTERVAL_SEC * 1e9)
MAX_CONTINUOUS_HOLD_NS: Final[int] = int(MAX_CONTINUOUS_HOLD_SEC * 1e9)
IDLE_TIMEOUT_NS: Final[int] = int(IDLE_TIMEOUT_SEC * 1e9)
FIRST_COMMAND_DEADLINE_NS: Final[int] = int(FIRST_COMMAND_DEADLINE_SEC * 1e9)
TARGET_ZERO_SUBMISSION_DEADLINE_NS: Final[int] = int(
    TARGET_ZERO_SUBMISSION_DEADLINE_SEC * 1e9
)

# Speed limits (m/s and rad/s)
DEFAULT_WEB_LINEAR_SPEED: Final[float] = 0.20  # m/s
DEFAULT_WEB_ANGULAR_SPEED: Final[float] = 0.50  # rad/s
MAX_PERMISSIBLE_LINEAR_SPEED: Final[float] = 0.50  # m/s (hard system limit)
MAX_PERMISSIBLE_ANGULAR_SPEED: Final[float] = 2.00  # rad/s (hard system limit)

# Telemetry freshness thresholds (seconds)
BATTERY_FRESHNESS_MAX_AGE_SEC: Final[float] = 3.0  # 1 Hz publisher -> 3.0 s max age
GUARD_STATE_FRESHNESS_MAX_AGE_SEC: Final[float] = (
    0.50  # 10 Hz publisher -> 0.5 s max age
)
ODOM_FRESHNESS_MAX_AGE_SEC: Final[float] = 0.25  # 20 Hz publisher -> 0.25 s max age
DELIVERY_OBSERVATION_MAX_AGE_SEC: Final[float] = 0.25  # 0.25 s max age when active
MIN_SAFE_BATTERY_VOLTAGE: Final[float] = 9.60  # V

# Log and buffer constraints
MAX_LOG_LINES_LIMIT: Final[int] = 200
MAX_LOG_BYTES_LIMIT: Final[int] = 65536  # 64 KiB

# Lock paths and hierarchy levels
LOCK_PATH_DEPLOYMENT: Final[str] = "/run/lock/ubuntu_tank/deploy.lock"
LOCK_PATH_OPERATOR: Final[str] = "/run/lock/ubuntu_tank/operator.lock"
LOCK_PATH_LIFECYCLE: Final[str] = "/run/lock/ubuntu_tank/lifecycle.lock"
LOCK_LEVEL_DEPLOYMENT: Final[int] = 1
LOCK_LEVEL_OPERATOR: Final[int] = 2
LOCK_LEVEL_LIFECYCLE: Final[int] = 3

# IPC socket paths and constraints
DEFAULT_OPERATOR_SOCKET_PATH: Final[str] = "/run/ubuntu_tank/operator.sock"
ENV_OPERATOR_SOCKET_PATH: Final[str] = "UBUNTU_TANK_OPERATOR_SOCKET"
ENV_OPERATOR_LOCK_PATH: Final[str] = "UBUNTU_TANK_OPERATOR_LOCK"
MAX_IPC_MESSAGE_BYTES: Final[int] = 65536
