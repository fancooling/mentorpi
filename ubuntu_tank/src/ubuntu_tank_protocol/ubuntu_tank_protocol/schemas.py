"""
Versioned data schemas and validation models for MentorPi Pi 5 Web Control.

Provides strictly validated data transfer models for HTTP REST endpoints,
WebSocket streaming, and internal IPC without external runtime dependencies.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import (
    API_VERSION,
    BATTERY_FRESHNESS_MAX_AGE_SEC,
    DEFAULT_CONTROL_IDLE_TIMEOUT_SEC,
    GUARD_STATE_FRESHNESS_MAX_AGE_SEC,
    MAX_LOG_LINES_LIMIT,
    MIN_SAFE_BATTERY_VOLTAGE,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from .enums import (
    ControllerServiceState,
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)


def _check_no_extra_fields(
    data: dict[str, Any], allowed_fields: set, model_name: str
) -> None:
    extra = set(data.keys()) - allowed_fields
    if extra:
        raise ValueError(
            f"Validation error in {model_name}: unexpected field(s) {sorted(extra)}"
        )


@dataclass(frozen=True)
class VersionResponse:
    """Protocol and release compatibility response."""

    protocol_version: str = PROTOCOL_VERSION
    api_version: str = API_VERSION
    schema_version: int = SCHEMA_VERSION
    release_id: str = "current"
    supported_protocols: list[str] = field(
        default_factory=lambda: list(SUPPORTED_PROTOCOL_VERSIONS)
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionResponse":
        _check_no_extra_fields(
            data,
            {
                "protocol_version",
                "api_version",
                "schema_version",
                "release_id",
                "supported_protocols",
            },
            "VersionResponse",
        )
        return cls(
            protocol_version=str(data.get("protocol_version", PROTOCOL_VERSION)),
            api_version=str(data.get("api_version", API_VERSION)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_id=str(data.get("release_id", "current")),
            supported_protocols=list(
                data.get("supported_protocols", SUPPORTED_PROTOCOL_VERSIONS)
            ),
        )


@dataclass(frozen=True)
class StatusResponse:
    """Live controller, safety, battery, and operator status."""

    service_state: ControllerServiceState
    operator_state: OperatorState
    active_owner: str | None
    current_epoch: int | None
    guard_armed: bool | None
    battery_voltage: float | None
    linear_speed: float
    angular_speed: float
    limits: dict[str, float]
    freshness: dict[str, float | None]
    last_fault: str | None
    release_id: str
    status_revision: int = 0
    session_id: str | None = None
    control_idle_timeout_sec: float = DEFAULT_CONTROL_IDLE_TIMEOUT_SEC
    remaining_inactivity_sec: float | None = None
    release_progress: str | None = None
    last_release_reason: str | None = None
    last_released_session_id: str | None = None
    input_generation: int = 0
    pause_reason: str | None = None
    recovery_ready: bool = False
    disarm_pending: bool = False
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_state": self.service_state.value,
            "operator_state": self.operator_state.value,
            "active_owner": self.active_owner,
            "current_epoch": self.current_epoch,
            "guard_armed": self.guard_armed,
            "disarm_pending": self.disarm_pending,
            "status_revision": self.status_revision,
            "session_id": self.session_id,
            "control_idle_timeout_sec": self.control_idle_timeout_sec,
            "remaining_inactivity_sec": self.remaining_inactivity_sec,
            "release_progress": self.release_progress,
            "last_release_reason": self.last_release_reason,
            "last_released_session_id": self.last_released_session_id,
            "input_generation": self.input_generation,
            "pause_reason": self.pause_reason,
            "recovery_ready": self.recovery_ready,
            "battery_voltage": self.battery_voltage,
            "linear_speed": self.linear_speed,
            "angular_speed": self.angular_speed,
            "limits": dict(self.limits),
            "freshness": dict(self.freshness),
            "last_fault": self.last_fault,
            "release_id": self.release_id,
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True)
class LogsRequest:
    """Bounded recent log query request."""

    limit: int = 50

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogsRequest":
        _check_no_extra_fields(data, {"limit"}, "LogsRequest")
        limit = data.get("limit", 50)
        if not isinstance(limit, int) or limit < 1 or limit > MAX_LOG_LINES_LIMIT:
            raise ValueError(
                f"Validation error in LogsRequest: limit must be an integer between 1 and {MAX_LOG_LINES_LIMIT}"
            )
        return cls(limit=limit)


@dataclass(frozen=True)
class LogsResponse:
    """Bounded log lines response."""

    lines: list[str]
    total_lines: int
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControllerOperationRequest:
    """Controller systemd service mutation request."""

    request_id: str
    action: str  # "start" or "stop"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControllerOperationRequest":
        _check_no_extra_fields(
            data, {"request_id", "action"}, "ControllerOperationRequest"
        )
        req_id = data.get("request_id")
        action = data.get("action")
        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControllerOperationRequest: request_id must be non-empty string"
            )
        if action not in ("start", "stop"):
            raise ValueError(
                "Validation error in ControllerOperationRequest: action must be 'start' or 'stop'"
            )
        return cls(request_id=req_id, action=action)


@dataclass(frozen=True)
class OperationStatusResponse:
    """Asynchronous or retained operation status."""

    operation_id: str
    status: str  # "pending", "completed", "failed"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlAcquireRequest:
    """Request to acquire single operator authority."""

    request_id: str
    operator_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlAcquireRequest":
        _check_no_extra_fields(
            data, {"request_id", "operator_id"}, "ControlAcquireRequest"
        )
        req_id = data.get("request_id")
        op_id = data.get("operator_id")
        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControlAcquireRequest: request_id must be non-empty string"
            )
        if not isinstance(op_id, str) or not op_id:
            raise ValueError(
                "Validation error in ControlAcquireRequest: operator_id must be non-empty string"
            )
        return cls(request_id=req_id, operator_id=op_id)


@dataclass(frozen=True)
class ControlAcquireResponse:
    """Result of control acquisition attempt."""

    success: bool
    epoch: int | None = None
    error: WebControlErrorCode | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "epoch": self.epoch,
            "error": self.error.value if self.error else None,
            "message": self.message,
        }


@dataclass(frozen=True)
class ControlReleaseRequest:
    """Request to relinquish operator authority."""

    request_id: str
    protocol_version: str = PROTOCOL_VERSION
    epoch: int | None = None
    operation_id: str | None = None
    operation_token: str | None = None
    operator_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlReleaseRequest":
        _check_no_extra_fields(
            data,
            {
                "request_id",
                "epoch",
                "operation_id",
                "operation_token",
                "operator_id",
                "protocol_version",
            },
            "ControlReleaseRequest",
        )
        req_id = data.get("request_id")
        epoch = data.get("epoch")
        proto = data.get("protocol_version", PROTOCOL_VERSION)
        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControlReleaseRequest: request_id must be non-empty string"
            )
        if epoch is not None and (
            not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1
        ):
            raise ValueError(
                "Validation error in ControlReleaseRequest: epoch must be positive integer"
            )
        if proto != PROTOCOL_VERSION:
            raise ValueError(
                f"Validation error in ControlReleaseRequest: protocol_version must be {PROTOCOL_VERSION}"
            )
        return cls(
            request_id=req_id,
            protocol_version=proto,
            epoch=epoch,
            operation_id=data.get("operation_id"),
            operation_token=data.get("operation_token"),
            operator_id=data.get("operator_id"),
        )


@dataclass(frozen=True)
class ControlReleaseResponse:
    """Control release result."""

    success: bool
    status: str = "completed"
    operation_id: str | None = None
    operation_token: str | None = None
    error: WebControlErrorCode | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "operation_id": self.operation_id,
            "operation_token": self.operation_token,
            "error": self.error.value if self.error else None,
            "message": self.message,
        }


@dataclass(frozen=True)
class ControlStartRequest:
    """Request to explicitly arm the robot controller chassis."""

    request_id: str
    epoch: int
    protocol_version: str = PROTOCOL_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlStartRequest":
        _check_no_extra_fields(
            data, {"request_id", "epoch", "protocol_version"}, "ControlStartRequest"
        )
        req_id = data.get("request_id")
        epoch = data.get("epoch")
        proto = data.get("protocol_version", PROTOCOL_VERSION)
        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControlStartRequest: request_id must be non-empty string"
            )
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError(
                "Validation error in ControlStartRequest: epoch must be positive integer"
            )
        if proto != PROTOCOL_VERSION:
            raise ValueError(
                f"Validation error in ControlStartRequest: protocol_version must be {PROTOCOL_VERSION}"
            )
        return cls(request_id=req_id, epoch=epoch, protocol_version=proto)


@dataclass(frozen=True)
class ControlStartResponse:
    """Control start/arming confirmation."""

    success: bool
    status: str = "completed"
    error: WebControlErrorCode | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "error": self.error.value if self.error else None,
            "message": self.message,
        }


@dataclass(frozen=True)
class ControlArmRequest:
    """Arming request requiring explicit ownership and a current epoch."""

    request_id: str
    epoch: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlArmRequest":
        _check_no_extra_fields(data, {"request_id", "epoch"}, "ControlArmRequest")
        req_id = data.get("request_id")
        epoch = data.get("epoch")

        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControlArmRequest: request_id must be non-empty string"
            )
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError(
                "Validation error in ControlArmRequest: epoch must be positive integer"
            )
        return cls(request_id=req_id, epoch=epoch)


@dataclass(frozen=True)
class ControlArmResponse:
    """Arming transaction confirmation."""

    success: bool
    error: WebControlErrorCode | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "error": self.error.value if self.error else None,
            "message": self.message,
        }


@dataclass(frozen=True)
class ControlStopRequest:
    """Immediate stop and disarm request, independent of ownership."""

    request_id: str
    epoch: int | None = None
    operator_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlStopRequest":
        _check_no_extra_fields(
            data, {"request_id", "epoch", "operator_id"}, "ControlStopRequest"
        )
        req_id = data.get("request_id")
        if not isinstance(req_id, str) or not req_id:
            raise ValueError(
                "Validation error in ControlStopRequest: request_id must be non-empty string"
            )
        epoch = data.get("epoch")
        if epoch is not None and (
            not isinstance(epoch, int) or isinstance(epoch, bool)
        ):
            raise ValueError(
                "Validation error in ControlStopRequest: epoch must be integer if provided"
            )
        operator_id = data.get("operator_id")
        if operator_id is not None and (
            not isinstance(operator_id, str) or not operator_id
        ):
            raise ValueError("operator_id must be non-empty when provided")
        return cls(request_id=req_id, epoch=epoch, operator_id=operator_id)


@dataclass(frozen=True)
class ControlStopResponse:
    """Immediate stop result."""

    success: bool
    disarmed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Challenge:
    """Cryptographic single-use challenge issued by operator agent."""

    token: str
    epoch: int
    deadline_monotonic_ns: int
    issued_monotonic_ns: int
    input_generation: int = 0
    recovery_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChallengeResponse:
    """Client response to single-use challenge carrying motion intent."""

    token: str
    epoch: int
    sequence: int
    direction: MotionDirection
    input_generation: int = 0
    client_timestamp_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_generation": self.input_generation,
            "token": self.token,
            "epoch": self.epoch,
            "sequence": self.sequence,
            "direction": self.direction.value,
            "client_timestamp_ms": self.client_timestamp_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChallengeResponse":
        _check_no_extra_fields(
            data,
            {
                "token",
                "epoch",
                "sequence",
                "direction",
                "client_timestamp_ms",
                "input_generation",
            },
            "ChallengeResponse",
        )
        generation = data.get("input_generation")
        if type(generation) is not int or generation < 0:
            raise ValueError("input_generation must be a nonnegative integer")
        token = data.get("token")
        epoch = data.get("epoch")
        sequence = data.get("sequence")
        raw_dir = data.get("direction")

        if not isinstance(token, str) or not token:
            raise ValueError("ChallengeResponse token must be non-empty string")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError("ChallengeResponse epoch must be positive integer")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise TypeError("ChallengeResponse sequence must be integer")

        try:
            direction = MotionDirection(raw_dir)
        except (ValueError, KeyError):
            raise ValueError(f"ChallengeResponse invalid direction: '{raw_dir}'")

        ts = data.get("client_timestamp_ms")
        if ts is not None and (not isinstance(ts, int) or isinstance(ts, bool)):
            raise TypeError(
                "ChallengeResponse client_timestamp_ms must be integer if provided"
            )

        return cls(
            input_generation=generation,
            token=token,
            epoch=epoch,
            sequence=sequence,
            direction=direction,
            client_timestamp_ms=ts,
        )


@dataclass
class TelemetrySnapshot:
    """Local observation of robot sensors and safety state."""

    battery_voltage: float | None = None
    battery_monotonic_ns: int | None = None
    guard_armed: bool | None = None
    guard_monotonic_ns: int | None = None
    odom_linear_x: float | None = None
    odom_angular_z: float | None = None
    odom_monotonic_ns: int | None = None
    delivery_monotonic_ns: int | None = None

    def is_healthy(
        self, current_monotonic_ns: int
    ) -> tuple[bool, WebControlErrorCode | None, str | None]:
        """Determine if telemetry meets freshness and safety thresholds."""
        # 1. Battery voltage and freshness check
        if (
            self.battery_voltage is None
            or self.battery_monotonic_ns is None
            or (current_monotonic_ns - self.battery_monotonic_ns) / 1e9
            > BATTERY_FRESHNESS_MAX_AGE_SEC
        ):
            return (
                False,
                WebControlErrorCode.STALE_TELEMETRY,
                "Battery telemetry missing or stale (> 3.0 s)",
            )
        if self.battery_voltage < MIN_SAFE_BATTERY_VOLTAGE:
            return (
                False,
                WebControlErrorCode.PREFLIGHT_FAILED,
                (
                    f"Battery voltage {self.battery_voltage:.2f} V below safety threshold"
                    f" ({MIN_SAFE_BATTERY_VOLTAGE:.2f} V)"
                ),
            )

        # 2. Guard state freshness check
        if (
            self.guard_armed is None
            or self.guard_monotonic_ns is None
            or (current_monotonic_ns - self.guard_monotonic_ns) / 1e9
            > GUARD_STATE_FRESHNESS_MAX_AGE_SEC
        ):
            return (
                False,
                WebControlErrorCode.STALE_TELEMETRY,
                "Motor guard state telemetry missing or stale (> 0.5 s)",
            )

        return True, None, None

    def get_freshness_dict(self, current_monotonic_ns: int) -> dict[str, float | None]:
        def calc_age(t_ns: int | None) -> float | None:
            if t_ns is None:
                return None
            return max(0.0, (current_monotonic_ns - t_ns) / 1e9)

        return {
            "battery_age_sec": calc_age(self.battery_monotonic_ns),
            "guard_age_sec": calc_age(self.guard_monotonic_ns),
            "odom_age_sec": calc_age(self.odom_monotonic_ns),
            "delivery_age_sec": calc_age(self.delivery_monotonic_ns),
        }


@dataclass(frozen=True)
class WsClientFrame:
    """Standard envelope for WebSocket client frames."""

    action: str  # "bind", "intent", "stop", "heartbeat"
    payload: dict[str, Any]

    @classmethod
    def from_json(cls, text: str) -> "WsClientFrame":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise ValueError(f"Invalid JSON in WebSocket frame: {e}") from e
        if not isinstance(data, dict):
            raise TypeError("WebSocket frame must be a JSON object")
        _check_no_extra_fields(data, {"action", "payload"}, "WsClientFrame")
        action = data.get("action")
        payload = data.get("payload")
        if not isinstance(action, str) or not action:
            raise ValueError("WsClientFrame action must be a non-empty string")
        if not isinstance(payload, dict):
            raise TypeError("WsClientFrame payload must be a JSON object")
        return cls(action=action, payload=payload)

    def to_json(self) -> str:
        return json.dumps({"action": self.action, "payload": self.payload})


@dataclass(frozen=True)
class WsServerFrame:
    """Standard envelope for WebSocket server frames."""

    type: str  # "challenge", "state", "ack", "error"
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "payload": self.payload})

    @classmethod
    def from_json(cls, text: str) -> "WsServerFrame":
        data = json.loads(text)
        return cls(type=data["type"], payload=data["payload"])
