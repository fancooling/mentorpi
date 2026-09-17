// Auto-generated TypeScript definitions for MentorPi Pi 5 Web Control.
// Protocol Version: 1.0.0, API Version: v1, Schema Version: 1

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

export interface VersionResponse {
  protocol_version: string;
  api_version: string;
  schema_version: number;
  release_id: string;
  supported_protocols: string[];
}

export interface StatusResponse {
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
}

export interface LogsResponse {
  lines: string[];
  total_lines: number;
  total_bytes: number;
}

export interface ControllerOperationRequest {
  request_id: string;
  action: 'start' | 'stop';
}

export interface OperationStatusResponse {
  operation_id: string;
  status: 'pending' | 'completed' | 'failed';
  error: string | null;
}

export interface ControlAcquireRequest {
  request_id: string;
  operator_id: string;
}

export interface ControlAcquireResponse {
  success: boolean;
  epoch: number | null;
  error: WebControlErrorCode | null;
  message: string | null;
}

export interface ControlReleaseRequest {
  request_id: string;
  epoch: number;
}

export interface ControlReleaseResponse {
  success: boolean;
  error: WebControlErrorCode | null;
}

export interface ControlArmRequest {
  request_id: string;
  epoch: number;
  tracks_raised: true; // Strictly exact boolean true
}

export interface ControlArmResponse {
  success: boolean;
  error: WebControlErrorCode | null;
  message: string | null;
}

export interface ControlStopRequest {
  request_id: string;
  epoch?: number | null;
}

export interface ControlStopResponse {
  success: boolean;
  disarmed: boolean;
}

export interface Challenge {
  token: string;
  epoch: number;
  deadline_monotonic_ns: number;
  issued_monotonic_ns: number;
}

export interface ChallengeResponse {
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
