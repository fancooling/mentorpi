<template>
  <div v-if="isOpen" class="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="logs-title" @keydown.esc="emit('close')">
    <div class="modal-content">
      <div class="modal-header">
        <h2 id="logs-title" class="modal-title">Recent Controller & Web Logs</h2>
        <div class="header-actions">
          <button class="btn-icon" :disabled="isLoading" @click="fetchLogs" aria-label="Refresh Logs">
            🔄
          </button>
          <button class="btn-close" @click="emit('close')" aria-label="Close Logs Dialog">
            &times;
          </button>
        </div>
      </div>

      <div class="modal-body">
        <div v-if="isLoading && logLines.length === 0" class="loading-state">
          Loading logs...
        </div>
        <div v-else-if="error" class="error-state">
          {{ error }}
        </div>
        <pre v-else class="logs-container"><code>{{ logText }}</code></pre>
      </div>

      <div class="modal-footer">
        <span class="footer-info">{{ logLines.length }} lines rendered</span>
        <button class="btn btn-secondary" @click="emit('close')">Close</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { apiClient } from '../services/apiClient';

const props = defineProps<{
  isOpen: boolean;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const logLines = ref<string[]>([]);
const isLoading = ref(false);
const error = ref<string | null>(null);

const logText = computed(() => {
  if (logLines.value.length === 0) return 'No log records available.';
  return logLines.value.join('\n');
});

async function fetchLogs() {
  isLoading.value = true;
  error.value = null;
  try {
    const res = await apiClient.getLogs(50);
    logLines.value = res.lines || [];
  } catch (err: any) {
    error.value = err.message || 'Failed to load logs';
  } finally {
    isLoading.value = false;
  }
}

watch(
  () => props.isOpen,
  (open) => {
    if (open) {
      fetchLogs();
    }
  }
);
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
  max-width: 720px;
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

.header-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.btn-icon,
.btn-close {
  background: transparent;
  border: none;
  color: #94a3b8;
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0.25rem;
  line-height: 1;
  border-radius: 0.25rem;
}

.btn-icon:hover,
.btn-close:hover {
  color: #f8fafc;
  background: #334155;
}

.modal-body {
  flex: 1;
  overflow: hidden;
  padding: 0.75rem 1rem;
  display: flex;
}

.logs-container {
  flex: 1;
  margin: 0;
  padding: 0.75rem;
  background: #0f172a;
  border: 1px solid #020617;
  border-radius: 0.375rem;
  overflow-y: auto;
  font-family: monospace;
  font-size: 0.8rem;
  color: #cbd5e1;
  white-space: pre-wrap;
  word-break: break-all;
}

.loading-state,
.error-state {
  padding: 2rem;
  text-align: center;
  color: #94a3b8;
  width: 100%;
}

.error-state {
  color: #f87171;
}

.modal-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1rem;
  border-top: 1px solid #334155;
}

.footer-info {
  font-size: 0.8rem;
  color: #64748b;
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

