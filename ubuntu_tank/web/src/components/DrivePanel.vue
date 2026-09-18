<template>
  <section
    class="drive-panel"
    :class="{ 'panel-focused': isFocused, 'panel-armed': isArmed }"
    tabindex="0"
    role="region"
    aria-label="Directional Teleoperation Drive Panel"
    @focus="onFocus"
    @blur="onBlur"
  >
    <div class="panel-header">
      <h2 class="panel-title">Chassis Teleoperation</h2>
      <div class="focus-indicator" :class="{ active: isFocused }">
        <span class="indicator-icon" aria-hidden="true">{{ isFocused ? '⌨️' : '🖱️' }}</span>
        <span>{{ isFocused ? 'Keyboard active (W/A/S/D)' : 'Click to focus keyboard' }}</span>
      </div>
    </div>

    <!-- Directional Pad Grid -->
    <div class="dpad-grid">
      <!-- Forward -->
      <div class="dpad-cell dpad-forward">
        <button
          class="drive-btn btn-dir"
          :class="{ active: activeDirection === 'forward' }"
          :disabled="!canDrive"
          aria-label="Drive Forward (Hold W)"
          @pointerdown="emit('pointer-down', $event, 'forward')"
          @pointerup="emit('pointer-up', $event)"
          @pointercancel="emit('pointer-cancel', $event)"
          @pointerleave="emit('pointer-leave', $event)"
          @contextmenu.prevent
        >
          <span class="btn-arrow" aria-hidden="true">▲</span>
          <span class="btn-label">Forward</span>
          <kbd class="btn-kbd">W</kbd>
        </button>
      </div>

      <!-- Left -->
      <div class="dpad-cell dpad-left">
        <button
          class="drive-btn btn-dir"
          :class="{ active: activeDirection === 'spin_left' }"
          :disabled="!canDrive"
          aria-label="Spin Left (Hold A)"
          @pointerdown="emit('pointer-down', $event, 'spin_left')"
          @pointerup="emit('pointer-up', $event)"
          @pointercancel="emit('pointer-cancel', $event)"
          @pointerleave="emit('pointer-leave', $event)"
          @contextmenu.prevent
        >
          <span class="btn-arrow" aria-hidden="true">◀</span>
          <span class="btn-label">Left</span>
          <kbd class="btn-kbd">A</kbd>
        </button>
      </div>

      <!-- STOP / Space (Center) -->
      <div class="dpad-cell dpad-stop">
        <button
          class="drive-btn btn-stop"
          aria-label="Emergency Stop and Disarm (Press Space)"
          @click="emit('emergency-stop')"
        >
          <span class="btn-stop-icon" aria-hidden="true">🛑</span>
          <span class="btn-stop-text">STOP</span>
          <kbd class="btn-kbd kbd-stop">Space</kbd>
        </button>
      </div>

      <!-- Right -->
      <div class="dpad-cell dpad-right">
        <button
          class="drive-btn btn-dir"
          :class="{ active: activeDirection === 'spin_right' }"
          :disabled="!canDrive"
          aria-label="Spin Right (Hold D)"
          @pointerdown="emit('pointer-down', $event, 'spin_right')"
          @pointerup="emit('pointer-up', $event)"
          @pointercancel="emit('pointer-cancel', $event)"
          @pointerleave="emit('pointer-leave', $event)"
          @contextmenu.prevent
        >
          <span class="btn-arrow" aria-hidden="true">▶</span>
          <span class="btn-label">Right</span>
          <kbd class="btn-kbd">D</kbd>
        </button>
      </div>

      <!-- Reverse -->
      <div class="dpad-cell dpad-reverse">
        <button
          class="drive-btn btn-dir"
          :class="{ active: activeDirection === 'reverse' }"
          :disabled="!canDrive"
          aria-label="Drive Reverse (Hold S)"
          @pointerdown="emit('pointer-down', $event, 'reverse')"
          @pointerup="emit('pointer-up', $event)"
          @pointercancel="emit('pointer-cancel', $event)"
          @pointerleave="emit('pointer-leave', $event)"
          @contextmenu.prevent
        >
          <span class="btn-arrow" aria-hidden="true">▼</span>
          <span class="btn-label">Reverse</span>
          <kbd class="btn-kbd">S</kbd>
        </button>
      </div>
    </div>

    <!-- Instruction Subtext -->
    <div class="panel-footer" aria-live="polite">
      <p class="instruction-text">
        Hold a direction to move. Release to stop. Space stops and disarms immediately.
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import type { MotionDirection } from '../types/api';

const props = defineProps<{
  isOwner: boolean;
  isArmed: boolean;
  activeDirection: MotionDirection;
}>();

