<template>
  <div class="teleop-feedback" role="region" aria-label="Command State and Diagnostics">
    <!-- Telemetry State Summary -->
    <div class="command-status-row">
      <div class="status-item">
        <span class="status-k">Command:</span>
        <span class="status-v" :class="{ 'v-active': activeDirection !== 'neutral' && activeDirection !== 'stop' }">
          {{ commandLabel }}
        </span>
      </div>

      <div class="status-divider" aria-hidden="true">|</div>

      <div class="status-item">
        <span class="status-k">Delivery:</span>
        <span class="status-v">{{ deliveryLabel }}</span>
      </div>

      <div class="status-divider" aria-hidden="true">|</div>

      <div class="status-item">
        <span class="status-k">Last update:</span>
        <span class="status-v">{{ lastUpdateDisplay }}</span>
      </div>
    </div>

    <!-- Hold Cap Progress Bar (0 to 5s limit) -->
    <div v-if="holdProgressPercent > 0" class="hold-cap-container" role="progressbar" :aria-valuenow="holdProgressPercent" aria-valuemin="0" aria-valuemax="100">
      <div class="hold-cap-bar" :style="{ width: `${holdProgressPercent}%` }"></div>
      <span class="hold-cap-text">Hold limit: {{ (continuousHoldDurationMs / 1000).toFixed(1) }}s / 5.0s</span>
    </div>

    <!-- Modals Action Buttons -->
    <div class="dialog-actions">
      <button class="btn btn-sm" @click="emit('open-logs')" aria-label="Open Recent Logs Dialog">
        📋 Recent logs
      </button>
      <button class="btn btn-sm" @click="emit('open-diagnostics')" aria-label="Open System Diagnostics Dialog">
        🔍 Diagnostics
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { MotionDirection } from '../types/api';

const props = defineProps<{
  activeDirection: MotionDirection;
  isDriving: boolean;
  linearSpeed: number;
  angularSpeed: number;
  lastUpdateTimestamp: number | null;
  continuousHoldDurationMs: number;
}>();

const emit = defineEmits<{
  (e: 'open-logs'): void;
  (e: 'open-diagnostics'): void;
}>();

const commandLabel = computed(() => {
  switch (props.activeDirection) {
    case 'forward':
      return 'forward (0.20 m/s)';
    case 'reverse':
      return 'reverse (-0.20 m/s)';
    case 'spin_left':
      return 'spin left (0.50 rad/s)';
    case 'spin_right':
      return 'spin right (-0.50 rad/s)';
    case 'stop':
      return 'stop';
    default:
      return 'zero';
  }
});

const deliveryLabel = computed(() => {
  if (props.isDriving) {
    return 'active (20 Hz)';
  }
  return 'idle';
});

const lastUpdateDisplay = computed(() => {
  if (!props.lastUpdateTimestamp) return '--';
  const ageSec = Math.max(0, (Date.now() - props.lastUpdateTimestamp) / 1000);
  return `${ageSec.toFixed(1)} s ago`;
});

const holdProgressPercent = computed(() => {
  if (props.continuousHoldDurationMs <= 0) return 0;
  return Math.min(100, (props.continuousHoldDurationMs / 5000) * 100);
});
</script>

<style scoped>
.teleop-feedback {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
}

.command-status-row {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.85rem;
  color: #94a3b8;
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 0.375rem;
  padding: 0.4rem 0.75rem;
}

.status-item {
  display: flex;
  gap: 0.35rem;
}

.status-k {
  color: #64748b;
}

.status-v {
  color: #f1f5f9;
  font-weight: 600;
  font-family: monospace;
}

.v-active {
  color: #38bdf8;
}

.status-divider {
  color: #334155;
}

.hold-cap-container {
  width: 100%;
  max-width: 320px;
  height: 18px;
  background: #334155;
  border-radius: 9999px;
  overflow: hidden;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}

.hold-cap-bar {
  position: absolute;
  left: 0;
  top: 0;
  height: 100%;
  background: #38bdf8;
  transition: width 0.05s linear;
}

.hold-cap-text {
  position: relative;
  font-size: 0.65rem;
  font-weight: 700;
  color: #f8fafc;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
}

.dialog-actions {
  display: flex;
  gap: 0.75rem;
}

.btn-sm {
  background: #1e293b;
  color: #cbd5e1;
  border: 1px solid #334155;
  border-radius: 0.375rem;
  padding: 0.35rem 0.65rem;
  font-size: 0.8rem;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.btn-sm:hover {
  background: #334155;
  color: #ffffff;
}

.btn-sm:focus-visible {
  outline: 2px solid #38bdf8;
  outline-offset: 2px;
}
</style>

