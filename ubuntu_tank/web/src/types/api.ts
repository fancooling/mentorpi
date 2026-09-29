// Auto-generated TypeScript definitions for MentorPi Pi 5 Web Control.
// Protocol Version: 3.0.0, API Version: v1, Schema Version: 3

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

export interface VersionResponse {
  protocol_version: string;
  api_version: string;
  schema_version: number;
  release_id: string;
  supported_protocols: string[];
}

export interface StatusResponse {
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
}

export interface LogsResponse {
  lines: string[];
  total_lines: number;
  total_bytes: number;
}

export interface OperationStatusResponse {
  epoch?: number | null;
  bind_token?: string | null;
  success?: boolean;
  message?: string | null;
  operation_id: string;
  status: 'pending' | 'completed' | 'failed';
  error: string | null;
}

export interface ControlAcquireRequest {
  protocol_version: "3.0.0";
  request_id: string;
  operator_id: string;
  max_linear_speed?: number | null;
  max_angular_speed?: number | null;
}

export interface ControlAcquireResponse {
  operation_id?: string | null;
  operation_token?: string | null;
  status?: "pending" | "completed" | "failed";
  success: boolean;
  epoch: number | null;
  bind_token?: string | null;
  active_owner?: string | null;
  error: WebControlErrorCode | null;
  message: string | null;
}

export interface ControlReleaseRequest {
  protocol_version: "3.0.0";
  request_id: string;
  epoch?: number | null;
  operation_id?: string | null;
  operation_token?: string | null;
  operator_id?: string | null;
}

export interface ControlReleaseResponse {
  status?: "pending" | "completed" | "failed";
  operation_id?: string | null;
  operation_token?: string | null;
  success: boolean;
  error: WebControlErrorCode | null;
  message?: string | null;
}

export interface ControlStartRequest {
  protocol_version: "3.0.0";
  request_id: string;
  epoch: number;
  operator_id?: string | null;
}

export interface ControlStartResponse {
  success: boolean;
  error: WebControlErrorCode | null;
  message: string | null;
}

export interface ControlStopRequest {
  request_id: string;
  epoch?: number | null;
  operator_id?: string | null;
}

export interface ControlStopResponse {
  success: boolean;
  disarmed: boolean;
}

export interface Challenge {
  input_generation: number;
  recovery_required: boolean;
  token: string;
  epoch: number;
  deadline_monotonic_ns: number;
  issued_monotonic_ns: number;
}

export interface ChallengeResponse {
  input_generation: number;
  token: string;
  epoch: number;
  sequence: number;
  direction: MotionDirection;
  client_timestamp_ms?: number | null;
}

// WebSocket message wrappers
export type WsClientAction = 'bind' | 'intent' | 'stop' | 'heartbeat';

export interface WsClientMessage {
  action: WsClientAction;
  payload: Record<string, any>;
}

export type WsServerType = 'challenge' | 'state' | 'ack' | 'error';

export interface WsServerMessage {
  type: WsServerType;
  payload: Record<string, any>;
}
