// Own one cancellable startup/acquisition/binding transaction; Start is always explicit.
import { computed, ref, watch } from 'vue';
import { apiClient } from '../services/apiClient';
import { wsControlClient } from '../services/wsClient';
import type { ControlReleaseResponse } from '../types/api';
import type { OperationFeedback } from '../types/ui';
const uuid = () => crypto.randomUUID?.() ?? `req-${Date.now()}-${Math.random()}`;
/** Coordinate one tab's setup/Start commands; stale asynchronous results never restore control. */
export function useControlSession(operatorId: string, pollStatus: () => Promise<void>, isProtocolCompatible = () => false, getCurrentEpoch = () => null as number | null, getActiveOwner = () => null as string | null, getSessionId = () => null as string | null, getReleaseReason = () => null as string | null) {
    const currentEpoch = ref<number | null>(null);
    const isOperating = ref(false);
    const isBound = ref(false);
    const feedback = ref<OperationFeedback | null>(null);
    const isReleasing = ref(false);
    const stopsInFlight = ref(0);
    const isStopping = computed(() => stopsInFlight.value > 0);
    const hasSession = ref(false);
    let pendingAcquire: ReturnType<typeof apiClient.acquireControl> | null = null;
    let generation = 0;
    let setup: {
        operation_id: string;
        operation_token: string;
    } | null = null;
    const setFeedback = (type: OperationFeedback['type'], message: string, code?: any) => {
        feedback.value = { type, message, code, timestamp: Date.now() };
    };
    const clearFeedback = () => { feedback.value = null; };
    watch(getCurrentEpoch, epoch => {
        if (epoch !== null && currentEpoch.value !== null) {
            currentEpoch.value = epoch;
            wsControlClient.updateEpoch(epoch);
        }
    });
    watch([getActiveOwner, getSessionId], ([owner, session]) => {
        if (isBound.value && (owner !== operatorId || session !== operatorId)) {
            void invalidateSetup();
            wsControlClient.disconnect();
            isBound.value = false;
            hasSession.value = false;
            currentEpoch.value = null;
            setFeedback('info', getReleaseReason() === 'CONTROL_IDLE_TIMEOUT' ? 'Control released due to inactivity' : getReleaseReason() ? `Control released: ${getReleaseReason()}` : 'Control session lost; take control again');
        }
    });
    async function waitForRelease(result: Omit<ControlReleaseResponse, 'error'> & { error?: string | null }) {
        const deadline = performance.now() + 22000;
        while (result.status === 'pending' && result.operation_id && performance.now() < deadline) {
            const status = await apiClient.getOperation(result.operation_id, result.operation_token ?? undefined);
            result = { ...result, ...status, success: status.success ?? false };
            if (result.status === 'pending')
                await new Promise(resolve => setTimeout(resolve, 100));
        }
        if (!result.success || result.status !== 'completed')
            throw new Error(result.message || result.error || 'Controller shutdown unconfirmed; retry Release control');
    }
    async function cancelOperation(operation: typeof setup) {
        if (!operation) return true;
        try {
            await waitForRelease(await apiClient.releaseControl({ protocol_version: '3.0.0', request_id: uuid(), ...operation }));
            return true;
        } catch {
            // An inactive controller can still have startup in flight. Only a
            // completed acquisition or terminal startup error permits this fallback;
            // cancellation (STALE_TRANSACTION) does not prove its worker finished.
            try {
                const operationStatus = await apiClient.getOperation(operation.operation_id, operation.operation_token);
                if (operationStatus.status !== 'completed' &&
                    !(operationStatus.status === 'failed' && operationStatus.error === 'OPERATION_FAILED')) return false;
                const status = await apiClient.getStatus();
                return status.active_owner === null && status.service_state === 'inactive' && !status.release_progress;
            } catch { return false; }
        }
    }
    async function invalidateSetup() {
        generation++;
        let previous = setup;
        const acquiring = pendingAcquire;
        setup = null;
        if (isOperating.value && !isBound.value)
            wsControlClient.disconnect();
        if (!isReleasing.value) isOperating.value = false;
        const cancellationGeneration = generation;
        if (!previous && acquiring) {
            try {
                const result = await acquiring;
                if (result.operation_id && result.operation_token)
                    previous = { operation_id: result.operation_id, operation_token: result.operation_token };
                else if (result.success) return false;
            } catch { return false; }
        }
        return cancelOperation(previous).then(ok => {
            if (!ok && generation === cancellationGeneration && !isBound.value)
                setup = previous;
            return ok;
        });
    }
    async function takeControl() {
        if (isStopping.value || isReleasing.value || isOperating.value || isBound.value || !isProtocolCompatible())
            return false;
        const transaction = ++generation;
        isOperating.value = true;
        hasSession.value = true;
        setFeedback('info', 'Starting controller and taking control…');
        let operation: typeof setup = null;
        try {
            pendingAcquire = apiClient.acquireControl({ request_id: uuid(), operator_id: operatorId, protocol_version: '3.0.0' });
            const result = await pendingAcquire;
            if (!result.success || !result.operation_id || !result.operation_token)
                throw new Error(result.message || result.error || 'Control acquisition failed');
            operation = { operation_id: result.operation_id, operation_token: result.operation_token };
            if (transaction !== generation) {
                await cancelOperation(operation);
                return false;
            }
            setup = operation;
            const deadline = performance.now() + 22000;
            while (transaction === generation && performance.now() < deadline) {
                const status = await apiClient.getOperation(operation.operation_id, operation.operation_token);
                if (transaction !== generation)
                    break;
                if (status.status === 'failed')
                    throw new Error(status.message || status.error || 'Controller setup failed');
                if (status.status === 'completed') {
                    if (status.epoch == null || !status.bind_token)
                        throw new Error('Control binding unavailable');
                    currentEpoch.value = status.epoch;
                    setFeedback('info', 'Connecting control…');
                    await wsControlClient.connect();
                    if (transaction !== generation)
                        break;
                    await wsControlClient.bind(operatorId, status.epoch, status.bind_token);
                    if (transaction !== generation)
                        break;
                    isBound.value = true;
                    setup = null;
                    setFeedback('success', 'Control ready. Start to enable movement.');
                    await pollStatus();
                    return true;
                }
                await new Promise(resolve => setTimeout(resolve, 100));
            }
            if (transaction === generation)
                throw new Error('Control setup timed out');
            return false;
        }
        catch (error: any) {
            if (transaction === generation)
                setFeedback('error', error.message || 'Control setup failed');
            return false;
        }
        finally {
            let canceled = true;
            if (!isBound.value) {
                canceled = await cancelOperation(operation);
                if (canceled && !isReleasing.value) hasSession.value = false;
            }
            pendingAcquire = null;
            if (transaction === generation) {
                setup = canceled ? null : operation;
                isOperating.value = false;
                if (!isBound.value) {
                    wsControlClient.disconnect();
                    currentEpoch.value = null;
                }
            }
        }
    }
    async function emergencyStop() {
        stopsInFlight.value++;
        const cancellation = invalidateSetup();
        const request_id = uuid();
        wsControlClient.sendStop(request_id, currentEpoch.value);
        try {
            const result = await apiClient.stopControl({ request_id, epoch: currentEpoch.value, operator_id: operatorId });
            await cancellation;
            await pollStatus();
            return result.success;
        }
        catch {
            return false;
        }
        finally {
            stopsInFlight.value--;
        }
    }
    async function releaseControl() {
        if (isReleasing.value) return false;
        if (currentEpoch.value === null && setup === null && !isOperating.value) {
            setFeedback('error', 'This tab does not hold control');
            return false;
        }
        isReleasing.value = true;
        const cancellation = invalidateSetup();
        isOperating.value = true;
        setFeedback('info', 'Releasing control — waiting for controller shutdown…');
        try {
            if (!await cancellation) {
                setFeedback('error', 'Controller shutdown unconfirmed; retry Release control');
                return false;
            }
            if (currentEpoch.value !== null) {
                await waitForRelease(await apiClient.releaseControl({ protocol_version: '3.0.0', request_id: uuid(), epoch: getCurrentEpoch() ?? currentEpoch.value }));
            }
            wsControlClient.disconnect();
            isBound.value = false;
            hasSession.value = false;
            currentEpoch.value = null;
            setFeedback('info', 'Control released; controller stopped');
            return true;
        }
        catch (error: any) {
            // Retain the bound session so a transient shutdown failure can be retried.
            setFeedback('error', error.message || 'Release failed');
            return false;
        }
        finally {
            isReleasing.value = false;
            isOperating.value = false;
            await pollStatus();
        }
    }
    async function start() {
        if (isStopping.value || !isBound.value || isOperating.value || !isProtocolCompatible() || currentEpoch.value === null)
            return false;
        const transaction = generation;
        isOperating.value = true;
        try {
            const result = await apiClient.startControl({ protocol_version: '3.0.0', request_id: uuid(), epoch: getCurrentEpoch() ?? currentEpoch.value });
            if (transaction !== generation)
                return false;
            setFeedback(result.success ? 'success' : 'error', result.success ? 'Chassis armed. Ready to drive.' : result.message || 'Starting failed');
            await pollStatus();
            return result.success;
        }
        catch (error: any) {
            if (transaction === generation)
                setFeedback('error', error.message || 'Starting failed');
            return false;
        }
        finally {
            if (transaction === generation)
                isOperating.value = false;
        }
    }
    wsControlClient.setHandlers({ onDisconnect: () => {
            isBound.value = false;
            hasSession.value = false;
            currentEpoch.value = null;
            void emergencyStop();
        } });
    return { currentEpoch, isOperating, isReleasing, isStopping, isBound, hasSession, feedback, setFeedback, clearFeedback,
        takeControl, releaseControl, start, emergencyStop };
}
