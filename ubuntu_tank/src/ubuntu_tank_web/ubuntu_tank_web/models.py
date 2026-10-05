"""
Pydantic models for MentorPi Pi 5 Web Control API.

Provides strict request and response schemas matching docs/MENTORPI_WEB_CONTROL_DESIGN.md
and ubuntu_tank/docs/openapi_v1.json.
Enforces rejection of unexpected extra fields, bounded numbers, exact boolean types,
and valid enumerated actions and directions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from ubuntu_tank_protocol.constants import (
    API_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from ubuntu_tank_protocol.enums import (
    ControllerServiceState,
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)


class BaseStrictModel(BaseModel):
    """Base model forbidding unexpected extra fields."""

    model_config = ConfigDict(extra="forbid")


class VersionResponseModel(BaseStrictModel):
    """Protocol and release version metadata."""

    protocol_version: str = Field(default=PROTOCOL_VERSION)
    api_version: str = Field(default=API_VERSION)
    schema_version: int = Field(default=SCHEMA_VERSION)
    release_id: str = Field(default="unknown")
    supported_protocols: list[str] = Field(
        default_factory=lambda: list(SUPPORTED_PROTOCOL_VERSIONS)
    )


class StatusResponseModel(BaseStrictModel):
    """Live controller, operator, safety, and robot telemetry status."""

    service_state: ControllerServiceState
    operator_state: OperatorState
    active_owner: str | None = None
    current_epoch: int | None = None
    guard_armed: bool | None = None
    input_generation: int = 0
    pause_reason: str | None = None
    recovery_ready: bool = False
    disarm_pending: bool = False
    battery_voltage: float | None = None
    linear_speed: float = 0.0
    angular_speed: float = 0.0
    limits: dict[str, float] = Field(default_factory=dict)
    freshness: dict[str, float | None] = Field(default_factory=dict)
    last_fault: str | None = None
    release_id: str = Field(default="unknown")
    protocol_version: str = Field(default=PROTOCOL_VERSION)
    status_revision: int = 0
    session_id: str | None = None
    control_idle_timeout_sec: float | None = None
    remaining_inactivity_sec: float | None = None
    release_progress: str | None = None
    last_release_reason: str | None = None
    last_released_session_id: str | None = None


class LogsRequestModel(BaseStrictModel):
    """Query parameters for recent log retrieval."""

    limit: int = Field(default=50, ge=1, le=200)


class LogsResponseModel(BaseStrictModel):
    """Recent bounded log lines."""

    lines: list[str] = Field(default_factory=list)
    total_lines: int = 0
    total_bytes: int = 0


class ControllerOperationRequestModel(BaseStrictModel):
    """Request to start or stop the native controller systemd service."""

    request_id: str = Field(..., min_length=1)
    action: Literal["start", "stop"] | None = None


class OperationStatusResponseModel(BaseStrictModel):
    """Status of an asynchronous controller or lifecycle operation."""

    epoch: int | None = None
    bind_token: str | None = None
    success: bool = True
    message: str | None = None
    operation_id: str
    status: Literal["pending", "completed", "failed"]
    error: str | None = None


class ControlAcquireRequestModel(BaseStrictModel):
    """Request to acquire exclusive operator control authority."""

    protocol_version: Literal["3.0.0"]
    request_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    max_linear_speed: float | None = Field(default=None, gt=0.0)
    max_angular_speed: float | None = Field(default=None, gt=0.0)


class ControlAcquireResponseModel(BaseStrictModel):
    """Response to an operator control authority acquisition attempt."""

    operation_id: str | None = None
    operation_token: str | None = None
    status: Literal["pending", "completed", "failed"] = "failed"
    success: bool
    epoch: int | None = None
    bind_token: str | None = None
    active_owner: str | None = None
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlReleaseRequestModel(BaseStrictModel):
    """Request to relinquish operator control authority and disarm."""

    protocol_version: Literal["3.0.0"]
    request_id: str = Field(..., min_length=1)
    epoch: StrictInt | None = Field(default=None, ge=1)
    operation_id: str | None = None
    operation_token: str | None = None
    operator_id: str | None = None


class ControlReleaseResponseModel(BaseStrictModel):
    """Response to an operator release request."""

    status: Literal["pending", "completed", "failed"] = "completed"
    operation_id: str | None = None
    operation_token: str | None = None
    success: bool
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlStartRequestModel(BaseStrictModel):
    """Request to explicitly arm the chassis controller under owner authority."""

    protocol_version: Literal["3.0.0"]
    epoch: StrictInt = Field(..., ge=1)
    request_id: str = Field(..., min_length=1)
    operator_id: str | None = None


class ControlStartResponseModel(BaseStrictModel):
    """Response to a chassis arming/starting request."""

    success: bool
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlStopRequestModel(BaseStrictModel):
    """Always stop; optional owner identity attributes inactivity activity only."""

    request_id: str = Field(..., min_length=1)
    epoch: int | None = Field(default=None, ge=0)
    operator_id: str | None = Field(default=None, min_length=1)


class ControlStopResponseModel(BaseStrictModel):
    """Response confirming immediate stop request dispatch."""

    success: bool
    disarmed: bool = False


# WebSocket framing models


class WsClientIntentPayload(BaseStrictModel):
    """Payload for client motion intent in response to an active challenge."""

    token: str = Field(..., min_length=1)
    epoch: int = Field(..., ge=1)
    sequence: int = Field(..., ge=1)
    direction: MotionDirection
    client_timestamp_ms: int | None = Field(default=None, ge=0)


class WsClientBindPayload(BaseStrictModel):
    """Payload to bind a WebSocket connection to an active operator session."""

    operator_id: str = Field(..., min_length=1)
    epoch: int = Field(..., ge=1)
    bind_token: str = Field(..., min_length=1)


class WsClientStopPayload(BaseStrictModel):
    """Payload for immediate stop via WebSocket."""

    request_id: str = Field(..., min_length=1)
    epoch: int | None = Field(default=None, ge=0)


class WsClientMessageModel(BaseStrictModel):
    """Inbound message from browser client over WebSocket."""

    action: Literal["bind", "intent", "stop", "heartbeat"]
    payload: dict[str, Any] = Field(default_factory=dict)


class WsServerMessageModel(BaseStrictModel):
    """Outbound message from server to browser client over WebSocket."""

    type: Literal["challenge", "state", "ack", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)


class CameraProfileModel(BaseStrictModel):
    """Camera resolution and frame rate profile."""

    width: int = Field(default=640, ge=1)
    height: int = Field(default=480, ge=1)
    fps: int = Field(default=15, ge=1)


class CameraStatusResponseModel(BaseStrictModel):
    """Live camera status, frame age, and recording/storage availability."""

    state: Literal["live", "connecting", "stale", "unavailable", "finalizing", "error"]
    frame_age_sec: float | None = None
    profile: CameraProfileModel = Field(default_factory=CameraProfileModel)
    recording_state: str = "disabled"
    recording_id: str | None = None
    elapsed_sec: float | None = None
    storage_available_bytes: int | None = None
    viewers_count: int = 0
    last_error: str | None = None


class CameraCaptureRequestModel(BaseStrictModel):
    """Optional parameters for camera capture request."""

    request_id: str | None = None
    idempotency_key: str | None = None


class CameraCaptureResponseModel(BaseStrictModel):
    """Metadata response for a captured image."""

    media_id: str
    filename: str
    timestamp: float
    url: str
    width: int
    height: int
    bytes: int


class CameraMediaItemModel(BaseStrictModel):
    """Metadata for a saved media file."""

    media_id: str
    type: Literal["image", "video"] = "image"
    filename: str
    timestamp: float
    url: str
    width: int
    height: int
    bytes: int
    completed: bool = True


class CameraMediaListResponseModel(BaseStrictModel):
    """Paginated list of saved media files."""

    items: list[CameraMediaItemModel] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class CameraRecordingResponseModel(BaseStrictModel):
    """Metadata response for video recording start/stop."""

    recording_id: str
    state: str
    elapsed_sec: float = 0.0
    url: str | None = None
