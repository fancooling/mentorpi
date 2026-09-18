<template>
  <section class="service-controls" aria-label="System and Operator Authority Controls">
    <!-- Row 1: Controller Service Lifecycle & Control Ownership -->
    <div class="control-row">
      <div class="button-group" role="group" aria-label="Controller Service Actions">
        <button
          class="btn btn-secondary"
          :disabled="isOperating || isControllerRunning"
          @click="onStartController"
          aria-label="Start Controller Service"
        >
          Start controller
        </button>
        <button
          class="btn btn-secondary"
          :disabled="isOperating || !isControllerRunning"
          @click="onStopController"
          aria-label="Stop Controller Service"
        >
          Stop controller
        </button>
      </div>

      <div class="button-group" role="group" aria-label="Control Authority Actions">
        <button
          v-if="!isOwner"
          class="btn btn-primary"
          :disabled="isOperating || !isControllerRunning || !isProtocolCompatible"
          @click="onTakeControl"
          aria-label="Take Control Authority"
        >
          Take control
        </button>
        <button
          v-else
          class="btn btn-warning"
          :disabled="isOperating"
          @click="onReleaseControl"
          aria-label="Release Control Authority"
        >
          Release control
        </button>
      </div>
    </div>

    <!-- Row 2: Physical Safety Gate Checkbox -->
    <div class="safety-gate-card">
      <label class="safety-checkbox-label">
        <input
          type="checkbox"
          class="safety-checkbox"
          :checked="tracksRaisedConfirmed"
          :disabled="!isOwner || isArmed || !isProtocolCompatible"
          @change="onToggleTracksRaised(($event.target as HTMLInputElement).checked)"
          aria-describedby="safety-desc"
        />
        <span class="safety-text">
          I confirm the tracks are raised clear of ground and the power disconnect is accessible
        </span>
      </label>
      <div id="safety-desc" class="sr-only">
        Physical bench testing requirement: tracks must be elevated to prevent uncontrolled movement.
      </div>
    </div>

    <!-- Row 3: Arm / Disarm Controls -->
    <div class="arm-row">
      <button
        class="btn btn-arm"
        :disabled="!canArm"
        @click="onArm"
        aria-label="Arm Chassis Motors"
      >
        <span class="btn-icon" aria-hidden="true">⚡</span>
        Arm
      </button>
      <button
        class="btn btn-disarm"
        :disabled="!canDisarm"
        @click="onDisarm"
        aria-label="Disarm Chassis Motors"
      >
        <span class="btn-icon" aria-hidden="true">🛑</span>
        Disarm
      </button>
    </div>

    <!-- User Feedback Banner -->
    <div
      v-if="feedback"
      class="feedback-banner"
      :class="`feedback-${feedback.type}`"
      role="alert"
    >
      <span class="feedback-msg">{{ feedback.message }}</span>
      <button class="feedback-close" @click="onClearFeedback" aria-label="Dismiss message">
        &times;
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { OperationFeedback } from '../types/ui';

const props = withDefaults(
  defineProps<{
    isOwner: boolean;
    isArmed: boolean;
    isControllerRunning: boolean;
    tracksRaisedConfirmed: boolean;
    isOperating: boolean;
    isProtocolCompatible?: boolean;
    feedback: OperationFeedback | null;
  }>(),
  {
    isProtocolCompatible: true,
  }
);

const emit = defineEmits<{
  (e: 'start-controller'): void;
  (e: 'stop-controller'): void;
  (e: 'take-control'): void;
  (e: 'release-control'): void;
  (e: 'update:tracksRaisedConfirmed', value: boolean): void;
  (e: 'arm'): void;
  (e: 'disarm'): void;
  (e: 'clear-feedback'): void;
}>();

const canArm = computed(() => {
  return (
    props.isOwner &&
    props.isControllerRunning &&
    props.tracksRaisedConfirmed &&
    props.isProtocolCompatible &&
    !props.isArmed &&
    !props.isOperating
  );
});

const canDisarm = computed(() => {
  return props.isArmed && !props.isOperating;
});

function onStartController() {
  emit('start-controller');
}

function onStopController() {
  emit('stop-controller');
}

function onTakeControl() {
  emit('take-control');
}

function onReleaseControl() {
  emit('release-control');
}

function onToggleTracksRaised(checked: boolean) {
  emit('update:tracksRaisedConfirmed', checked);
}

function onArm() {
  emit('arm');
}

function onDisarm() {
  emit('disarm');
}

function onClearFeedback() {
  emit('clear-feedback');
}
</script>

<style scoped>
.service-controls {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 0.5rem;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.control-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.button-group {
  display: flex;
  gap: 0.5rem;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  font-size: 0.875rem;
  font-weight: 600;
  padding: 0.5rem 0.85rem;
  border-radius: 0.375rem;
  border: 1px solid transparent;
  cursor: pointer;
  min-height: 44px;
  transition: background-color 0.15s ease, opacity 0.15s ease, border-color 0.15s ease;
  user-select: none;
}

.btn:focus-visible {
  outline: 2px solid #38bdf8;
  outline-offset: 2px;
}

.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn-secondary {
  background: #334155;
  color: #f1f5f9;
  border-color: #475569;
}
.btn-secondary:hover:not(:disabled) {
  background: #475569;
}

.btn-primary {
  background: #0284c7;
  color: #ffffff;
}
.btn-primary:hover:not(:disabled) {
  background: #0369a1;
}

.btn-warning {
  background: #d97706;
  color: #ffffff;
}
.btn-warning:hover:not(:disabled) {
  background: #b45309;
}

.safety-gate-card {
  background: rgba(15, 23, 42, 0.6);
  border: 1px solid rgba(245, 158, 11, 0.4);
  border-radius: 0.375rem;
  padding: 0.65rem 0.75rem;
}

.safety-checkbox-label {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 500;
  color: #f1f5f9;
}

.safety-checkbox {
  width: 1.25rem;
  height: 1.25rem;
  accent-color: #f59e0b;
  cursor: pointer;
}

.safety-checkbox:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.arm-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}

.btn-arm {
  background: #15803d;
  color: #ffffff;
  font-size: 1rem;
}
.btn-arm:hover:not(:disabled) {
  background: #166534;
}

.btn-disarm {
  background: #b91c1c;
  color: #ffffff;
  font-size: 1rem;
}
.btn-disarm:hover:not(:disabled) {
  background: #991b1b;
}

.feedback-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  border-radius: 0.375rem;
  font-size: 0.85rem;
  font-weight: 500;
}

.feedback-success {
  background: rgba(34, 197, 94, 0.15);
  border: 1px solid #22c55e;
  color: #4ade80;
}

.feedback-error {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid #ef4444;
  color: #f87171;
}

.feedback-warning {
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid #f59e0b;
  color: #fbbf24;
}

.feedback-info {
  background: rgba(56, 189, 248, 0.15);
  border: 1px solid #38bdf8;
  color: #38bdf8;
}

.feedback-close {
  background: transparent;
  border: none;
  color: inherit;
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0 0.25rem;
  line-height: 1;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}
</style>

