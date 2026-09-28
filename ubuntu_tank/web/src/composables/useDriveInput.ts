// Track physical release independently of active motion so paused or held input cannot replay.
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { wsControlClient } from '../services/wsClient';
import type { MotionDirection } from '../types/api';
import type { DriveControlState } from '../types/ui';

const CONTINUOUS_HOLD_CAP_MS = 5000;
const IDLE_TIMEOUT_MS = 30000;

/** Bind keyboard/pointer control to armed ownership and acknowledged neutral recovery. */
export function useDriveInput(
  isArmed: () => boolean,
  isOwner: () => boolean,
  onEmergencyStop: () => Promise<boolean>
) {
  const state = reactive<DriveControlState>({
    activeDirection: 'neutral',
    drivingStatus: 'idle',
    isPanelFocused: false,
    continuousHoldDurationMs: 0,
    idleDurationMs: 0,
    inputConflict: false,
    holdCapped: false,
    heldKeys: new Set<string>(),
    activePointerId: null,
  });

  const recovery = ref<'ready' | 'release' | 'confirm'>('ready');
  const recovered = ref(false);
  const physicalPointers = new Set<number>();
  let observedGeneration = -1;
  const recoveryMessage = computed(() => recovery.value === 'release'
    ? 'Input paused — release controls'
    : recovery.value === 'confirm' ? 'Input paused — confirming neutral' : recovered.value ? 'Armed idle — ready for a new press' : '');
  function released() { return physicallyDepressedKeys.size === 0 && physicalPointers.size === 0; }
  function pauseInput() {
    clearDirection(); state.heldKeys.clear(); state.activePointerId = null;
    physicallyDepressedKeys.forEach(key => keysHeldBeforeArm.add(key));
    recovered.value = false;
    recovery.value = released() ? 'confirm' : 'release';
  }

  const keysHeldBeforeArm = new Set<string>();
  const physicallyDepressedKeys = new Set<string>();
  let holdTimer: ReturnType<typeof setInterval> | null = null;
  let holdStartTime = 0;
  let lastDirectionalInputTime = Date.now();

  const isDriving = computed(() => state.activeDirection !== 'neutral' && state.activeDirection !== 'stop');

  // Register challenge handler with WebSocket client
  wsControlClient.setHandlers({
    onChallenge: (challenge) => {
      if (challenge.recovery_required && recovery.value === 'ready') pauseInput();
      observedGeneration = Math.max(observedGeneration, challenge.input_generation);
      if (!isArmed() || !isOwner()) return null;
      if (recovery.value !== 'ready') {
        if (!released()) { recovery.value = 'release'; return null; }
        recovery.value = 'confirm';
        return 'neutral';
      }
      return state.inputConflict || state.holdCapped ? 'neutral' : state.activeDirection;
    },
    onAck: (payload, _challenge, direction) => {
      if (recovery.value !== 'ready' && direction === 'neutral' &&
          payload.operator_state === 'ARMED_IDLE' && payload.input_generation >= observedGeneration) {
        observedGeneration = payload.input_generation;
        clearDirection(); state.heldKeys.clear(); state.activePointerId = null;
        physicallyDepressedKeys.forEach(key => keysHeldBeforeArm.add(key));
        recovery.value = 'ready'; recovered.value = true;
      }
    },
    onError: (code, _message, payload) => {
      if (!wsControlClient.isSessionBound) return;
      if (payload.operator_state === 'INPUT_PAUSED') { pauseInput(); return; }
      if (['CHALLENGE_EXPIRED', 'STALE_TRANSACTION', 'LEASE_EXPIRED'].includes(code)) {
        pauseInput(); return;
      }
      resetAllInput(); void onEmergencyStop();
    },
    onDisconnect: () => { resetAllInput(); },
  });

  function clearDirection() {
    state.activeDirection = 'neutral';
    state.continuousHoldDurationMs = 0;
    holdStartTime = 0;
    if (holdTimer) {
      clearInterval(holdTimer);
      holdTimer = null;
    }
  }

  function resetAllInput() {
    clearDirection();
    state.heldKeys.clear();
    state.activePointerId = null;
    state.inputConflict = false;
    state.drivingStatus = 'idle';
    recovery.value = 'ready';
    observedGeneration = -1; recovered.value = false;
    physicallyDepressedKeys.forEach(key => keysHeldBeforeArm.add(key));
  }

  function handleConflict() {
    state.inputConflict = true;
    state.drivingStatus = 'conflict';
    clearDirection();
    onEmergencyStop();
  }

  function handleHoldCapped() {
    state.holdCapped = true;
    state.drivingStatus = 'hold_capped';
    clearDirection();
    onEmergencyStop();
  }

  function checkInputIntegrity() {
    // Exactly one input source is valid. If multiple keys held or key + pointer: conflict!
    const keyCount = state.heldKeys.size;
    const hasPointer = state.activePointerId !== null;

    if (keyCount > 1 || (keyCount >= 1 && hasPointer)) {
      handleConflict();
      return false;
    }

    if (keyCount === 0 && !hasPointer) {
      // All inputs released: clear conflict and capped state
      state.inputConflict = false;
      state.holdCapped = false;
      clearDirection();
      state.drivingStatus = 'idle';
      return false;
    }

    return true;
  }

  function setDirection(dir: MotionDirection) {
    if (!isArmed() || !isOwner() || recovery.value !== 'ready') {
      clearDirection();
      state.drivingStatus = 'disabled';
      return;
    }

    if (state.inputConflict || state.holdCapped) {
      return;
    }

    state.activeDirection = dir;
    if (dir !== 'neutral') recovered.value = false;
    state.drivingStatus = dir === 'neutral' ? 'idle' : 'driving';
    lastDirectionalInputTime = Date.now();

    if (dir !== 'neutral') {
      holdStartTime = Date.now();
      if (!holdTimer) {
        holdTimer = setInterval(tickTimers, 50);
      }
    } else {
      clearDirection();
    }
  }

  function tickTimers() {
    const now = Date.now();
    // 1. Continuous hold cap (5.0 seconds)
    if (isDriving.value && holdStartTime > 0) {
      const elapsed = now - holdStartTime;
      state.continuousHoldDurationMs = elapsed;
      if (elapsed >= CONTINUOUS_HOLD_CAP_MS) {
        handleHoldCapped();
        return;
      }
    }

    // 2. Idle timeout (30.0 seconds without directional input)
    if (isArmed()) {
      const idleElapsed = now - lastDirectionalInputTime;
      state.idleDurationMs = idleElapsed;
      if (idleElapsed >= IDLE_TIMEOUT_MS) {
        state.drivingStatus = 'idle_timeout';
        clearDirection();
        onEmergencyStop();
      }
    }
  }

  // Pointer event handlers
  function onPointerDown(e: PointerEvent, dir: MotionDirection) {
    e.preventDefault();
    physicalPointers.add(e.pointerId);
    if (!isArmed() || !isOwner() || recovery.value !== 'ready') return;

    if (physicalPointers.size > 1 || state.activePointerId !== null || state.heldKeys.size > 0) {
      handleConflict();
      return;
    }

    state.activePointerId = e.pointerId;
    const target = e.currentTarget as HTMLElement | null;
    if (target && typeof target.setPointerCapture === 'function') {
      try {
        target.setPointerCapture(e.pointerId);
      } catch {
        // Ignore pointer capture errors
      }
    }

    setDirection(dir);
  }

  function onPointerUp(e: PointerEvent) {
    e.preventDefault();
    physicalPointers.delete(e.pointerId);
    if (state.activePointerId === e.pointerId) {
      state.activePointerId = null;
      const target = e.currentTarget as HTMLElement | null;
      if (target && typeof target.releasePointerCapture === 'function') {
        try {
          target.releasePointerCapture(e.pointerId);
        } catch {
          // Ignore
        }
      }
      checkInputIntegrity();
      clearDirection();
    }
  }

  function onPointerCancel(e: PointerEvent) {
    e.preventDefault();
    physicalPointers.delete(e.pointerId);
    if (state.activePointerId === e.pointerId) {
      state.activePointerId = null;
      checkInputIntegrity();
      clearDirection();
    }
  }

  function onPointerLeave(e: PointerEvent) {
    if (state.activePointerId === e.pointerId) {
      state.activePointerId = null;
      checkInputIntegrity();
      clearDirection();
    }
  }

  // Keyboard driving handlers
  function codeToDirection(code: string): MotionDirection | null {
    switch (code) {
      case 'KeyW':
        return 'forward';
      case 'KeyS':
        return 'reverse';
      case 'KeyA':
        return 'spin_left';
      case 'KeyD':
        return 'spin_right';
      default:
        return null;
    }
  }

  function onKeyDown(e: KeyboardEvent) {
    // 1. Space has STOP priority document-wide
    if (e.code === 'Space') {
      e.preventDefault();
      resetAllInput();
      onEmergencyStop();
      return;
    }

    const dir = codeToDirection(e.code);
    if (!dir) return;

    // Track physically depressed directional keys regardless of arming or panel focus
    physicallyDepressedKeys.add(e.code);
    if (recovery.value !== 'ready') { keysHeldBeforeArm.add(e.code); return; }

    // Key held before Arm cannot start motion until released
    if (keysHeldBeforeArm.has(e.code)) {
      return;
    }

    // Ignore if typing in an input or dialog, or if modifier keys held
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const activeEl = document.activeElement;
    if (
      activeEl &&
      (activeEl.tagName === 'INPUT' ||
        activeEl.tagName === 'TEXTAREA' ||
        activeEl.getAttribute('contenteditable') === 'true' ||
        activeEl.tagName === 'SELECT')
    ) {
      return;
    }

    // Must be owner, armed, and drive panel focused
    if (!isOwner() || !isArmed() || !state.isPanelFocused) {
      return;
    }

    // Prevent default scrolling for W/A/S/D
    e.preventDefault();

    // Detect repeat
    if (e.repeat) {
      return;
    }

    state.heldKeys.add(e.code);

    if (!checkInputIntegrity()) {
      return;
    }

    setDirection(dir);
  }

  function onKeyUp(e: KeyboardEvent) {
    if (e.code === 'Space') {
      e.preventDefault();
      return;
    }

    physicallyDepressedKeys.delete(e.code);
    keysHeldBeforeArm.delete(e.code);

    if (state.heldKeys.has(e.code)) {
      e.preventDefault();
      state.heldKeys.delete(e.code);
      checkInputIntegrity();
      if (state.heldKeys.size === 0 && state.activePointerId === null) {
        clearDirection();
      }
    }
  }

  // Releases must still arrive when the paused button is disabled or capture is lost.
  function physicalPointerDown(e: PointerEvent) {
    if ((e.target as Element)?.closest?.('.btn-dir')) physicalPointers.add(e.pointerId);
  }
  function physicalPointerUp(e: PointerEvent) {
    physicalPointers.delete(e.pointerId);
    if (state.activePointerId === e.pointerId) onPointerUp(e);
  }

  // Lifecycle blur & visibility change handlers
  function onBlur() {
    resetAllInput();
    onEmergencyStop();
  }

  function onVisibilityChange() {
    if (document.hidden) {
      resetAllInput();
      onEmergencyStop();
    }
  }

  // Watch arm status to track keys held prior to arming or during disarm
  watch(isArmed, (armed) => {
    if (armed) {
      // Record any currently physically depressed keys as invalid until released
      physicallyDepressedKeys.forEach((k) => keysHeldBeforeArm.add(k));
      lastDirectionalInputTime = Date.now();
    } else {
      resetAllInput();
      // When disarmed, any key that is still physically depressed must NOT start motion if re-armed
      physicallyDepressedKeys.forEach((k) => keysHeldBeforeArm.add(k));
    }
  });

  // Watch panel focus: blurring the drive panel during keyboard driving immediately halts motion
  watch(
    () => state.isPanelFocused,
    (focused) => {
      if (!focused) {
        if (state.heldKeys.size > 0 || state.activeDirection !== 'neutral') {
          clearDirection();
          state.heldKeys.clear();
          state.drivingStatus = 'idle';
        }
      }
    }
  );

  onMounted(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', onKeyDown, { capture: true });
      window.addEventListener('keyup', onKeyUp, { capture: true });
      window.addEventListener('blur', onBlur);
      document.addEventListener('visibilitychange', onVisibilityChange);
      window.addEventListener('pointerdown', physicalPointerDown, true);
      window.addEventListener('pointerup', physicalPointerUp, true);
      window.addEventListener('pointercancel', physicalPointerUp, true);
      window.addEventListener('pagehide', onBlur);
    }
  });

  onUnmounted(() => {
    resetAllInput();
    if (holdTimer) {
      clearInterval(holdTimer);
      holdTimer = null;
    }
    if (typeof window !== 'undefined') {
      window.removeEventListener('keydown', onKeyDown, { capture: true });
      window.removeEventListener('keyup', onKeyUp, { capture: true });
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('pointerdown', physicalPointerDown, true);
      window.removeEventListener('pointerup', physicalPointerUp, true);
      window.removeEventListener('pointercancel', physicalPointerUp, true);
      window.removeEventListener('pagehide', onBlur);
    }
  });

  return {
    state,
    recovery,
    recoveryMessage,
    isDriving,
    onPointerDown,
    onPointerUp,
    onPointerCancel,
    onPointerLeave,
    resetAllInput,
    clearDirection,
  };
}