const emit = defineEmits<{
  (e: 'pointer-down', event: PointerEvent, direction: MotionDirection): void;
  (e: 'pointer-up', event: PointerEvent): void;
  (e: 'pointer-cancel', event: PointerEvent): void;
  (e: 'pointer-leave', event: PointerEvent): void;
  (e: 'emergency-stop'): void;
  (e: 'focus-change', focused: boolean): void;
}>();

const isFocused = ref(false);

const canDrive = computed(() => {
  return props.isOwner && props.isArmed;
});

function onFocus() {
  isFocused.value = true;
  emit('focus-change', true);
}

function onBlur() {
  isFocused.value = false;
  emit('focus-change', false);
}
</script>

<style scoped>
.drive-panel {
  background: #1e293b;
  border: 2px solid #334155;
  border-radius: 0.75rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.85rem;
  outline: none;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
  user-select: none;
  -webkit-user-select: none;
  touch-action: manipulation;
}

.drive-panel:focus-visible,
.drive-panel.panel-focused {
  border-color: #38bdf8;
  box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2);
}

.panel-armed {
  border-color: rgba(34, 197, 94, 0.4);
}

.panel-header {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-title {
  font-size: 1rem;
  font-weight: 700;
  margin: 0;
  color: #f1f5f9;
}

.focus-indicator {
  font-size: 0.75rem;
  font-weight: 500;
  color: #94a3b8;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.25rem 0.5rem;
  border-radius: 0.25rem;
  background: #0f172a;
}

.focus-indicator.active {
  color: #38bdf8;
  background: rgba(56, 189, 248, 0.15);
  border: 1px solid rgba(56, 189, 248, 0.3);
}

/* 3x3 Grid for Directional Controls */
.dpad-grid {
  display: grid;
  grid-template-columns: repeat(3, 84px);
  grid-template-rows: repeat(3, 84px);
  gap: 0.65rem;
  justify-content: center;
  align-items: center;
  margin: 0.5rem 0;
}

.dpad-forward {
  grid-column: 2;
  grid-row: 1;
}
.dpad-left {
  grid-column: 1;
  grid-row: 2;
}
.dpad-stop {
  grid-column: 2;
  grid-row: 2;
}
.dpad-right {
  grid-column: 3;
  grid-row: 2;
}
.dpad-reverse {
  grid-column: 2;
  grid-row: 3;
}

.dpad-cell {
  width: 100%;
  height: 100%;
}

.drive-btn {
  width: 100%;
  height: 100%;
  border-radius: 0.5rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.15rem;
  border: 2px solid transparent;
  cursor: pointer;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  transition: transform 0.05s ease, background-color 0.1s ease;
}

.btn-dir {
  background: #334155;
  color: #f8fafc;
  border-color: #475569;
}

.btn-dir:hover:not(:disabled) {
  background: #475569;
}

.btn-dir:active:not(:disabled),
.btn-dir.active {
  background: #0284c7;
  border-color: #38bdf8;
  transform: scale(0.96);
  box-shadow: 0 0 12px rgba(56, 189, 248, 0.4);
}

.btn-dir:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.btn-arrow {
  font-size: 1.15rem;
  line-height: 1;
}

.btn-label {
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.btn-kbd {
  font-size: 0.65rem;
  font-family: monospace;
  font-weight: 600;
  padding: 0.1rem 0.3rem;
  border-radius: 0.2rem;
  background: rgba(0, 0, 0, 0.3);
  color: #cbd5e1;
  border: 1px solid rgba(255, 255, 255, 0.15);
}

/* STOP Button */
.btn-stop {
  background: #dc2626;
  color: #ffffff;
  border-color: #ef4444;
  box-shadow: 0 4px 6px -1px rgba(220, 38, 38, 0.3);
}

.btn-stop:hover {
  background: #b91c1c;
}

.btn-stop:active {
  background: #991b1b;
  transform: scale(0.95);
}

.btn-stop-icon {
  font-size: 1.1rem;
  line-height: 1;
}

.btn-stop-text {
  font-size: 0.9rem;
  font-weight: 900;
  letter-spacing: 0.05em;
}

.kbd-stop {
  background: rgba(0, 0, 0, 0.4);
  color: #fee2e2;
  border-color: rgba(254, 226, 226, 0.3);
}

.panel-footer {
  text-align: center;
}

.instruction-text {
  font-size: 0.8rem;
  color: #94a3b8;
  margin: 0;
}

@media (max-width: 480px) {
  .dpad-grid {
    grid-template-columns: repeat(3, 76px);
    grid-template-rows: repeat(3, 76px);
    gap: 0.5rem;
  }
}
</style>

