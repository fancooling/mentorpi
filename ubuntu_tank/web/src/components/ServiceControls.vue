<template>
  <section class="service-controls" aria-label="System and Operator Authority Controls">
    <!-- Row 1: Controller Service Lifecycle & Control Ownership -->
    <div class="control-row">
      <div class="button-group" role="group" aria-label="Control Authority Actions">
        <button
          v-if="!hasSession"
          class="btn btn-primary"
          :disabled="isOperating || !isProtocolCompatible"
          @click="onTakeControl"
          aria-label="Take Control Authority"
        >
          {{ isOperating ? 'Taking control…' : 'Take control' }}
        </button>
        <button
          v-else
          class="btn btn-warning"
          :disabled="isReleasing"
          @click="onReleaseControl"
          aria-label="Release Control Authority"
        >
          Release control
        </button>
      </div>
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
import type { OperationFeedback } from '../types/ui';

/** Ownership actions remain separate from the direction pad's Start / Stop. */
defineProps<{
  hasSession: boolean;
  isOperating: boolean;
  isReleasing: boolean;
  isProtocolCompatible: boolean;
  feedback: OperationFeedback | null;
}>();
const emit = defineEmits<{
  (e: 'take-control'): void;
  (e: 'release-control'): void;
  (e: 'clear-feedback'): void;
}>();
const onTakeControl = () => emit('take-control');
const onReleaseControl = () => emit('release-control');
const onClearFeedback = () => emit('clear-feedback');
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

</style>

