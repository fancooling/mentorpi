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
    from ubuntu_tank_operator.constants import (
        API_VERSION,
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
    )
    from ubuntu_tank_operator.enums import (
        ControllerServiceState,
        MotionDirection,
        OperatorState,
        WebControlErrorCode,
    )
else:
    from .constants import API_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION
    from .enums import (
        ControllerServiceState,
        MotionDirection,
        OperatorState,
        WebControlErrorCode,
    )


def generate_openapi_spec() -> dict[str, Any]:
    """Construct complete OpenAPI 3.0.3 specification for Web Control API."""
    return {
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
            "/login": {
                "post": {
                    "summary": "Authenticate operator secret",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LoginRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Session established",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": ("#/components/schemas/LoginResponse")
                                    }
                                }
                            },
                        },
                        "401": {"description": "Invalid credentials"},
                    },
                }
            },
            "/logout": {
                "post": {
                    "summary": "Invalidate session and relinquish control",
                    "responses": {
                        "200": {
                            "description": "Session terminated",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": ("#/components/schemas/LogoutResponse")
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
            "/controller/start": {
                "post": {
                    "summary": "Start controller systemd service unit",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/ControllerOperationRequest"
                                    )
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Operation result",
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
            "/controller/stop": {
                "post": {
                    "summary": (
                        "Stop controller systemd service (invalidates driving first)"
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/ControllerOperationRequest"
                                    )
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Operation result",
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
            "/control/acquire": {
                "post": {
                    "summary": "Acquire single operator ownership slot",
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
                    "summary": "Relinquish operator ownership",
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
            "/control/arm": {
                "post": {
                    "summary": "Arm motor guard with tracks_raised affirmation",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": ("#/components/schemas/ControlArmRequest")
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Arming result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": (
                                            "#/components/schemas/ControlArmResponse"
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
                "LoginRequest": {
                    "type": "object",
                    "required": ["password"],
                    "properties": {"password": {"type": "string"}},
                },
                "LoginResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "session_id": {"type": "string", "nullable": True},
                        "expires_in_sec": {"type": "integer"},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                    },
                },
                "LogoutResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {"success": {"type": "boolean"}},
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
                "ControllerOperationRequest": {
                    "type": "object",
                    "required": ["request_id", "action"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["start", "stop"]},
                    },
                },
                "OperationStatusResponse": {
                    "type": "object",
                    "required": ["operation_id", "status"],
                    "properties": {
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
                    "required": ["request_id", "operator_id"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "operator_id": {"type": "string"},
                    },
                },
                "ControlAcquireResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "epoch": {"type": "integer", "nullable": True},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                        "message": {"type": "string", "nullable": True},
                    },
                },
                "ControlReleaseRequest": {
                    "type": "object",
                    "required": ["request_id", "epoch"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "epoch": {"type": "integer"},
                    },
                },
                "ControlReleaseResponse": {
                    "type": "object",
                    "required": ["success"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "error": {
                            "$ref": "#/components/schemas/WebControlErrorCode",
                            "nullable": True,
                        },
                    },
                },
                "ControlArmRequest": {
                    "type": "object",
                    "required": ["request_id", "epoch", "tracks_raised"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "epoch": {"type": "integer"},
                        "tracks_raised": {"type": "boolean", "enum": [True]},
                    },
                },
                "ControlArmResponse": {
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
                        "token",
                        "epoch",
                        "deadline_monotonic_ns",
                        "issued_monotonic_ns",
                    ],
                    "properties": {
                        "token": {"type": "string"},
                        "epoch": {"type": "integer"},
                        "deadline_monotonic_ns": {"type": "integer"},
                        "issued_monotonic_ns": {"type": "integer"},
                    },
                },
                "ChallengeResponse": {
                    "type": "object",
                    "required": ["token", "epoch", "sequence", "direction"],
                    "properties": {
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
            }
        },
    }


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

export interface LoginRequest {{
  password: string;
}}

export interface LoginResponse {{
  success: boolean;
  session_id: string | null;
  expires_in_sec: number;
  error?: WebControlErrorCode | null;
}}

export interface LogoutResponse {{
  success: boolean;
}}

export interface StatusResponse {{
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
}}

export interface LogsResponse {{
  lines: string[];
  total_lines: number;
  total_bytes: number;
}}

export interface ControllerOperationRequest {{
  request_id: string;
  action: 'start' | 'stop';
}}

export interface OperationStatusResponse {{
  operation_id: string;
  status: 'pending' | 'completed' | 'failed';
  error: string | null;
}}

export interface ControlAcquireRequest {{
  request_id: string;
  operator_id: string;
}}

export interface ControlAcquireResponse {{
  success: boolean;
  epoch: number | null;
  error: WebControlErrorCode | null;
  message: string | null;
}}

export interface ControlReleaseRequest {{
  request_id: string;
  epoch: number;
}}

export interface ControlReleaseResponse {{
  success: boolean;
  error: WebControlErrorCode | null;
}}

export interface ControlArmRequest {{
  request_id: string;
  epoch: number;
  tracks_raised: true; // Strictly exact boolean true
}}

export interface ControlArmResponse {{
  success: boolean;
  error: WebControlErrorCode | null;
  message: string | null;
}}

export interface ControlStopRequest {{
  request_id: string;
  epoch?: number | null;
}}

export interface ControlStopResponse {{
  success: boolean;
  disarmed: boolean;
}}

export interface Challenge {{
  token: string;
  epoch: number;
  deadline_monotonic_ns: number;
  issued_monotonic_ns: number;
}}

export interface ChallengeResponse {{
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
