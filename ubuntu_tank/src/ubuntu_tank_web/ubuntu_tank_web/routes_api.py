"""
FastAPI REST API routes for MentorPi Pi 5 Web Control.

Implements all endpoints defined in docs/MENTORPI_WEB_CONTROL_DESIGN.md:
  GET  /api/v1/version
  GET  /api/v1/status
  GET  /api/v1/logs
  POST /api/v1/control/acquire
  POST /api/v1/control/release
  POST /api/v1/control/start
  POST /api/v1/control/stop
  GET  /api/v1/operations/{id}
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from ubuntu_tank_protocol.constants import (
    API_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)

from .models import (
    ControlAcquireRequestModel,
    ControlAcquireResponseModel,
    ControlReleaseRequestModel,
    ControlReleaseResponseModel,
    ControlStartRequestModel,
    ControlStartResponseModel,
    ControlStopRequestModel,
    ControlStopResponseModel,
    LogsResponseModel,
    OperationStatusResponseModel,
    StatusResponseModel,
    VersionResponseModel,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["control"])

# Bounded in-memory store for recent operation outcomes
MAX_RETAINED_OPERATIONS = 100
_operations: collections.OrderedDict[str, OperationStatusResponseModel] = (
    collections.OrderedDict()
)


def _store_operation(op: OperationStatusResponseModel) -> None:
    """Store an operation status, keeping max MAX_RETAINED_OPERATIONS entries."""
    if len(_operations) >= MAX_RETAINED_OPERATIONS:
        _operations.popitem(last=False)
    _operations[op.operation_id] = op


@router.get(
    "/version",
    response_model=VersionResponseModel,
    summary="Compatibility and release identity",
)
async def get_version(request: Request) -> VersionResponseModel:
    """Network-only protocol compatibility and release metadata."""
    relay = request.app.state.operator_relay
    ver_dict = await asyncio.to_thread(relay.get_version)
    return VersionResponseModel(
        protocol_version=ver_dict.get("protocol_version", PROTOCOL_VERSION),
        api_version=ver_dict.get("api_version", API_VERSION),
        schema_version=ver_dict.get("schema_version", SCHEMA_VERSION),
        release_id=ver_dict.get("release_id", "unknown"),
        supported_protocols=ver_dict.get(
            "supported_protocols", list(SUPPORTED_PROTOCOL_VERSIONS)
        ),
    )


@router.get(
    "/status",
    response_model=StatusResponseModel,
    summary="Live controller and operator status",
)
async def get_status(request: Request) -> StatusResponseModel:
    """Query live controller, safety, battery, and operator status."""
    relay = request.app.state.operator_relay
    lifecycle = request.app.state.lifecycle_client
    st = await asyncio.to_thread(relay.get_status)

    # systemd state belongs to the lifecycle helper. The operator agent can
    # remain available while the controller is intentionally stopped, so its
    # default status must not override this observation.
    lifecycle_ok, service_state, _ = await asyncio.to_thread(lifecycle.get_status)
    if not lifecycle_ok or service_state not in ("active", "inactive", "failed"):
        service_state = "unknown"

    return StatusResponseModel(
        service_state=service_state,
        operator_state=st.get("operator_state", "FAULT"),
        active_owner=st.get("active_owner"),
        current_epoch=st.get("current_epoch"),
        guard_armed=st.get("guard_armed"),
        input_generation=st.get("input_generation", 0),
        pause_reason=st.get("pause_reason"),
        recovery_ready=st.get("recovery_ready", False),
        disarm_pending=st.get("disarm_pending", False),
        battery_voltage=st.get("battery_voltage"),
        linear_speed=st.get("linear_speed", 0.0),
        angular_speed=st.get("angular_speed", 0.0),
        limits=st.get("limits", {}),
        freshness=st.get("freshness", {}),
        last_fault=st.get("last_fault"),
        release_id=st.get("release_id", "unknown"),
        protocol_version=st.get("protocol_version", PROTOCOL_VERSION),
        status_revision=st.get("status_revision", 0),
        session_id=st.get("session_id"),
        control_idle_timeout_sec=st.get("control_idle_timeout_sec"),
        remaining_inactivity_sec=st.get("remaining_inactivity_sec"),
        release_progress=st.get("release_progress"),
        last_release_reason=st.get("last_release_reason"),
        last_released_session_id=st.get("last_released_session_id"),
    )


@router.get(
    "/logs",
    response_model=LogsResponseModel,
    summary="Recent bounded system logs",
)
async def get_logs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LogsResponseModel:
    """Retrieve recent bounded journal logs from the lifecycle helper."""
    lifecycle = request.app.state.lifecycle_client
    success, lines, total_lines, total_bytes, _ = await asyncio.to_thread(
        lifecycle.get_logs, limit=limit
    )
    if not success and not lines:
        lines = ["Log retrieval unavailable from lifecycle helper"]
        total_lines = len(lines)
        total_bytes = sum(len(line) for line in lines)
    return LogsResponseModel(
        lines=lines,
        total_lines=total_lines,
        total_bytes=total_bytes,
    )


@router.post(
    "/control/acquire",
    response_model=ControlAcquireResponseModel,
    summary="Acquire exclusive operator control authority",
)
async def control_acquire(
    req: ControlAcquireRequestModel, request: Request
) -> ControlAcquireResponseModel:
    """Acquire single-operator control authority."""
    relay = request.app.state.operator_relay
    config = request.app.state.config
    max_linear_speed = min(
        req.max_linear_speed
        if req.max_linear_speed is not None
        else config.linear_speed_cap,
        config.linear_speed_cap,
    )
    max_angular_speed = min(
        req.max_angular_speed
        if req.max_angular_speed is not None
        else config.angular_speed_cap,
        config.angular_speed_cap,
    )
    result = await asyncio.to_thread(
        relay.take_control,
        operator_id=req.operator_id,
        request_id=req.request_id,
        max_linear_speed=max_linear_speed,
        max_angular_speed=max_angular_speed,
    )
    return ControlAcquireResponseModel(**result)


@router.post(
    "/control/release",
    response_model=ControlReleaseResponseModel,
    summary="Relinquish operator control authority and shut down controller",
)
async def control_release(
    req: ControlReleaseRequestModel, request: Request
) -> ControlReleaseResponseModel:
    """Relinquish operator control authority, zero motion, shut down controller, and release."""
    relay = request.app.state.operator_relay
    result = await asyncio.to_thread(
        relay.release,
        request_id=req.request_id,
        epoch=req.epoch,
        operation_id=req.operation_id,
        operation_token=req.operation_token,
        operator_id=req.operator_id,
    )
    return ControlReleaseResponseModel(
        status=result.get("status", "completed" if result.get("success") else "failed"),
        operation_id=result.get("operation_id"),
        operation_token=result.get("operation_token") or req.operation_token,
        success=result.get("success", False),
        error=result.get("error"),
        message=result.get("message"),
    )


@router.post(
    "/control/start",
    response_model=ControlStartResponseModel,
    summary="Explicitly arm the chassis controller under owner authority",
)
async def control_start(
    req: ControlStartRequestModel, request: Request
) -> ControlStartResponseModel:
    """Start/arm the chassis controller; requires explicit ownership and healthy preflight."""
    relay = request.app.state.operator_relay
    success, err, msg = await asyncio.to_thread(
        relay.start,
        epoch=req.epoch,
        request_id=req.request_id,
        operator_id=req.operator_id,
    )
    return ControlStartResponseModel(
        success=success,
        error=err,
        message=msg,
    )


@router.post(
    "/control/stop",
    response_model=ControlStopResponseModel,
    summary="Stop and disarm immediately",
)
async def control_stop(
    req: ControlStopRequestModel, request: Request
) -> ControlStopResponseModel:
    """Stop motion and disarm, independent of control ownership."""
    relay = request.app.state.operator_relay
    lifecycle = request.app.state.lifecycle_client
    relay_ok, _ = await asyncio.to_thread(
        relay.stop,
        request_id=req.request_id,
        epoch=req.epoch,
        operator_id=req.operator_id,
    )
    disarmed = False
    if relay_ok:
        success = True
        disarmed = True
    else:
        lc_success, lc_state, _ = await asyncio.to_thread(lifecycle.stop_controller)
        success = lc_success
        disarmed = bool(lc_success and lc_state == "inactive")

    return ControlStopResponseModel(
        success=success,
        disarmed=disarmed,
    )


@router.get(
    "/operations/{id}",
    response_model=OperationStatusResponseModel,
    summary="Get status of an asynchronous operation",
)
async def get_operation(
    id: str, request: Request, operation_token: str | None = None
) -> OperationStatusResponseModel:
    """Query status of a retained operation by its ID."""
    result = await asyncio.to_thread(
        request.app.state.operator_relay.operation_result, id, operation_token
    )
    if not result.get("success") and result.get("error") == "NOT_OWNER":
        op = _operations.get(id)
        if op is not None:
            return op
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Operation '{id}' not found",
        )
    allowed_fields = {
        "epoch",
        "bind_token",
        "success",
        "message",
        "operation_id",
        "status",
        "error",
    }
    filtered = {k: v for k, v in result.items() if k in allowed_fields}
    return OperationStatusResponseModel(**filtered)
