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
