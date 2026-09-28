<template>
  <div id="mentorpi-app" class="app-root">
    <!-- Top System Header -->
    <HeaderBar
      :telemetry="telemetry"
      :my-operator-id="operatorId"
    />

    <main class="main-container" role="main">
      <!-- PWA Status / Offline / Update Banners -->
      <PwaBanner
        :is-offline="isOffline || telemetry.connectionStatus === 'offline'"
        :is-protocol-compatible="telemetry.isProtocolCompatible"
        :need-refresh="needRefresh"
        @apply-update="onApplyPwaUpdate"
      />

      <!-- Service Lifecycle & Control Ownership -->
      <ServiceControls
        :is-owner="isOwner"
        :is-armed="isArmed"
        :is-controller-running="isControllerRunning"
        :is-bound="isBound"
        :is-operating="isOperating"
        :is-protocol-compatible="telemetry.isProtocolCompatible"
        :feedback="feedback"
        @stop-controller="handleStopController"
        @take-control="takeControl"
        @release-control="handleReleaseControl"
        @arm="arm"
        @disarm="handleDisarm"
        @clear-feedback="clearFeedback"
      />

      <!-- Teleoperation Driving Pad -->
      <DrivePanel
        :is-owner="isOwner"
        :is-armed="isArmed"
        :active-direction="driveState.activeDirection"
        :recovery-message="recoveryMessage"
        :is-ready="recovery === 'ready' && isBound"
        @pointer-down="onPointerDown"
        @pointer-up="onPointerUp"
        @pointer-cancel="onPointerCancel"
        @pointer-leave="onPointerLeave"
        @emergency-stop="handleEmergencyStop"
        @focus-change="(focused) => (driveState.isPanelFocused = focused)"
      />

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
import { ref } from 'vue';
import HeaderBar from './components/HeaderBar.vue';
import ServiceControls from './components/ServiceControls.vue';
import DrivePanel from './components/DrivePanel.vue';
import TeleopFeedback from './components/TeleopFeedback.vue';
import LogsModal from './components/LogsModal.vue';
import DiagnosticsModal from './components/DiagnosticsModal.vue';
import PwaBanner from './components/PwaBanner.vue';

import { useRobotState } from './composables/useRobotState';
import { useControlSession } from './composables/useControlSession';
import { useDriveInput } from './composables/useDriveInput';
import { usePwaUpdate } from './composables/usePwaUpdate';

// Unique operator ID generated in memory per browser tab
const operatorId = 'web-tab-' + Math.random().toString(36).substring(2, 10);

// Composables
const { telemetry, isOwner, isArmed, isControllerRunning, pollStatus } = useRobotState(operatorId);

const {
  isBound,
  isOperating,
  feedback,
  clearFeedback,
  takeControl,
  releaseControl,
  arm,
  disarm,
  emergencyStop,
  stopController,
} = useControlSession(
  operatorId,
  pollStatus,
  () => telemetry.isProtocolCompatible,
  () => telemetry.currentEpoch,
  () => telemetry.activeOwner
);

async function handleEmergencyStop(): Promise<boolean> {
  resetAllInput();
  return emergencyStop();
}

async function handleStopController(): Promise<boolean> {
  resetAllInput();
  return stopController();
}
async function handleReleaseControl(): Promise<boolean> {
  resetAllInput();
  return releaseControl();
}
async function handleDisarm(): Promise<boolean> {
  resetAllInput();
  return disarm();
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
  () => isArmed.value,
  () => isOwner.value,
  handleEmergencyStop
);

const { isOffline, needRefresh, applyUpdate } = usePwaUpdate();

// Modals
const isLogsOpen = ref(false);
const isDiagOpen = ref(false);

async function onApplyPwaUpdate() {
  await applyUpdate(async () => {
    if (isOwner.value) {
      await releaseControl();
    } else {
      await emergencyStop();
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
}
</style>

