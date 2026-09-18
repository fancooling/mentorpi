<template>
  <header class="header-bar" role="banner">
    <div class="brand-row">
      <div class="brand-title">
        <span class="tank-icon" aria-hidden="true">🛡️</span>
        <h1 class="title-text">MentorPi Tank</h1>
      </div>
      <div class="mode-badge" role="status" aria-label="Operating Mode">
        <span class="badge-dot" aria-hidden="true"></span>
        Raised-track mode
      </div>
    </div>

    <div class="status-grid" role="region" aria-label="System Status Indicators">
      <!-- Connection Status -->
      <div
        class="status-pill"
        :class="`conn-${telemetry.connectionStatus}`"
        role="status"
        aria-label="Connection Status"
      >
        <span class="indicator-dot" aria-hidden="true"></span>
        <span class="pill-label">Network:</span>
        <span class="pill-value">{{ connectionLabel }}</span>
      </div>

      <!-- Controller Service Status -->
      <div
        class="status-pill"
        :class="`service-${telemetry.serviceState}`"
        role="status"
        aria-label="Controller Service State"
      >
        <span class="indicator-dot" aria-hidden="true"></span>
        <span class="pill-label">Controller:</span>
        <span class="pill-value">{{ serviceLabel }}</span>
      </div>

      <!-- Guard / Arm Status -->
      <div
        class="status-pill"
        :class="`guard-${guardStateClass}`"
        role="status"
        aria-label="Safety Guard State"
      >
        <span class="indicator-dot" aria-hidden="true"></span>
        <span class="pill-label">Safety:</span>
        <span class="pill-value">{{ guardLabel }}</span>
      </div>

      <!-- Battery Voltage -->
      <div
        class="status-pill"
        :class="batteryClass"
        role="status"
        aria-label="Chassis Battery Voltage"
      >
        <span class="pill-icon" aria-hidden="true">🔋</span>
        <span class="pill-label">Battery:</span>
        <span class="pill-value">{{ batteryDisplay }}</span>
      </div>

      <!-- Control Owner -->
      <div
        class="status-pill"
        :class="ownerClass"
        role="status"
        aria-label="Active Control Slot"
      >
        <span class="pill-icon" aria-hidden="true">🎮</span>
        <span class="pill-label">Owner:</span>
        <span class="pill-value">{{ ownerDisplay }}</span>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { UiTelemetryState } from '../types/ui';

const props = defineProps<{
  telemetry: UiTelemetryState;
  myOperatorId: string;
}>();

const connectionLabel = computed(() => {
  switch (props.telemetry.connectionStatus) {
    case 'connected':
      return 'Connected';
    case 'offline':
      return 'Offline';
    default:
      return 'Disconnected';
  }
});

const serviceLabel = computed(() => {
  switch (props.telemetry.serviceState) {
    case 'active':
      return 'Running';
    case 'inactive':
      return 'Stopped';
    case 'failed':
      return 'Failed';
    default:
      return 'Unknown';
  }
});

const guardStateClass = computed(() => {
  if (props.telemetry.lastFault) return 'fault';
  if (props.telemetry.operatorState === 'ARMING') return 'arming';
  if (props.telemetry.guardArmed) return 'armed';
  return 'disarmed';
});

const guardLabel = computed(() => {
  if (props.telemetry.lastFault) return `Fault: ${props.telemetry.lastFault}`;
  if (props.telemetry.operatorState === 'ARMING') return 'Arming...';
  if (props.telemetry.guardArmed) return 'Armed';
  return 'Disarmed';
});

const batteryDisplay = computed(() => {
  if (props.telemetry.batteryVoltage === null || props.telemetry.connectionStatus !== 'connected') {
    return '-- V';
  }
  return `${props.telemetry.batteryVoltage.toFixed(1)} V`;
});

const batteryClass = computed(() => {
  const v = props.telemetry.batteryVoltage;
  if (v === null || props.telemetry.connectionStatus !== 'connected') return 'battery-unknown';
  if (v >= 11.5) return 'battery-good';
  if (v >= 10.8) return 'battery-warn';
  return 'battery-crit';
});

const ownerDisplay = computed(() => {
  if (!props.telemetry.activeOwner) return 'No owner';
  if (props.telemetry.activeOwner === props.myOperatorId) return 'This tab';
  return `Other (${props.telemetry.activeOwner.slice(0, 8)}...)`;
});

const ownerClass = computed(() => {
  if (!props.telemetry.activeOwner) return 'owner-none';
  if (props.telemetry.activeOwner === props.myOperatorId) return 'owner-self';
  return 'owner-other';
});
</script>

<style scoped>
.header-bar {
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
  padding: 0.75rem 1rem;
  color: #f8fafc;
}

.brand-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.65rem;
}

.brand-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.tank-icon {
  font-size: 1.35rem;
}

.title-text {
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: -0.025em;
  margin: 0;
  color: #f1f5f9;
}

.mode-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.25rem 0.6rem;
  border-radius: 9999px;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border: 1px solid rgba(245, 158, 11, 0.3);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #fbbf24;
}

.status-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.3rem 0.6rem;
  border-radius: 0.375rem;
  background: #1e293b;
  border: 1px solid #334155;
  font-size: 0.8rem;
  font-weight: 500;
}

.pill-label {
  color: #94a3b8;
}

.pill-value {
  color: #f8fafc;
  font-weight: 600;
}

.indicator-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

/* Status variants */
.conn-connected .indicator-dot { background: #22c55e; }
.conn-disconnected .indicator-dot { background: #eab308; }
.conn-offline .indicator-dot { background: #ef4444; }

.service-active .indicator-dot { background: #22c55e; }
.service-inactive .indicator-dot { background: #94a3b8; }
.service-failed .indicator-dot { background: #ef4444; }
.service-unknown .indicator-dot { background: #64748b; }

.guard-armed {
  background: rgba(34, 197, 94, 0.12);
  border-color: rgba(34, 197, 94, 0.3);
}
.guard-armed .indicator-dot {
  background: #22c55e;
  box-shadow: 0 0 6px #22c55e;
}
.guard-disarmed .indicator-dot { background: #eab308; }
.guard-arming .indicator-dot { background: #38bdf8; }
.guard-fault .indicator-dot { background: #ef4444; }

.battery-good .pill-value { color: #22c55e; }
.battery-warn .pill-value { color: #f59e0b; }
.battery-crit .pill-value { color: #ef4444; }
.battery-unknown .pill-value { color: #94a3b8; }

.owner-self {
  background: rgba(56, 189, 248, 0.15);
  border-color: rgba(56, 189, 248, 0.4);
}
.owner-self .pill-value { color: #38bdf8; }
.owner-other .pill-value { color: #f59e0b; }
.owner-none .pill-value { color: #94a3b8; }
</style>

