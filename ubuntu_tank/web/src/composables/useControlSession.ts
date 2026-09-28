// Composable managing operator control authority, safety acknowledgment, and arming
import { ref, watch } from 'vue';
import { apiClient } from '../services/apiClient';
import { wsControlClient } from '../services/wsClient';
import type {
  ControlAcquireResponse,
  ControlArmResponse,
  ControlReleaseResponse,
  ControlStopResponse,
  OperationStatusResponse,
} from '../types/api';
import type { OperationFeedback } from '../types/ui';

function generateUuid(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return 'req-' + Math.random().toString(36).substring(2, 12) + '-' + Date.now();
}

export function useControlSession(
  operatorId: string,
  pollStatus: () => Promise<void>,
  isProtocolCompatible?: () => boolean,
  getCurrentEpoch?: () => number | null
) {
  const currentEpoch = ref<number | null>(null);
  const bindToken = ref<string | null>(null);
  const tracksRaisedConfirmed = ref(false);
  const isOperating = ref(false);
  const feedback = ref<OperationFeedback | null>(null);

  function syncEpochFromTelemetry() {
    if (getCurrentEpoch && currentEpoch.value !== null) {
      const ep = getCurrentEpoch();
      if (ep !== null && ep !== undefined && ep !== currentEpoch.value) {
        currentEpoch.value = ep;
        wsControlClient.updateEpoch(ep);
      }
    }
  }

  if (getCurrentEpoch) {
    watch(getCurrentEpoch, (newEp) => {
      if (newEp !== null && newEp !== undefined && currentEpoch.value !== null) {
        currentEpoch.value = newEp;
        wsControlClient.updateEpoch(newEp);
      }
    });
  }

  function setFeedback(
    type: 'info' | 'success' | 'warning' | 'error',
    message: string,
    code?: any
  ) {
    feedback.value = {
      type,
      message,
      code,
      timestamp: Date.now(),
    };
  }

  function clearFeedback() {
    feedback.value = null;
  }

  async function takeControl(): Promise<boolean> {
    if (isProtocolCompatible && !isProtocolCompatible()) {
      setFeedback(
        'error',
        'Control acquisition disabled: client protocol is incompatible with the robot. Please reload to update.'
      );
      return false;
    }

    isOperating.value = true;
    clearFeedback();
    try {
      const reqId = generateUuid();
      const res: ControlAcquireResponse = await apiClient.acquireControl({
        request_id: reqId,
        operator_id: operatorId,
        protocol_version: "2.0.0",
      });

      if (!res.success || res.epoch === null || !res.bind_token) {
        setFeedback(
          'error',
          res.message || 'Failed to acquire control slot',
          res.error
        );
        await pollStatus();
        return false;
      }

      currentEpoch.value = res.epoch;
      bindToken.value = res.bind_token;

      // Connect and bind WebSocket
      await wsControlClient.connect();
      wsControlClient.bind(operatorId, res.epoch, res.bind_token);

      setFeedback('success', `Control acquired (Epoch ${res.epoch})`);
      await pollStatus();
      syncEpochFromTelemetry();
      return true;
    } catch (err: any) {
      setFeedback('error', err.message || 'Error acquiring control');
      return false;
    } finally {
      isOperating.value = false;
    }
  }

  async function releaseControl(): Promise<boolean> {
    isOperating.value = true;
    try {
      wsControlClient.sendStop(generateUuid(), currentEpoch.value);
      if (currentEpoch.value !== null) {
        syncEpochFromTelemetry();
        const reqId = generateUuid();
        const res: ControlReleaseResponse = await apiClient.releaseControl({
          request_id: reqId,
          epoch: currentEpoch.value,
        });
        if (!res.success) {
          setFeedback('warning', 'Release returned failure from server');
        }
      }

      wsControlClient.disconnect();
      currentEpoch.value = null;
      bindToken.value = null;
      tracksRaisedConfirmed.value = false;

      setFeedback('info', 'Control released');
      await pollStatus();
      return true;
    } catch (err: any) {
      setFeedback('error', err.message || 'Error releasing control');
      return false;
    } finally {
      isOperating.value = false;
    }
  }

  async function arm(): Promise<boolean> {
    if (isProtocolCompatible && !isProtocolCompatible()) {
      setFeedback(
        'error',
        'Arming disabled: client protocol is incompatible with the robot. Please reload to update.'
      );
      return false;
    }

    if (!tracksRaisedConfirmed.value) {
      setFeedback(
        'error',
        'Physical safety acknowledgment required: confirm tracks are raised clear of ground'
      );
      return false;
    }

    syncEpochFromTelemetry();

    if (currentEpoch.value === null) {
      setFeedback('error', 'Must acquire control authority before arming');
      return false;
    }

    isOperating.value = true;
    clearFeedback();
    try {
      const reqId = generateUuid();
      const res: ControlArmResponse = await apiClient.armControl({
        request_id: reqId,
        epoch: currentEpoch.value,
      });

      if (!res.success) {
        setFeedback('error', res.message || 'Failed to arm chassis', res.error);
        await pollStatus();
        syncEpochFromTelemetry();
        return false;
      }

      setFeedback('success', 'Chassis armed. Ready to drive.');
      await pollStatus();
      syncEpochFromTelemetry();
      return true;
    } catch (err: any) {
      setFeedback('error', err.message || 'Error during chassis arming');
      return false;
    } finally {
      isOperating.value = false;
    }
  }

  async function disarm(): Promise<boolean> {
    const ok = await emergencyStop();
    syncEpochFromTelemetry();
    return ok;
  }

  async function emergencyStop(): Promise<boolean> {
    const reqId = generateUuid();
    // 1. Immediate WebSocket stop priority
    wsControlClient.sendStop(reqId, currentEpoch.value);

    // 2. HTTP stop fallback
    try {
      const res: ControlStopResponse = await apiClient.stopControl({
        request_id: reqId,
        epoch: currentEpoch.value,
      });
      await pollStatus();
      syncEpochFromTelemetry();
      return res.success;
    } catch (err: any) {
      console.warn('Fallback HTTP stop failed:', err);
      return false;
    }
  }

  async function startController(): Promise<boolean> {
    isOperating.value = true;
    clearFeedback();
    try {
      const reqId = generateUuid();
      const op: OperationStatusResponse = await apiClient.startController(reqId);
      if (op.status === 'failed') {
        setFeedback('error', op.error || 'Failed to start controller service');
        return false;
      }

      // Poll operation status up to 10 seconds
      const opId = op.operation_id;
      let completed = false;
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 500));
        const status = await apiClient.getOperation(opId);
        if (status.status === 'completed') {
          completed = true;
          break;
        }
        if (status.status === 'failed') {
          setFeedback('error', status.error || 'Controller start failed');
          return false;
        }
      }

      await pollStatus();
      if (completed) {
        setFeedback('success', 'Controller service started');
        return true;
      } else {
        setFeedback('warning', 'Controller start timed out awaiting completion');
        return false;
      }
    } catch (err: any) {
      setFeedback('error', err.message || 'Error starting controller');
      return false;
    } finally {
      isOperating.value = false;
    }
  }

  async function stopController(): Promise<boolean> {
    isOperating.value = true;
    clearFeedback();
    try {
      // Always stop motion and disarm first
      await emergencyStop();

      const reqId = generateUuid();
      const op: OperationStatusResponse = await apiClient.stopController(reqId);
      if (op.status === 'failed') {
        setFeedback('error', op.error || 'Failed to stop controller');
        return false;
      }

      const opId = op.operation_id;
      let completed = false;
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 500));
        const status = await apiClient.getOperation(opId);
        if (status.status === 'completed') {
          completed = true;
          break;
        }
        if (status.status === 'failed') {
          setFeedback('error', status.error || 'Controller stop failed');
          return false;
        }
      }

      await pollStatus();
      if (completed) {
        setFeedback('info', 'Controller service stopped');
        return true;
      } else {
        setFeedback('warning', 'Controller stop timed out awaiting confirmation');
        return false;
      }
    } catch (err: any) {
      setFeedback('error', err.message || 'Error stopping controller');
      return false;
    } finally {
      isOperating.value = false;
    }
  }

  return {
    currentEpoch,
    bindToken,
    tracksRaisedConfirmed,
    isOperating,
    feedback,
    setFeedback,
    clearFeedback,
    takeControl,
    releaseControl,
    arm,
    disarm,
    emergencyStop,
    startController,
    stopController,
  };
}

