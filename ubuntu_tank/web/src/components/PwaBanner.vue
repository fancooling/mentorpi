<template>
  <div class="pwa-banners" role="region" aria-label="System Notifications and Alerts">
    <!-- Offline Alert Banner -->
    <div v-if="isOffline" class="banner banner-offline" role="alert">
      <div class="banner-content">
        <span class="banner-icon" aria-hidden="true">📡</span>
        <span class="banner-text">
          <strong>Offline Mode:</strong> Pi is unreachable. Teleoperation driving is disabled and live telemetry is paused.
        </span>
      </div>
    </div>

    <!-- Protocol Incompatible Banner -->
    <div v-if="!isProtocolCompatible" class="banner banner-incompatible" role="alert">
      <div class="banner-content">
        <span class="banner-icon" aria-hidden="true">⚠️</span>
        <span class="banner-text">
          <strong>Incompatible Protocol:</strong> Connected server protocol version is incompatible with this cached client. Arming and driving are disabled. Please reload to update.
        </span>
      </div>
      <button class="btn-banner" @click="reloadPage">Reload</button>
    </div>

    <!-- Service Worker Update Banner -->
    <div v-if="needRefresh" class="banner banner-update" role="alert">
      <div class="banner-content">
        <span class="banner-icon" aria-hidden="true">🚀</span>
        <span class="banner-text">
          <strong>Update Available:</strong> A new web control release is ready. Updating will disarm and reload the application.
        </span>
      </div>
      <button class="btn-banner btn-update" @click="emit('apply-update')">
        Update Now
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  isOffline: boolean;
  isProtocolCompatible: boolean;
  needRefresh: boolean;
}>();

const emit = defineEmits<{
  (e: 'apply-update'): void;
}>();

function reloadPage() {
  if (typeof window !== 'undefined') {
    window.location.reload();
  }
}
</script>

<style scoped>
.pwa-banners {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  width: 100%;
}

.banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.6rem 1rem;
  border-radius: 0.375rem;
  font-size: 0.85rem;
  gap: 1rem;
}

.banner-content {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.banner-icon {
  font-size: 1.1rem;
}

.banner-offline {
  background: rgba(239, 68, 68, 0.2);
  border: 1px solid #ef4444;
  color: #fca5a5;
}

.banner-incompatible {
  background: rgba(245, 158, 11, 0.2);
  border: 1px solid #f59e0b;
  color: #fde68a;
}

.banner-update {
  background: rgba(56, 189, 248, 0.2);
  border: 1px solid #38bdf8;
  color: #e0f2fe;
}

.btn-banner {
  background: #334155;
  color: #f8fafc;
  border: 1px solid #475569;
  border-radius: 0.25rem;
  padding: 0.3rem 0.6rem;
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.btn-update {
  background: #0284c7;
  border-color: #38bdf8;
}

.btn-update:hover {
  background: #0369a1;
}
</style>

