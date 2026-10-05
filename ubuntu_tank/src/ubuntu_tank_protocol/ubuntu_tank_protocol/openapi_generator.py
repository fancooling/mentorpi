"""
OpenAPI 3.0.3 Specification and TypeScript Type Generator for MentorPi Web Control.

Exports the official OpenAPI schema to docs/openapi_v1.json and corresponding
TypeScript type definitions to web/src/types/api.ts.
"""

import json
import os
import sys
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from ubuntu_tank_protocol.constants import (
        API_VERSION,
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
    )
    from ubuntu_tank_protocol.enums import (
        CameraState,
        ControllerServiceState,
        MotionDirection,
        OperatorState,
        WebControlErrorCode,
    )
else:
    from .constants import API_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION
    from .enums import (
        CameraState,
        ControllerServiceState,
        MotionDirection,
        OperatorState,
        WebControlErrorCode,
    )


def generate_openapi_spec() -> dict[str, Any]:
    """Construct complete OpenAPI 3.0.3 specification for Web Control API."""
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "MentorPi Pi 5 Web Control API",
            "version": PROTOCOL_VERSION,
            "description": (
                "Guarded web control, teleoperation, and lifecycle monitoring"
                " interface for MentorPi Tank on native Ubuntu 26.04 / ROS 2"
                " Lyrical."
            ),
        },
        "servers": [{"url": f"/api/{API_VERSION}"}],
        "paths": {
            "/version": {
                "get": {
                    "summary": "Protocol and release compatibility identity",
                    "description": (
                        "Returns API and protocol versions for client"
                        " compatibility checks before acquiring control."
                    ),
                    "responses": {
                        "200": {
                            "description": "Compatibility metadata",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": ("#/components/schemas/VersionResponse")
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/status": {
                "get": {
                    "summary": "Live controller and operator status",
                    "responses": {
                        "200": {
                            "description": "Current system status",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": ("#/components/schemas/StatusResponse")
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/logs": {
                "get": {
                    "summary": "Bounded recent log buffer",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "default": 50,
                                "maximum": 200,
                            },
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Log lines response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": ("#/components/schemas/LogsResponse")
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/control/acquire": {
                "post": {
                    "summary": (
                        "Start if needed and acquire exclusive control asynchronously"
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/ControlAcquireRequest"
                                    )
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Acquisition result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/ControlAcquireResponse"
                                        )
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/control/release": {
                "post": {
                    "summary": (
                        "Relinquish operator authority, stop controller, and release"
                        " ownership"
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/ControlReleaseRequest"
                                    )
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Release result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/ControlReleaseResponse"
                                        )
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/control/start": {
                "post": {
                    "summary": ("Explicitly arm after ownership and healthy preflight"),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ControlStartRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Start result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/ControlStartResponse"
                                        )
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/control/stop": {
                "post": {
                    "summary": (
                        "Immediate safety stop and disarm (independent of ownership)"
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": ("#/components/schemas/ControlStopRequest")
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Stop result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/ControlStopResponse"
                                        )
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/operations/{id}": {
                "get": {
                    "summary": "Query asynchronous operation status",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Operation status",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/OperationStatusResponse"
                                        )
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/camera/status": {
                "get": {
                    "summary": "Live camera acquisition status and profile",
                    "responses": {
                        "200": {
                            "description": "Camera status response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CameraStatusResponse"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/camera/stream": {
                "get": {
                    "summary": "Bounded MJPEG live camera stream",
                    "responses": {
                        "200": {
                            "description": "Multipart MJPEG frame stream",
                            "content": {
                                "multipart/x-mixed-replace; boundary=frame": {
                                    "schema": {"type": "string", "format": "binary"}
                                }
                            },
                        }
                    },
                }
            },
            "/camera/captures": {
                "post": {
                    "summary": "Capture fresh JPEG image frame",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/CameraCaptureRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Captured image metadata",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CameraCaptureResponse"
                                    }
                                }
                            },
                        },
                        "400": {"description": "Invalid payload"},
                        "503": {"description": "Camera unavailable or stale frames"},
                        "507": {
                            "description": "Insufficient storage or quota exceeded"
                        },
                    },
                }
            },
            "/camera/recordings": {
                "post": {
                    "summary": "Start video recording on robot",
                    "responses": {
                        "200": {
                            "description": "Recording started",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CameraRecordingResponse"
                                    }
                                }
                            },
                        },
                        "503": {"description": "Feature disabled in current milestone"},
                    },
                }
            },
            "/camera/recordings/{id}/stop": {
                "post": {
                    "summary": "Stop video recording on robot",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Recording stopped",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CameraRecordingResponse"
                                    }
                                }
                            },
                        },
                        "503": {"description": "Feature disabled in current milestone"},
                    },
                }
            },
            "/camera/media": {
                "get": {
                    "summary": "List saved media files",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "default": 50,
                                "minimum": 1,
                                "maximum": 100,
                            },
                        },
                        {
                            "name": "offset",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "default": 0,
                                "minimum": 0,
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Saved media list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CameraMediaListResponse"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/camera/media/{id}": {
                "get": {
                    "summary": "Download saved media file by ID",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Media file content",
                            "content": {"image/jpeg": {}, "video/mp4": {}},
                        },
                        "404": {"description": "Media item not found"},
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "OperatorState": {
                    "type": "string",
                    "enum": [s.value for s in OperatorState],
                },
                "MotionDirection": {
                    "type": "string",
                    "enum": [d.value for d in MotionDirection],
                },
                "WebControlErrorCode": {
                    "type": "string",
                    "enum": [e.value for e in WebControlErrorCode],
                },
                "ControllerServiceState": {
                    "type": "string",
                    "enum": [s.value for s in ControllerServiceState],
                },
                "VersionResponse": {
                    "type": "object",
                    "required": [
                        "protocol_version",
                        "api_version",
                        "schema_version",
                        "release_id",
                        "supported_protocols",
                    ],
                    "properties": {
                        "protocol_version": {"type": "string"},
                        "api_version": {"type": "string"},
                        "schema_version": {"type": "integer"},
                        "release_id": {"type": "string"},
                        "supported_protocols": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "StatusResponse": {
                    "type": "object",
                    "required": [
                        "service_state",
                        "operator_state",
                        "guard_armed",
                        "disarm_pending",
                        "linear_speed",
                        "angular_speed",
                        "limits",
                        "freshness",
                        "release_id",
                        "protocol_version",
                        "status_revision",
                    ],
                    "properties": {
                        "service_state": {
                            "$ref": ("#/components/schemas/ControllerServiceState")
                        },
                        "operator_state": {
                            "$ref": "#/components/schemas/OperatorState"
                        },
                        "active_owner": {"type": "string", "nullable": True},
                        "current_epoch": {"type": "integer", "nullable": True},
                        "guard_armed": {"type": "boolean", "nullable": True},
                        "disarm_pending": {"type": "boolean"},
                        "input_generation": {"type": "integer"},
                        "pause_reason": {"type": "string", "nullable": True},
                        "recovery_ready": {"type": "boolean"},
                        "battery_voltage": {"type": "number", "nullable": True},
                        "linear_speed": {"type": "number"},
                        "angular_speed": {"type": "number"},
                        "limits": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                        "freshness": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "number",
                                "nullable": True,
                            },
                        },
                        "last_fault": {"type": "string", "nullable": True},
                        "release_id": {"type": "string"},
                        "protocol_version": {"type": "string"},
                        "status_revision": {"type": "integer"},
                        "session_id": {"type": "string", "nullable": True},
                        "control_idle_timeout_sec": {
                            "type": "number",
                            "nullable": True,
                        },
                        "remaining_inactivity_sec": {
                            "type": "number",
                            "nullable": True,
                        },
                        "release_progress": {"type": "string", "nullable": True},
                        "last_release_reason": {"type": "string", "nullable": True},
                        "last_released_session_id": {
                            "type": "string",
                            "nullable": True,
                        },
                    },
                },
                "LogsResponse": {
                    "type": "object",
                    "required": ["lines", "total_lines", "total_bytes"],
                    "properties": {
                        "lines": {"type": "array", "items": {"type": "string"}},
                        "total_lines": {"type": "integer"},
                        "total_bytes": {"type": "integer"},
                    },
                },
                "OperationStatusResponse": {
                    "type": "object",
                    "required": ["operation_id", "status"],
                    "properties": {
                        "epoch": {"type": "integer", "nullable": True},
                        "bind_token": {"type": "string", "nullable": True},
                        "success": {"type": "boolean"},
                        "message": {"type": "string", "nullable": True},
                        "operation_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed", "failed"],
                        },
                        "error": {"type": "string", "nullable": True},
                    },
                },
                "ControlAcquireRequest": {
                    "type": "object",
                    "required": ["protocol_version", "request_id", "operator_id"],
                    "properties": {
                        "protocol_version": {
                            "type": "string",
                            "enum": [PROTOCOL_VERSION],
                        },
                        "request_id": {"type": "string"},
                        "operator_id": {"type": "string"},
                        "max_linear_speed": {"type": "number", "nullable": True},
                        "max_angular_speed": {"type": "number", "nullable": True},
                    },
                },
                "ControlAcquireResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "operation_id": {"type": "string", "nullable": True},
                        "operation_token": {"type": "string", "nullable": True},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed", "failed"],
                        },
                        "success": {"type": "boolean"},
                        "epoch": {"type": "integer", "nullable": True},
                        "bind_token": {"type": "string", "nullable": True},
                        "active_owner": {"type": "string", "nullable": True},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                        "message": {"type": "string", "nullable": True},
                    },
                },
                "ControlReleaseRequest": {
                    "type": "object",
                    "required": ["protocol_version", "request_id"],
                    "properties": {
                        "protocol_version": {
                            "type": "string",
                            "enum": [PROTOCOL_VERSION],
                        },
                        "request_id": {"type": "string"},
                        "epoch": {"type": "integer", "nullable": True},
                        "operation_id": {"type": "string", "nullable": True},
                        "operation_token": {"type": "string", "nullable": True},
                        "operator_id": {"type": "string", "nullable": True},
                    },
                },
                "ControlReleaseResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed", "failed"],
                        },
                        "operation_id": {"type": "string", "nullable": True},
                        "operation_token": {"type": "string", "nullable": True},
                        "success": {"type": "boolean"},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                        "message": {"type": "string", "nullable": True},
                    },
                },
                "ControlStartRequest": {
                    "type": "object",
                    "required": ["protocol_version", "request_id", "epoch"],
                    "properties": {
                        "protocol_version": {
                            "type": "string",
                            "enum": [PROTOCOL_VERSION],
                        },
                        "request_id": {"type": "string"},
                        "epoch": {"type": "integer"},
                        "operator_id": {"type": "string", "nullable": True},
                    },
                },
                "ControlStartResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                        "message": {"type": "string", "nullable": True},
                    },
                },
                "ControlStopRequest": {
                    "type": "object",
                    "required": ["request_id"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "epoch": {"type": "integer", "nullable": True},
                        "operator_id": {"type": "string", "nullable": True},
                    },
                },
                "ControlStopResponse": {
                    "type": "object",
                    "required": ["success", "disarmed"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "disarmed": {"type": "boolean"},
                    },
                },
                "Challenge": {
                    "type": "object",
                    "required": [
                        "input_generation",
                        "token",
                        "epoch",
                        "deadline_monotonic_ns",
                        "issued_monotonic_ns",
                    ],
                    "properties": {
                        "input_generation": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "recovery_required": {"type": "boolean"},
                        "token": {"type": "string"},
                        "epoch": {"type": "integer"},
                        "deadline_monotonic_ns": {"type": "integer"},
                        "issued_monotonic_ns": {"type": "integer"},
                    },
                },
                "ChallengeResponse": {
                    "type": "object",
                    "required": [
                        "input_generation",
                        "token",
                        "epoch",
                        "sequence",
                        "direction",
                    ],
                    "properties": {
                        "input_generation": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "token": {"type": "string"},
                        "epoch": {"type": "integer"},
                        "sequence": {"type": "integer"},
                        "direction": {"$ref": "#/components/schemas/MotionDirection"},
                        "client_timestamp_ms": {
                            "type": "integer",
                            "nullable": True,
                        },
                    },
                },
                "CameraState": {
                    "type": "string",
                    "enum": [s.value for s in CameraState],
                },
                "CameraProfile": {
                    "type": "object",
                    "required": ["width", "height", "fps"],
                    "properties": {
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "fps": {"type": "integer"},
                    },
                },
                "CameraStatusResponse": {
                    "type": "object",
                    "required": [
                        "state",
                        "profile",
                        "recording_state",
                        "viewers_count",
                    ],
                    "properties": {
                        "state": {"$ref": "#/components/schemas/CameraState"},
                        "frame_age_sec": {"type": "number", "nullable": True},
                        "profile": {"$ref": "#/components/schemas/CameraProfile"},
                        "recording_state": {"type": "string"},
                        "recording_id": {"type": "string", "nullable": True},
                        "elapsed_sec": {"type": "number", "nullable": True},
                        "storage_available_bytes": {
                            "type": "integer",
                            "nullable": True,
                        },
                        "viewers_count": {"type": "integer"},
                        "last_error": {"type": "string", "nullable": True},
                    },
                },
                "CameraCaptureRequest": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "nullable": True},
                        "idempotency_key": {"type": "string", "nullable": True},
                    },
                },
                "CameraCaptureResponse": {
                    "type": "object",
                    "required": [
                        "media_id",
                        "filename",
                        "timestamp",
                        "url",
                        "width",
                        "height",
                        "bytes",
                    ],
                    "properties": {
                        "media_id": {"type": "string"},
                        "filename": {"type": "string"},
                        "timestamp": {"type": "number"},
                        "url": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "bytes": {"type": "integer"},
                    },
                },
                "CameraMediaItem": {
                    "type": "object",
                    "required": [
                        "media_id",
                        "type",
                        "filename",
                        "timestamp",
                        "url",
                        "width",
                        "height",
                        "bytes",
                        "completed",
                    ],
                    "properties": {
                        "media_id": {"type": "string"},
                        "type": {"type": "string"},
                        "filename": {"type": "string"},
                        "timestamp": {"type": "number"},
                        "url": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "bytes": {"type": "integer"},
                        "completed": {"type": "boolean"},
                    },
                },
                "CameraMediaListResponse": {
                    "type": "object",
                    "required": ["items", "total", "limit", "offset"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/CameraMediaItem"},
                        },
                        "total": {"type": "integer"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                },
                "CameraRecordingResponse": {
                    "type": "object",
                    "required": ["recording_id", "state", "elapsed_sec"],
                    "properties": {
                        "recording_id": {"type": "string"},
                        "state": {"type": "string"},
                        "elapsed_sec": {"type": "number"},
                        "url": {"type": "string", "nullable": True},
                    },
                },
            }
        },
    }

    spec["paths"]["/operations/{id}"]["get"].setdefault("parameters", []).append(
        {
            "name": "operation_token",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
        }
    )
    return spec


