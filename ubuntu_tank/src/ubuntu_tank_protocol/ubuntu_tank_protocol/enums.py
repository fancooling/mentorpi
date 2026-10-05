"""
Enumerations for MentorPi Pi 5 Web Control.

Defines operator states, motion directions, error codes, and controller service states
according to docs/MENTORPI_WEB_CONTROL_DESIGN.md.
"""

from enum import Enum


class OperatorState(str, Enum):
    """Application operator control states."""

    NO_OWNER = "NO_OWNER"
    OWNED_DISARMED = "OWNED_DISARMED"
    ARMING = "ARMING"
    ARMED_IDLE = "ARMED_IDLE"
    DRIVING = "DRIVING"
    INPUT_PAUSED = "INPUT_PAUSED"
    FAULT = "FAULT"


class MotionDirection(str, Enum):
    """Permissible discrete motion directions."""

    NEUTRAL = "neutral"
    FORWARD = "forward"
    REVERSE = "reverse"
    SPIN_LEFT = "spin_left"
    SPIN_RIGHT = "spin_right"
    STOP = "stop"


class WebControlErrorCode(str, Enum):
    """Stable error codes returned by the web and operator APIs."""

    NOT_OWNER = "NOT_OWNER"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    CONTROLLER_UNAVAILABLE = "CONTROLLER_UNAVAILABLE"
    DEPLOYMENT_BUSY = "DEPLOYMENT_BUSY"
    INVALID_EPOCH = "INVALID_EPOCH"
    CHALLENGE_REUSED = "CHALLENGE_REUSED"
    CHALLENGE_EXPIRED = "CHALLENGE_EXPIRED"
    SEQUENCE_OUT_OF_ORDER = "SEQUENCE_OUT_OF_ORDER"
    MAX_HOLD_EXCEEDED = "MAX_HOLD_EXCEEDED"
    STALE_TELEMETRY = "STALE_TELEMETRY"
    INVALID_STATE = "INVALID_STATE"
    INPUT_CONFLICT = "INPUT_CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    TRACKS_NOT_RAISED = "TRACKS_NOT_RAISED"
    OPERATION_FAILED = "OPERATION_FAILED"
    TIMEOUT = "TIMEOUT"
    STALE_TRANSACTION = "STALE_TRANSACTION"
    INCOMPATIBLE_PROTOCOL = "INCOMPATIBLE_PROTOCOL"


class ControllerServiceState(str, Enum):
    """systemd controller service observation states."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReleaseReason(str, Enum):
    """Reasons recorded when operator authority is released."""

    EXPLICIT_RELEASE = "EXPLICIT_RELEASE"
    CONTROL_IDLE_TIMEOUT = "CONTROL_IDLE_TIMEOUT"
    DISCONNECT = "DISCONNECT"
    SHUTDOWN_FAILED = "SHUTDOWN_FAILED"


class CameraState(str, Enum):
    """Camera device and frame acquisition states."""

    LIVE = "live"
    CONNECTING = "connecting"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    FINALIZING = "finalizing"
    ERROR = "error"


class CameraErrorCode(str, Enum):
    """Error codes for camera operations."""

    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
    STALE_FRAMES = "STALE_FRAMES"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    NOT_FOUND = "NOT_FOUND"
    LOW_STORAGE = "LOW_STORAGE"
    STORAGE_QUOTA_EXCEEDED = "STORAGE_QUOTA_EXCEEDED"
    WRITE_FAILED = "WRITE_FAILED"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
