<template>
  <div v-if="isOpen" class="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="diag-title" @keydown.esc="emit('close')">
    <div class="modal-content">
      <div class="modal-header">
        <h2 id="diag-title" class="modal-title">Subsystem Diagnostics & Telemetry</h2>
        <button class="btn-close" @click="emit('close')" aria-label="Close Diagnostics Dialog">
          &times;
        </button>
      </div>

      <div class="modal-body">
        <div class="diag-section">
          <h3 class="section-title">Release & Protocol</h3>
          <div class="diag-grid">
            <div class="diag-item">
              <span class="diag-k">Release ID:</span>
              <span class="diag-v">{{ telemetry.releaseId }}</span>
            </div>
            <div class="diag-item">
              <span class="diag-k">Protocol Version:</span>
              <span class="diag-v">{{ telemetry.protocolVersion }}</span>
            </div>
            <div class="diag-item">
              <span class="diag-k">Compatibility:</span>
              <span class="diag-v" :class="telemetry.isProtocolCompatible ? 'text-ok' : 'text-err'">
                {{ telemetry.isProtocolCompatible ? 'Compatible (1.x)' : 'Incompatible!' }}
              </span>
            </div>
            <div class="diag-item">
              <span class="diag-k">Active Epoch:</span>
              <span class="diag-v">{{ telemetry.currentEpoch ?? 'None' }}</span>
            </div>
          </div>
        </div>

        <div class="diag-section">
          <h3 class="section-title">Motion Limits & Speeds</h3>
          <div class="diag-grid">
            <div class="diag-item">
              <span class="diag-k">Configured Linear:</span>
              <span class="diag-v">{{ telemetry.linearSpeed.toFixed(2) }} m/s</span>
            </div>
            <div class="diag-item">
              <span class="diag-k">Configured Angular:</span>
              <span class="diag-v">{{ telemetry.angularSpeed.toFixed(2) }} rad/s</span>
            </div>
            <div v-for="(val, limKey) in telemetry.limits" :key="limKey" class="diag-item">
              <span class="diag-k">Limit ({{ limKey }}):</span>
              <span class="diag-v">{{ val }}</span>
            </div>
          </div>
        </div>

        <div class="diag-section">
          <h3 class="section-title">Topic & Sensor Freshness (Age in seconds)</h3>
          <div class="diag-grid">
            <div v-for="(age, topic) in telemetry.freshness" :key="topic" class="diag-item">
              <span class="diag-k">{{ topic }}:</span>
              <span class="diag-v" :class="age !== null && age < 1.0 ? 'text-ok' : 'text-warn'">
                {{ age !== null ? `${age.toFixed(2)} s` : 'Stale / Missing' }}
              </span>
            </div>
          </div>
        </div>

        <div v-if="telemetry.lastFault" class="diag-section fault-section">
          <h3 class="section-title text-err">Last Recorded Fault</h3>
          <p class="fault-text">{{ telemetry.lastFault }}</p>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn btn-secondary" @click="emit('close')">Close</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { UiTelemetryState } from '../types/ui';

defineProps<{
  isOpen: boolean;
  telemetry: UiTelemetryState;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 1rem;
}

.modal-content {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 0.5rem;
  width: 100%;
  max-width: 640px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #334155;
}

.modal-title {
  font-size: 1.1rem;
  font-weight: 700;
  margin: 0;
  color: #f1f5f9;
}

.btn-close {
  background: transparent;
  border: none;
  color: #94a3b8;
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0.25rem;
  line-height: 1;
}

.btn-close:hover {
  color: #f8fafc;
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.diag-section {
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 0.375rem;
  padding: 0.75rem;
}

.section-title {
  font-size: 0.85rem;
  font-weight: 700;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0 0 0.5rem 0;
}

.diag-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.5rem;
}

.diag-item {
  display: flex;
  justify-content: space-between;
  font-size: 0.85rem;
  padding: 0.2rem 0;
}

.diag-k {
  color: #64748b;
}

.diag-v {
  color: #f8fafc;
  font-family: monospace;
  font-weight: 600;
}

.text-ok { color: #22c55e; }
.text-warn { color: #f59e0b; }
.text-err { color: #ef4444; }

.fault-section {
  border-color: rgba(239, 68, 68, 0.4);
  background: rgba(239, 68, 68, 0.05);
}

.fault-text {
  margin: 0;
  color: #f87171;
  font-family: monospace;
  font-size: 0.85rem;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  padding: 0.75rem 1rem;
  border-top: 1px solid #334155;
}

.btn-secondary {
  background: #334155;
  color: #f1f5f9;
  border: 1px solid #475569;
  padding: 0.4rem 0.85rem;
  border-radius: 0.375rem;
  font-size: 0.85rem;
  cursor: pointer;
}

.btn-secondary:hover {
  background: #475569;
}
</style>

