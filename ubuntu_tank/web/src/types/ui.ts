// UI State and View Models for MentorPi Pi 5 Web Control
import type {
  ControllerServiceState,
  MotionDirection,
  OperatorState,
  WebControlErrorCode,
} from './api';

export type ConnectionStatus = 'connected' | 'disconnected' | 'offline';

export type DrivingStatus =
  | 'idle'
  | 'driving'
  | 'conflict'
  | 'hold_capped'
  | 'idle_timeout'
  | 'disabled';

export interface UiTelemetryState {
  connectionStatus: ConnectionStatus;
  serviceState: ControllerServiceState;
  operatorState: OperatorState;
  activeOwner: string | null;
  isOwner: boolean;
  currentEpoch: number | null;
  guardArmed: boolean;
  disarmPending: boolean;
  batteryVoltage: number | null;
  linearSpeed: number;
  angularSpeed: number;
  limits: Record<string, number>;
  freshness: Record<string, number | null>;
  lastFault: string | null;
  releaseId: string;
  protocolVersion: string;
  isProtocolCompatible: boolean;
  lastUpdateTimestamp: number | null;
}

export interface DriveControlState {
  activeDirection: MotionDirection;
  drivingStatus: DrivingStatus;
  isPanelFocused: boolean;
  tracksRaisedConfirmed: boolean;
  continuousHoldDurationMs: number;
  idleDurationMs: number;
  inputConflict: boolean;
  holdCapped: boolean;
  heldKeys: Set<string>;
  activePointerId: number | null;
}

export interface OperationFeedback {
  type: 'info' | 'success' | 'warning' | 'error';
  message: string;
  code?: WebControlErrorCode | null;
  timestamp: number;
}

