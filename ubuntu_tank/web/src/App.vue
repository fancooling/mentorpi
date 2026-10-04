<template>
  <div id="mentorpi-app" class="app-root">
    <!-- Top System Header -->
    <HeaderBar
      :telemetry="telemetry"
      :my-operator-id="operatorId"
    />

    <main class="main-container" role="main">
      <!-- Standalone Review Mode Banner -->
      <div v-if="isReviewMode" class="review-banner" role="status">
        <div class="review-banner-text">
          <strong class="review-badge">Review Mode</strong>
          <span>Camera UI review active. Teleoperation controls are inert; no robot connection required.</span>
        </div>
        <a href="/" class="review-exit-link">Normal Mode</a>
      </div>

      <!-- PWA Status / Offline / Update Banners -->
      <PwaBanner
        :is-offline="!isReviewMode && (isOffline || telemetry.connectionStatus === 'offline')"
        :is-protocol-compatible="telemetry.isProtocolCompatible"
        :need-refresh="needRefresh"
        @apply-update="onApplyPwaUpdate"
      />

      <!-- Service Lifecycle & Control Ownership -->
      <ServiceControls
        :has-session="!isReviewMode && hasSession"
        :is-releasing="!isReviewMode && isReleasing"
        :is-operating="!isReviewMode && (isOperating || isStopping)"
        :is-protocol-compatible="telemetry.isProtocolCompatible"
        :feedback="feedback"
        @take-control="handleTakeControl"
        @release-control="handleReleaseControl"
        @clear-feedback="clearFeedback"
      />

      <!-- Dual-Panel Console Layout: Camera beside Drive on desktop, above Drive on narrow screens -->
      <div class="console-layout">
        <CameraPanel
          class="console-camera"
          :is-review-mode="isReviewMode"
        />

        <DrivePanel
          class="console-drive"
          :is-owner="!isReviewMode && isOwner"
          :is-armed="!isReviewMode && isArmed"
          :active-direction="driveState.activeDirection"
          :recovery-message="isReviewMode ? '' : recoveryMessage"
          :is-ready="!isReviewMode && recovery === 'ready' && isBound && !isOperating && !isStopping && !telemetry.releaseProgress"
          :show-start="showStart"
          :can-start="canStart"
          @start="handleStart"
          @pointer-down="onPointerDown"
          @pointer-up="onPointerUp"
          @pointer-cancel="onPointerCancel"
          @pointer-leave="onPointerLeave"
          @emergency-stop="handleEmergencyStop"
          @focus-change="(focused) => (driveState.isPanelFocused = focused)"
        />
      </div>

      <!-- Telemetry Command State & Diagnostics Links -->
      <TeleopFeedback
        :active-direction="driveState.activeDirection"
        :is-driving="isDriving"
        :linear-speed="telemetry.linearSpeed"
        :angular-speed="telemetry.angularSpeed"
        :last-update-timestamp="telemetry.lastUpdateTimestamp"
        :continuous-hold-duration-ms="driveState.continuousHoldDurationMs"
        @open-logs="isLogsOpen = true"
        @open-diagnostics="isDiagOpen = true"
      />
    </main>

    <!-- Modals -->
    <LogsModal :is-open="isLogsOpen" @close="isLogsOpen = false" />
    <DiagnosticsModal
      :is-open="isDiagOpen"
      :telemetry="telemetry"
      @close="isDiagOpen = false"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import HeaderBar from './components/HeaderBar.vue';
import ServiceControls from './components/ServiceControls.vue';
import DrivePanel from './components/DrivePanel.vue';
import CameraPanel from './components/CameraPanel.vue';
import TeleopFeedback from './components/TeleopFeedback.vue';
import LogsModal from './components/LogsModal.vue';
import DiagnosticsModal from './components/DiagnosticsModal.vue';
import PwaBanner from './components/PwaBanner.vue';

import { useRobotState } from './composables/useRobotState';
import { useControlSession } from './composables/useControlSession';
import { useDriveInput } from './composables/useDriveInput';
import { usePwaUpdate } from './composables/usePwaUpdate';

// Check standalone review mode (?review=camera, ?review=1, ?mode=review)
const isReviewMode = typeof window !== 'undefined' && (
  new URLSearchParams(window.location.search).has('review') ||
  new URLSearchParams(window.location.search).get('mode') === 'review'
);

