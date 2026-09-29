// Own one cancellable startup/acquisition/binding transaction; Arm is always explicit.
import { ref, watch } from 'vue';
import { apiClient } from '../services/apiClient';
import { wsControlClient } from '../services/wsClient';
import type { ControlReleaseResponse } from '../types/api';
import type { OperationFeedback } from '../types/ui';
const uuid = () => crypto.randomUUID?.() ?? `req-${Date.now()}-${Math.random()}`;
/** Coordinate one tab's setup/Arm commands; stale asynchronous results never restore control. */
export function useControlSession(operatorId: string, pollStatus: () => Promise<void>, isProtocolCompatible = () => false, getCurrentEpoch = () => null as number | null, getActiveOwner = () => null as string | null) {
    const currentEpoch = ref<number | null>(null);
    const isOperating = ref(false);
    const isBound = ref(false);
    const feedback = ref<OperationFeedback | null>(null);
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
    watch(getActiveOwner, owner => {
        if (isBound.value && owner !== operatorId) {
            void invalidateSetup();
            wsControlClient.disconnect();
            isBound.value = false;
            currentEpoch.value = null;
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
            // Cleanup must not overwrite the original setup failure or timeout.
            return false;
        }
    }
    function invalidateSetup() {
        generation++;
        const previous = setup;
        setup = null;
        if (isOperating.value && !isBound.value)
            wsControlClient.disconnect();
        isOperating.value = false;
        const cancellationGeneration = generation;
        return cancelOperation(previous).then(ok => {
            if (!ok && generation === cancellationGeneration && !isBound.value)
                setup = previous;
            return ok;
        });
    }
    async function takeControl() {
        if (isOperating.value || isBound.value || !isProtocolCompatible())
            return false;
        const transaction = ++generation;
        isOperating.value = true;
        setFeedback('info', 'Starting controller and taking control…');
        let operation: typeof setup = null;
        try {
            const result = await apiClient.acquireControl({ request_id: uuid(), operator_id: operatorId, protocol_version: '3.0.0' });
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
                    setFeedback('success', 'Control ready. Arm to enable movement.');
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
            if (!isBound.value)
                await cancelOperation(operation);
            if (transaction === generation) {
                setup = null;
                isOperating.value = false;
                if (!isBound.value) {
                    wsControlClient.disconnect();
                    currentEpoch.value = null;
                }
            }
        }
    }
    async function emergencyStop() {
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
    }
    async function releaseControl() {
        if (currentEpoch.value === null && setup === null && !isOperating.value) {
            setFeedback('error', 'This tab does not hold control');
            return false;
        }
        const cancellation = invalidateSetup();
        isOperating.value = true;
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
            isOperating.value = false;
            await pollStatus();
        }
    }
    async function arm() {
        if (!isBound.value || isOperating.value || !isProtocolCompatible() || currentEpoch.value === null)
            return false;
        const transaction = generation;
        isOperating.value = true;
        try {
            const result = await apiClient.startControl({ protocol_version: '3.0.0', request_id: uuid(), epoch: getCurrentEpoch() ?? currentEpoch.value });
            if (transaction !== generation)
                return false;
            setFeedback(result.success ? 'success' : 'error', result.success ? 'Chassis armed. Ready to drive.' : result.message || 'Arming failed');
            await pollStatus();
            return result.success;
        }
        catch (error: any) {
            if (transaction === generation)
                setFeedback('error', error.message || 'Arming failed');
            return false;
        }
        finally {
            if (transaction === generation)
                isOperating.value = false;
        }
    }
    // Transitional UI alias until M14.6 removes the redundant button.
    async function stopController() {
        return releaseControl();
    }
    wsControlClient.setHandlers({ onDisconnect: () => {
            isBound.value = false;
            currentEpoch.value = null;
            void emergencyStop();
        } });
    return { currentEpoch, isOperating, isBound, feedback, setFeedback, clearFeedback,
        takeControl, releaseControl, arm, disarm: emergencyStop, emergencyStop, stopController };
}
