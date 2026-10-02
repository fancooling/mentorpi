// Composable managing robot telemetry, connection health, and protocol compatibility
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { apiClient, ApiError } from '../services/apiClient';
import type { StatusResponse, VersionResponse } from '../types/api';
import type { UiTelemetryState } from '../types/ui';

/** Poll visible telemetry; hidden pages retain ownership but require fresh status before driving. */
export function useRobotState(myOperatorId: string) {
  const telemetry = reactive<UiTelemetryState>({
    connectionStatus: 'disconnected',
    serviceState: 'unknown',
    operatorState: 'NO_OWNER',
    activeOwner: null,
    isOwner: false,
    currentEpoch: null,
    sessionId: null,
    statusRevision: -1,
    releaseProgress: null,
    lastReleaseReason: null,
    guardArmed: false,
    disarmPending: false,
    batteryVoltage: null,
    linearSpeed: 0.0,
    angularSpeed: 0.0,
    limits: {},
    freshness: {},
    lastFault: null,
    releaseId: 'unknown',
    protocolVersion: '3.0.0',
    isProtocolCompatible: false,
    lastUpdateTimestamp: null,
  });

  const isPolling = ref(false);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  let requestSequence = 0;
  let appliedSequence = 0;
  let visibilityGeneration = 0;
  const FRESHNESS_MS = 2500;

  const isOwner = computed(() => {
    return telemetry.connectionStatus === 'connected' && telemetry.activeOwner === myOperatorId && telemetry.sessionId === myOperatorId;
  });

  const isArmed = computed(() => {
    return (
      telemetry.connectionStatus === 'connected' && telemetry.guardArmed &&
      (telemetry.operatorState === 'ARMED_IDLE' || telemetry.operatorState === 'DRIVING' || telemetry.operatorState === 'INPUT_PAUSED')
    );
  });

  const isControllerRunning = computed(() => {
    return telemetry.serviceState === 'active';
  });

  async function checkVersion(): Promise<boolean> {
    try {
      const v: VersionResponse = await apiClient.getVersion();
      telemetry.releaseId = v.release_id;
      telemetry.protocolVersion = v.protocol_version;

      const major = parseInt(v.protocol_version.split('.')[0] || '1', 10);
      telemetry.isProtocolCompatible = major === 3;
      return telemetry.isProtocolCompatible;
    } catch {
      telemetry.isProtocolCompatible = false;
      return false;
    }
  }

  async function pollStatus(): Promise<void> {
    if (document.hidden) return;
    const sequence = ++requestSequence;
    const generation = visibilityGeneration;
    const started = performance.now();
    if (typeof navigator !== 'undefined' && !navigator.onLine) {
      handleOffline();
      return;
    }

    try {
      const s: StatusResponse = await apiClient.getStatus();
      if (generation !== visibilityGeneration || sequence < appliedSequence ||
          performance.now() - started > FRESHNESS_MS) return;
      if (s.status_revision < telemetry.statusRevision) {
        // A runtime restart resets revisions. Drop authority before accepting a
        // subsequent read; an older in-flight response cannot restore the session.
        appliedSequence = requestSequence;
        telemetry.statusRevision = -1;
        handleDisconnected();
        return;
      }
      appliedSequence = sequence;
      telemetry.statusRevision = s.status_revision;
      telemetry.sessionId = s.session_id ?? null;
      telemetry.releaseProgress = s.release_progress ?? null;
      telemetry.lastReleaseReason = s.last_release_reason ?? null;
      telemetry.connectionStatus = 'connected';
      telemetry.serviceState = s.service_state;
      telemetry.operatorState = s.operator_state;
      telemetry.activeOwner = s.active_owner;
      telemetry.isOwner = s.active_owner === myOperatorId;
      telemetry.currentEpoch = s.current_epoch;
      telemetry.guardArmed = Boolean(s.guard_armed);
      telemetry.disarmPending = Boolean(s.disarm_pending);
      telemetry.batteryVoltage = s.battery_voltage;
      telemetry.linearSpeed = s.linear_speed;
      telemetry.angularSpeed = s.angular_speed;
      telemetry.limits = s.limits || {};
      telemetry.freshness = s.freshness || {};
      telemetry.lastFault = s.last_fault;
      telemetry.releaseId = s.release_id;
      telemetry.protocolVersion = s.protocol_version;
      telemetry.lastUpdateTimestamp = Date.now();
    } catch (err) {
      if (generation !== visibilityGeneration || sequence < appliedSequence) return;
      appliedSequence = sequence;
      if (err instanceof ApiError && err.code === 'OFFLINE') {
        handleOffline();
      } else {
        handleDisconnected();
      }
    }
  }

  function handleOffline() {
    telemetry.connectionStatus = 'offline';
    // Clear live values: never show cached battery or armed state as live when offline!
    telemetry.batteryVoltage = null;
    telemetry.guardArmed = false;
    telemetry.isOwner = false;
    telemetry.activeOwner = null;
    telemetry.sessionId = null;
    telemetry.currentEpoch = null;
  }

  function handleDisconnected() {
    handleOffline();
    telemetry.connectionStatus = 'disconnected';
  }

  function startPolling(intervalMs: number = 1000) {
    if (isPolling.value) return;
    isPolling.value = true;
    pollStatus();
    pollTimer = setInterval(() => {
      if (document.hidden) return;
      if (telemetry.lastUpdateTimestamp !== null && Date.now() - telemetry.lastUpdateTimestamp > FRESHNESS_MS)
        handleDisconnected();
      void pollStatus();
    }, intervalMs);
  }

  function stopPolling() {
    isPolling.value = false;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function onOnline() {
    telemetry.connectionStatus = 'disconnected';
    pollStatus();
  }

  function onOffline() {
    handleOffline();
  }

  function onVisibilityChange() {
    visibilityGeneration++;
    // Visibility is not transport loss. Preserve the owner/session binding while
    // disabling controls and invalidating cached telemetry until a fresh read.
    // Clearing the owner here makes useControlSession close the control socket.
    telemetry.connectionStatus = 'disconnected';
    telemetry.guardArmed = false;
    telemetry.batteryVoltage = null;
    telemetry.lastUpdateTimestamp = null;
    if (!document.hidden) {
      void checkVersion();
      void pollStatus();
    }
  }

  onMounted(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('online', onOnline);
      window.addEventListener('offline', onOffline);
      document.addEventListener('visibilitychange', onVisibilityChange);
    }
    checkVersion();
    startPolling();
  });

  onUnmounted(() => {
    stopPolling();
    if (typeof window !== 'undefined') {
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
  });

  return {
    telemetry,
    isOwner,
    isArmed,
    isControllerRunning,
    pollStatus,
    checkVersion,
    startPolling,
    stopPolling,
  };
}