// Unique operator ID generated in memory per browser tab
const operatorId = 'web-tab-' + Math.random().toString(36).substring(2, 10);

// Composables (dormant in review mode to prevent any backend requests)
const { telemetry, isOwner, isArmed, isControllerRunning, pollStatus } = useRobotState(operatorId, isReviewMode);

const {
  isBound,
  hasSession,
  isOperating,
  isStopping,
  feedback,
  clearFeedback,
  takeControl,
  releaseControl,
  start,
  isReleasing,
  emergencyStop,
} = useControlSession(
  operatorId,
  pollStatus,
  () => telemetry.isProtocolCompatible,
  () => telemetry.currentEpoch,
  () => telemetry.activeOwner,
  () => telemetry.sessionId,
  () => telemetry.lastReleaseReason
);

async function handleTakeControl(): Promise<boolean> {
  if (isReviewMode) {
    feedback.value = {
      type: 'info',
      message: 'Driving controls are inert in standalone review mode.',
      timestamp: Date.now(),
    };
    return false;
  }
  return takeControl();
}

async function handleEmergencyStop(): Promise<boolean> {
  resetAllInput();
  if (isReviewMode) {
    return true;
  }
  return emergencyStop();
}

const showStart = computed(() => !isReviewMode && isOwner.value && isBound.value &&
  telemetry.operatorState === 'OWNED_DISARMED' && !telemetry.guardArmed &&
  !telemetry.disarmPending && !isOperating.value && !isStopping.value && !telemetry.releaseProgress);
const canStart = computed(() => !isReviewMode && showStart.value && isControllerRunning.value && telemetry.isProtocolCompatible);

async function handleStart(): Promise<boolean> {
  if (isReviewMode || !canStart.value) return false;
  resetAllInput();
  return start();
}

async function handleReleaseControl(): Promise<boolean> {
  resetAllInput();
  if (isReviewMode) return false;
  return releaseControl();
}

const {
  state: driveState,
  isDriving,
  recovery,
  recoveryMessage,
  onPointerDown,
  onPointerUp,
  onPointerCancel,
  onPointerLeave,
  resetAllInput,
} = useDriveInput(
  () => !isReviewMode && isArmed.value,
  () => !isReviewMode && isOwner.value && isBound.value && !isOperating.value && !isStopping.value && !telemetry.releaseProgress,
  handleEmergencyStop
);

const { isOffline, needRefresh, applyUpdate } = usePwaUpdate();

// Modals
const isLogsOpen = ref(false);
const isDiagOpen = ref(false);

async function onApplyPwaUpdate() {
  await applyUpdate(async () => {
    if (hasSession.value) {
      return handleReleaseControl();
    } else {
      return handleEmergencyStop();
    }
  });
}
</script>

<style>
/* Global CSS resets and theme variables */
*, *::before, *::after {
  box-sizing: border-box;
}

html, body {
  margin: 0;
  padding: 0;
  background-color: #0f172a;
  color: #f8fafc;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  min-height: 100vh;
  overflow-x: hidden;
}

#app {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
</style>

<style scoped>
.app-root {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: #0f172a;
}

.main-container {
  flex: 1;
  width: 100%;
  max-width: 600px;
  margin: 0 auto;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  transition: max-width 0.2s ease;
}

@media (min-width: 900px) {
  .main-container {
    max-width: 1100px;
  }
}

/* Dual-Panel Console Layout */
.console-layout {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.85rem;
  width: 100%;
}

@media (min-width: 900px) {
  .console-layout {
    grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr);
    align-items: start;
  }
}

/* Standalone Review Banner */
.review-banner {
  background: #064e3b;
  border: 1px solid #059669;
  border-radius: 0.5rem;
  padding: 0.6rem 0.85rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  font-size: 0.8rem;
  color: #a7f3d0;
}

.review-banner-text {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.review-badge {
  background: #10b981;
  color: #064e3b;
  padding: 0.15rem 0.4rem;
  border-radius: 0.25rem;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.review-exit-link {
  color: #6ee7b7;
  font-weight: 600;
  text-decoration: underline;
  white-space: nowrap;
}
</style>
