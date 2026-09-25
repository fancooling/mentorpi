"""
Pydantic models for MentorPi Pi 5 Web Control API.

Provides strict request and response schemas matching docs/MENTORPI_WEB_CONTROL_DESIGN.md
and ubuntu_tank/docs/openapi_v1.json.
Enforces rejection of unexpected extra fields, bounded numbers, exact boolean types,
and valid enumerated actions and directions.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

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
    disarm_pending: bool = False
    battery_voltage: float | None = None
    linear_speed: float = 0.0
    angular_speed: float = 0.0
    limits: dict[str, float] = Field(default_factory=dict)
    freshness: dict[str, float | None] = Field(default_factory=dict)
    last_fault: str | None = None
    release_id: str = Field(default="unknown")
    protocol_version: str = Field(default=PROTOCOL_VERSION)


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

    operation_id: str
    status: Literal["pending", "completed", "failed"]
    error: str | None = None


class ControlAcquireRequestModel(BaseStrictModel):
    """Request to acquire exclusive operator control authority."""

    request_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    max_linear_speed: float | None = Field(default=None, gt=0.0)
    max_angular_speed: float | None = Field(default=None, gt=0.0)


class ControlAcquireResponseModel(BaseStrictModel):
    """Response to an operator control authority acquisition attempt."""

    success: bool
    epoch: int | None = None
    bind_token: str | None = None
    active_owner: str | None = None
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlReleaseRequestModel(BaseStrictModel):
    """Request to relinquish operator control authority and disarm."""

    request_id: str = Field(..., min_length=1)
    epoch: int = Field(..., ge=1)
    operator_id: str | None = None


class ControlReleaseResponseModel(BaseStrictModel):
    """Response to an operator release request."""

    success: bool
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlArmRequestModel(BaseStrictModel):
    """Request to arm the robot controller chassis."""

    request_id: str = Field(..., min_length=1)
    epoch: int = Field(..., ge=1)
    tracks_raised: StrictBool = Field(...)

    @field_validator("tracks_raised", mode="before")
    @classmethod
    def require_exact_bool_true(cls, v: Any) -> bool:
        """Enforce exact boolean True; reject truthy strings, integers, or False."""
        if v is not True or type(v) is not bool:
            raise ValueError(
                "tracks_raised must be exact boolean True (cannot be string, number, or False)"
            )
        return True


class ControlArmResponseModel(BaseStrictModel):
    """Response to a chassis arming request."""

    success: bool
    error: WebControlErrorCode | None = None
    message: str | None = None


class ControlStopRequestModel(BaseStrictModel):
    """Stop request independent of control ownership."""

    request_id: str = Field(..., min_length=1)
    epoch: int | None = Field(default=None, ge=0)


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
