"""
FastAPI REST API routes for MentorPi Pi 5 Web Control.

Implements all endpoints defined in docs/MENTORPI_WEB_CONTROL_DESIGN.md:
  GET  /api/v1/version
  GET  /api/v1/status
  GET  /api/v1/logs
  POST /api/v1/controller/start
  POST /api/v1/controller/stop
  POST /api/v1/control/acquire
  POST /api/v1/control/release
  POST /api/v1/control/arm
  POST /api/v1/control/stop
  GET  /api/v1/operations/{id}
"""

from __future__ import annotations

import asyncio
import collections
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from .models import (
    ControlAcquireRequestModel,
    ControlAcquireResponseModel,
    ControlArmRequestModel,
    ControlArmResponseModel,
    ControllerOperationRequestModel,
    ControlReleaseRequestModel,
    ControlReleaseResponseModel,
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
        protocol_version=ver_dict.get("protocol_version", "1.0.0"),
        api_version=ver_dict.get("api_version", "v1"),
        schema_version=ver_dict.get("schema_version", 1),
        release_id=ver_dict.get("release_id", "unknown"),
        supported_protocols=ver_dict.get("supported_protocols", ["1.0.0"]),
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
        disarm_pending=st.get("disarm_pending", False),
        battery_voltage=st.get("battery_voltage"),
        linear_speed=st.get("linear_speed", 0.0),
        angular_speed=st.get("angular_speed", 0.0),
        limits=st.get("limits", {}),
        freshness=st.get("freshness", {}),
        last_fault=st.get("last_fault"),
        release_id=st.get("release_id", "unknown"),
        protocol_version=st.get("protocol_version", "1.0.0"),
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
    "/controller/start",
    response_model=OperationStatusResponseModel,
    summary="Start the motion controller systemd service",
)
async def controller_start(
    req: ControllerOperationRequestModel, request: Request
) -> OperationStatusResponseModel:
    """Start mentorpi-tank.service via the restricted lifecycle helper."""
    lifecycle = request.app.state.lifecycle_client
    op_id = f"op-{uuid.uuid4().hex[:8]}"
    success, _state, err_msg = await asyncio.to_thread(lifecycle.start_controller)
    op_status = OperationStatusResponseModel(
        operation_id=op_id,
        status="completed" if success else "failed",
        error=err_msg if not success else None,
    )
    _store_operation(op_status)
    return op_status


@router.post(
    "/controller/stop",
    response_model=OperationStatusResponseModel,
    summary="Stop the motion controller systemd service",
)
async def controller_stop(
    req: ControllerOperationRequestModel, request: Request
) -> OperationStatusResponseModel:
    """Invalidate driving and stop mentorpi-tank.service."""
    relay = request.app.state.operator_relay
    lifecycle = request.app.state.lifecycle_client

    # Invalidate driving first
    await asyncio.to_thread(relay.stop, request_id=req.request_id)

    op_id = f"op-{uuid.uuid4().hex[:8]}"
    success, _state, err_msg = await asyncio.to_thread(lifecycle.stop_controller)
    op_status = OperationStatusResponseModel(
        operation_id=op_id,
        status="completed" if success else "failed",
        error=err_msg if not success else None,
    )
    _store_operation(op_status)
    return op_status


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
    success, epoch, bind_token_or_err, msg = await asyncio.to_thread(
        relay.acquire,
        operator_id=req.operator_id,
        request_id=req.request_id,
        max_linear_speed=max_linear_speed,
        max_angular_speed=max_angular_speed,
    )
    return ControlAcquireResponseModel(
        success=success,
        epoch=epoch,
        bind_token=bind_token_or_err if success else None,
        active_owner=req.operator_id if success else None,
        error=None if success else bind_token_or_err,
        message=msg,
    )


@router.post(
    "/control/release",
    response_model=ControlReleaseResponseModel,
    summary="Relinquish operator control authority",
)
async def control_release(
    req: ControlReleaseRequestModel, request: Request
) -> ControlReleaseResponseModel:
    """Relinquish operator control authority and disarm."""
    relay = request.app.state.operator_relay
    success, err, msg = await asyncio.to_thread(
        relay.release,
        epoch=req.epoch,
        request_id=req.request_id,
    )
    return ControlReleaseResponseModel(
        success=success,
        error=err,
        message=msg,
    )


@router.post(
    "/control/arm",
    response_model=ControlArmResponseModel,
    summary="Arm the chassis controller",
)
async def control_arm(
    req: ControlArmRequestModel, request: Request
) -> ControlArmResponseModel:
    """Arm the robot chassis; requires strict tracks_raised: true."""
    relay = request.app.state.operator_relay
    success, err, msg = await asyncio.to_thread(
        relay.arm,
        epoch=req.epoch,
        tracks_raised=req.tracks_raised,
        request_id=req.request_id,
    )
    return ControlArmResponseModel(
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
        relay.stop, request_id=req.request_id, epoch=req.epoch
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
async def get_operation(id: str) -> OperationStatusResponseModel:
    """Query status of a retained operation by its ID."""
    op = _operations.get(id)
    if op is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Operation '{id}' not found",
        )
    return op