def generate_typescript_definitions() -> str:
    """Generate strongly typed TypeScript interfaces for the Vue/PWA frontend."""
    return f"""// Auto-generated TypeScript definitions for MentorPi Pi 5 Web Control.
// Protocol Version: {PROTOCOL_VERSION}, API Version: {API_VERSION}, Schema Version: {SCHEMA_VERSION}

export type OperatorState =
  | 'NO_OWNER'
  | 'OWNED_DISARMED'
  | 'ARMING'
  | 'ARMED_IDLE'
  | 'DRIVING'
  | 'INPUT_PAUSED'
  | 'FAULT';

export type MotionDirection =
  | 'neutral'
  | 'forward'
  | 'reverse'
  | 'spin_left'
  | 'spin_right'
  | 'stop';

export type WebControlErrorCode =
  | 'NOT_OWNER'
  | 'LEASE_EXPIRED'
  | 'PREFLIGHT_FAILED'
  | 'CONTROLLER_UNAVAILABLE'
  | 'DEPLOYMENT_BUSY'
  | 'INVALID_EPOCH'
  | 'CHALLENGE_REUSED'
  | 'CHALLENGE_EXPIRED'
  | 'SEQUENCE_OUT_OF_ORDER'
  | 'MAX_HOLD_EXCEEDED'
  | 'STALE_TELEMETRY'
  | 'INVALID_STATE'
  | 'INPUT_CONFLICT'
  | 'UNAUTHORIZED'
  | 'INVALID_PAYLOAD'
  | 'TRACKS_NOT_RAISED'
  | 'OPERATION_FAILED'
  | 'TIMEOUT'
  | 'INCOMPATIBLE_PROTOCOL';

export type ControllerServiceState =
  | 'active'
  | 'inactive'
  | 'failed'
  | 'unknown';

export interface VersionResponse {{
  protocol_version: string;
  api_version: string;
  schema_version: number;
  release_id: string;
  supported_protocols: string[];
}}

export interface StatusResponse {{
  input_generation?: number;
  pause_reason?: string | null;
  recovery_ready?: boolean;
  service_state: ControllerServiceState;
  operator_state: OperatorState;
  active_owner: string | null;
  current_epoch: number | null;
  guard_armed: boolean | null;
  disarm_pending: boolean;
  battery_voltage: number | null;
  linear_speed: number;
  angular_speed: number;
  limits: Record<string, number>;
  freshness: Record<string, number | null>;
  last_fault: string | null;
  release_id: string;
  protocol_version: string;
  status_revision: number;
  session_id?: string | null;
  control_idle_timeout_sec?: number | null;
  remaining_inactivity_sec?: number | null;
  release_progress?: string | null;
  last_release_reason?: string | null;
  last_released_session_id?: string | null;
}}

export interface LogsResponse {{
  lines: string[];
  total_lines: number;
  total_bytes: number;
}}

export interface OperationStatusResponse {{
  epoch?: number | null;
  bind_token?: string | null;
  success?: boolean;
  message?: string | null;
  operation_id: string;
  status: 'pending' | 'completed' | 'failed';
  error: string | null;
}}

export interface ControlAcquireRequest {{
  protocol_version: "3.0.0";
  request_id: string;
  operator_id: string;
  max_linear_speed?: number | null;
  max_angular_speed?: number | null;
}}

export interface ControlAcquireResponse {{
  operation_id?: string | null;
  operation_token?: string | null;
  status?: "pending" | "completed" | "failed";
  success: boolean;
  epoch: number | null;
  bind_token?: string | null;
  active_owner?: string | null;
  error: WebControlErrorCode | null;
  message: string | null;
}}

export interface ControlReleaseRequest {{
  protocol_version: "3.0.0";
  request_id: string;
  epoch?: number | null;
  operation_id?: string | null;
  operation_token?: string | null;
  operator_id?: string | null;
}}

export interface ControlReleaseResponse {{
  status?: "pending" | "completed" | "failed";
  operation_id?: string | null;
  operation_token?: string | null;
  success: boolean;
  error: WebControlErrorCode | null;
  message?: string | null;
}}

export interface ControlStartRequest {{
  protocol_version: "3.0.0";
  request_id: string;
  epoch: number;
  operator_id?: string | null;
}}

export interface ControlStartResponse {{
  success: boolean;
  error: WebControlErrorCode | null;
  message: string | null;
}}

export interface ControlStopRequest {{
  request_id: string;
  epoch?: number | null;
  operator_id?: string | null;
}}

export interface ControlStopResponse {{
  success: boolean;
  disarmed: boolean;
}}

export interface Challenge {{
  input_generation: number;
  recovery_required: boolean;
  token: string;
  epoch: number;
  deadline_monotonic_ns: number;
  issued_monotonic_ns: number;
}}

export interface ChallengeResponse {{
  input_generation: number;
  token: string;
  epoch: number;
  sequence: number;
  direction: MotionDirection;
  client_timestamp_ms?: number | null;
}}

// WebSocket message wrappers
export type WsClientAction = 'bind' | 'intent' | 'stop' | 'heartbeat';

export interface WsClientMessage {{
  action: WsClientAction;
  payload: Record<string, any>;
}}

export type WsServerType = 'challenge' | 'state' | 'ack' | 'error';

export interface WsServerMessage {{
  type: WsServerType;
  payload: Record<string, any>;
}}

// Camera types and models
export type CameraState =
  | 'live'
  | 'connecting'
  | 'stale'
  | 'unavailable'
  | 'finalizing'
  | 'error';

export interface CameraProfile {{
  width: number;
  height: number;
  fps: number;
}}

export interface CameraStatusResponse {{
  state: CameraState;
  frame_age_sec: number | null;
  profile: CameraProfile;
  recording_state: string;
  recording_id: string | null;
  elapsed_sec: number | null;
  storage_available_bytes: number | null;
  viewers_count: number;
  last_error: string | null;
}}

export interface CameraCaptureRequest {{
  request_id?: string | null;
  idempotency_key?: string | null;
}}

export interface CameraCaptureResponse {{
  media_id: string;
  filename: string;
  timestamp: number;
  url: string;
  width: number;
  height: number;
  bytes: number;
}}

export interface CameraMediaItem {{
  media_id: string;
  type: string;
  filename: string;
  timestamp: number;
  url: string;
  width: number;
  height: number;
  bytes: number;
  completed: boolean;
}}

export interface CameraMediaListResponse {{
  items: CameraMediaItem[];
  total: number;
  limit: number;
  offset: number;
}}

export interface CameraRecordingResponse {{
  recording_id: string;
  state: string;
  elapsed_sec: number;
  url: string | null;
}}
"""


def export_specs(docs_dir: str, web_types_path: str) -> None:
    """Write OpenAPI JSON and TypeScript definitions to disk."""
    os.makedirs(docs_dir, exist_ok=True)
    os.makedirs(os.path.dirname(web_types_path), exist_ok=True)

    openapi_path = os.path.join(docs_dir, "openapi_v1.json")
    spec = generate_openapi_spec()
    with open(openapi_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    ts_code = generate_typescript_definitions()
    with open(web_types_path, "w", encoding="utf-8") as f:
        f.write(ts_code)


if __name__ == "__main__":
    import sys

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
    docs_d = os.path.join(repo_root, "ubuntu_tank/docs")
    ts_p = os.path.join(repo_root, "ubuntu_tank/web/src/types/api.ts")
    export_specs(docs_d, ts_p)
    print(f"Exported OpenAPI spec to {docs_d}/openapi_v1.json")
    print(f"Exported TypeScript types to {ts_p}")
