<template>
  <section
    class="camera-panel"
    role="region"
    aria-label="Robot Camera Stream and Media Controls"
  >
    <!-- Header: Title, Preview Tag, and Live Status Badge -->
    <div class="camera-header">
      <div class="header-left">
        <h2 class="panel-title">Camera</h2>
        <span class="preview-tag" title="No backend camera integration active in this milestone">
          UI preview — camera disconnected
        </span>
      </div>
      <div
        class="camera-status-indicator"
        :class="`status-${effectiveState}`"
        :aria-label="`Camera status: ${statusLabel}`"
      >
        <span class="status-dot" aria-hidden="true" />
        <span class="status-label">{{ statusLabel }}</span>
      </div>
    </div>

    <!-- Live Preview Viewport (4:3 aspect ratio, bundled placeholder SVG) -->
    <div class="camera-viewport-wrapper">
      <div
        class="camera-viewport"
        :class="`viewport-${effectiveState}`"
        role="img"
        :aria-label="`Camera preview: ${statusLabel}`"
      >
        <svg
          class="preview-svg"
          viewBox="0 0 640 480"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          <!-- Viewport Background -->
          <rect width="640" height="480" fill="#090d16" />

          <!-- Grid Lines -->
          <g stroke="#1e293b" stroke-width="1" stroke-dasharray="4 4" opacity="0.6">
            <line x1="160" y1="0" x2="160" y2="480" />
            <line x1="320" y1="0" x2="320" y2="480" />
            <line x1="480" y1="0" x2="480" y2="480" />
            <line x1="0" y1="120" x2="640" y2="120" />
            <line x1="0" y1="240" x2="640" y2="240" />
            <line x1="0" y1="360" x2="640" y2="360" />
          </g>

          <!-- Framing Corners -->
          <g stroke="#38bdf8" stroke-width="2" fill="none" opacity="0.8">
            <!-- Top-Left -->
            <path d="M 20 50 L 20 20 L 50 20" />
            <!-- Top-Right -->
            <path d="M 590 20 L 620 20 L 620 50" />
            <!-- Bottom-Left -->
            <path d="M 20 430 L 20 460 L 50 460" />
            <!-- Bottom-Right -->
            <path d="M 590 460 L 620 460 L 620 430" />
          </g>

          <!-- Center Reticle / State Visuals -->
          <template v-if="effectiveState === 'live' || effectiveState === 'recording'">
            <circle cx="320" cy="240" r="64" stroke="#38bdf8" stroke-width="1.5" fill="none" opacity="0.4" />
            <circle cx="320" cy="240" r="4" fill="#38bdf8" />
            <line x1="320" y1="160" x2="320" y2="190" stroke="#38bdf8" stroke-width="1.5" />
            <line x1="320" y1="290" x2="320" y2="320" stroke="#38bdf8" stroke-width="1.5" />
            <line x1="240" y1="240" x2="270" y2="240" stroke="#38bdf8" stroke-width="1.5" />
            <line x1="370" y1="240" x2="400" y2="240" stroke="#38bdf8" stroke-width="1.5" />
            <text x="320" y="360" text-anchor="middle" fill="#94a3b8" font-size="16" font-family="sans-serif" font-weight="600">
              Live preview
            </text>
            <text x="320" y="386" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              640 × 480 @ 15 fps (UI preview)
            </text>
          </template>

          <template v-else-if="effectiveState === 'connecting'">
            <circle cx="320" cy="240" r="40" stroke="#f59e0b" stroke-width="2" stroke-dasharray="6 6" fill="none" class="svg-spin" />
            <text x="320" y="320" text-anchor="middle" fill="#f59e0b" font-size="16" font-family="sans-serif" font-weight="600">
              Connecting to camera...
            </text>
            <text x="320" y="344" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              Establishing MJPEG stream
            </text>
          </template>

          <template v-else-if="effectiveState === 'stale'">
            <circle cx="320" cy="240" r="44" stroke="#f97316" stroke-width="2" fill="none" opacity="0.6" />
            <text x="320" y="248" text-anchor="middle" fill="#f97316" font-size="28" font-family="sans-serif">⏸</text>
            <text x="320" y="320" text-anchor="middle" fill="#f97316" font-size="16" font-family="sans-serif" font-weight="600">
              Camera frames stale
            </text>
            <text x="320" y="344" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              Frame arrival timeout exceeded
            </text>
          </template>

          <template v-else-if="effectiveState === 'unavailable'">
            <circle cx="320" cy="240" r="44" stroke="#64748b" stroke-width="2" fill="none" opacity="0.5" />
            <line x1="285" y1="205" x2="355" y2="275" stroke="#ef4444" stroke-width="3" />
            <text x="320" y="320" text-anchor="middle" fill="#94a3b8" font-size="16" font-family="sans-serif" font-weight="600">
              Camera unavailable
            </text>
            <text x="320" y="344" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              Device disconnected or not ready
            </text>
          </template>

          <template v-else-if="effectiveState === 'finalizing'">
            <circle cx="320" cy="240" r="40" stroke="#38bdf8" stroke-width="2" stroke-dasharray="8 4" fill="none" class="svg-spin" />
            <text x="320" y="320" text-anchor="middle" fill="#38bdf8" font-size="16" font-family="sans-serif" font-weight="600">
              Finalizing video...
            </text>
            <text x="320" y="344" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              Packaging MP4 container
            </text>
          </template>

          <template v-else-if="effectiveState === 'error'">
            <polygon points="320,195 365,275 275,275" stroke="#ef4444" stroke-width="2" fill="none" />
            <text x="320" y="260" text-anchor="middle" fill="#ef4444" font-size="22" font-weight="bold" font-family="sans-serif">!</text>
            <text x="320" y="320" text-anchor="middle" fill="#ef4444" font-size="16" font-family="sans-serif" font-weight="600">
              Camera error
            </text>
            <text x="320" y="344" text-anchor="middle" fill="#64748b" font-size="12" font-family="sans-serif">
              Stream communication failure
            </text>
          </template>
        </svg>

        <!-- Viewport Overlays (REC badge and stream info) -->
        <div v-if="effectiveState === 'recording'" class="viewport-rec-badge" aria-hidden="true">
          <span class="rec-dot" />
          <span>REC {{ formattedElapsed }}</span>
        </div>
        <div class="viewport-res-badge" aria-hidden="true">
          640×480
        </div>
      </div>
    </div>

    <!-- Production Action Buttons: Exactly Two Buttons (Capture and Record / Stop recording) -->
    <div class="camera-actions-grid">
      <!-- 1. Capture Button -->
      <button
        type="button"
        class="camera-btn btn-capture"
        :disabled="!canCapture"
        :aria-busy="pendingAction === 'capturing'"
        aria-label="Capture JPEG frame"
        @click="handleCapture"
      >
        <span class="btn-icon" aria-hidden="true">{{ pendingAction === 'capturing' ? '⏳' : '📷' }}</span>
        <span class="btn-text">Capture</span>
      </button>

      <!-- 2. Record / Stop recording Button -->
      <button
        type="button"
        class="camera-btn"
        :class="isRecording ? 'btn-stop-rec' : 'btn-record'"
        :disabled="!canRecord"
        :aria-busy="pendingAction === 'starting' || pendingAction === 'stopping'"
        :aria-label="isRecording ? 'Stop video recording' : 'Start video recording'"
        @click="handleRecordToggle"
      >
        <span class="btn-icon" aria-hidden="true">
          <template v-if="pendingAction === 'starting' || pendingAction === 'stopping'">⏳</template>
          <template v-else-if="isRecording">⏹</template>
          <template v-else>⏺</template>
        </span>
        <span class="btn-text">
          {{ isRecording ? 'Stop recording' : 'Record' }}
        </span>
      </button>
    </div>

    <!-- Feedback Message Area -->
    <div
      class="camera-feedback"
      :class="`feedback-${feedback.type}`"
      role="status"
      aria-live="polite"
    >
      <span class="feedback-icon" aria-hidden="true">{{ feedbackIcon }}</span>
      <span class="feedback-text">{{ feedback.text }}</span>
    </div>

    <!-- Download Links: Link only, NOT action buttons -->
    <div class="camera-media-section">
      <span class="media-label">Latest image / video:</span>
      <div v-if="latestMedia" class="media-link-wrapper">
        <a
          href="#"
          class="media-download-link disabled-example"
          aria-disabled="true"
          tabindex="-1"
          role="link"
          title="Download link disabled — UI preview example"
          @click.prevent
        >
          {{ latestMedia.filename }}
        </a>
      </div>
      <span v-else class="media-none">None yet</span>
    </div>

    <!-- Review Fixtures: Interactive State Simulation Selector for Owner Review -->
    <div class="review-fixtures-box" role="region" aria-label="Review Fixtures">
      <div class="fixtures-header">
        <span class="fixtures-title">Review fixtures:</span>
        <span class="fixtures-hint">Test component states</span>
      </div>
      <div class="fixtures-control">
        <label for="camera-fixture-select" class="sr-only">Select camera preview fixture</label>
        <select
          id="camera-fixture-select"
          v-model="selectedFixture"
          class="fixture-select"
        >
          <option value="interactive">Interactive (No-op simulated preview)</option>
          <option value="live">Live preview</option>
          <option value="connecting">Connecting</option>
          <option value="stale">Stale frames</option>
          <option value="unavailable">Unavailable</option>
          <option value="pending">Pending request</option>
          <option value="finalizing">Finalizing</option>
          <option value="error">Error state</option>
        </select>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';

type FixtureState =
  | 'interactive'
  | 'live'
  | 'connecting'
  | 'stale'
  | 'unavailable'
  | 'pending'
  | 'finalizing'
  | 'error';

type FeedbackType = 'info' | 'success' | 'warning' | 'error';

interface MediaItem {
  filename: string;
  type: 'image' | 'video';
  timestamp: number;
}

const selectedFixture = ref<FixtureState>('interactive');
const isRecording = ref(false);
const elapsedSeconds = ref(0);
let timerId: ReturnType<typeof setInterval> | null = null;

const isPending = ref(false);
const pendingAction = ref<'capturing' | 'starting' | 'stopping' | null>(null);

const feedback = ref<{ text: string; type: FeedbackType }>({
  text: 'UI preview active — camera operations are simulated.',
  type: 'info',
});

// Default initial example download link per CAM-1 spec: disabled and identified as example
const latestMedia = ref<MediaItem | null>({
  filename: 'capture_preview.jpg (example)',
  type: 'image',
  timestamp: Date.now(),
});

// Effective component state: accounts for selected fixture and active recording
const effectiveState = computed(() => {
  if (selectedFixture.value === 'interactive') {
    if (pendingAction.value === 'stopping') return 'finalizing';
    if (isRecording.value) return 'recording';
    return 'live';
  }
  return selectedFixture.value;
});

// Status badge label and dot color
const statusLabel = computed(() => {
  switch (effectiveState.value) {
    case 'recording':
      return `REC ${formattedElapsed.value}`;
    case 'live':
      return 'Live';
    case 'connecting':
      return 'Connecting';
    case 'stale':
      return 'Stale';
    case 'unavailable':
      return 'Unavailable';
    case 'pending':
      return 'Pending';
    case 'finalizing':
      return 'Finalizing';
    case 'error':
      return 'Error';
    default:
      return 'Preview';
  }
});

const formattedElapsed = computed(() => {
  const m = Math.floor(elapsedSeconds.value / 60)
    .toString()
    .padStart(2, '0');
  const s = (elapsedSeconds.value % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
});

const feedbackIcon = computed(() => {
  switch (feedback.value.type) {
    case 'success':
      return '✓';
    case 'warning':
      return '⚠';
    case 'error':
      return '✖';
    case 'info':
    default:
      return 'ℹ';
  }
});

// Can Capture:
// Enabled during live and recording. Disabled when frames are stale, unavailable, connecting, error, or pending.
const canCapture = computed(() => {
  if (isPending.value || effectiveState.value === 'pending') return false;
  if (selectedFixture.value === 'interactive') return true;
  if (selectedFixture.value === 'live') return true;
  return false;
});

// Can Record:
// Disabled when frames are stale, connecting, unavailable, error, or pending.
// Exception: Keep Stop recording available while a recording is active, even if frames become stale.
const canRecord = computed(() => {
  if (isPending.value || effectiveState.value === 'pending') return false;
  if (effectiveState.value === 'finalizing') return false;

  if (isRecording.value) {
    // Stop recording is kept available even if frames are stale
    return true;
  }

  if (selectedFixture.value === 'interactive') return true;
  if (selectedFixture.value === 'live') return true;
  return false;
});

function getTimestampStr(): string {
  const d = new Date();
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

// Action 1: Capture (works during recording as well)
function handleCapture() {
  if (!canCapture.value) return;

  isPending.value = true;
  pendingAction.value = 'capturing';

  // Simulate short capture latency
  setTimeout(() => {
    isPending.value = false;
    pendingAction.value = null;
    feedback.value = {
      text: 'Preview only — no image saved.',
      type: 'info',
    };
    latestMedia.value = {
      filename: `capture_${getTimestampStr()}.jpg (example)`,
      type: 'image',
      timestamp: Date.now(),
    };
  }, 250);
}

// Action 2: Record / Stop recording
function handleRecordToggle() {
  if (!canRecord.value) return;

  if (isRecording.value) {
    // Request Stop recording -> enters finalizing transition -> returns download link
    isPending.value = true;
    pendingAction.value = 'stopping';
    feedback.value = {
      text: 'Finalizing recording...',
      type: 'info',
    };

    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }

    setTimeout(() => {
      isRecording.value = false;
      isPending.value = false;
      pendingAction.value = null;
      feedback.value = {
        text: 'Simulated recording ended — no file created.',
        type: 'info',
      };
      latestMedia.value = {
        filename: `recording_${getTimestampStr()}.mp4 (example)`,
        type: 'video',
        timestamp: Date.now(),
      };
      elapsedSeconds.value = 0;
    }, 450);
  } else {
    // Request Start recording -> enters recording state, starts timer
    isPending.value = true;
    pendingAction.value = 'starting';

    setTimeout(() => {
      isPending.value = false;
      pendingAction.value = null;
      isRecording.value = true;
      elapsedSeconds.value = 0;
      feedback.value = {
        text: 'Simulated recording active.',
        type: 'success',
      };

      if (timerId) clearInterval(timerId);
      timerId = setInterval(() => {
        elapsedSeconds.value++;
      }, 1000);
    }, 200);
  }
}

// Watch fixture changes to update preview state and feedback
watch(selectedFixture, (newFix) => {
  if (newFix !== 'interactive') {
    if (isRecording.value && newFix !== 'stale') {
      if (timerId) clearInterval(timerId);
      timerId = null;
      isRecording.value = false;
      elapsedSeconds.value = 0;
    }
  }

  switch (newFix) {
    case 'interactive':
      feedback.value = {
        text: 'UI preview active — camera operations are simulated.',
        type: 'info',
      };
      break;
    case 'live':
      feedback.value = {
        text: 'Live preview state active.',
        type: 'success',
      };
      break;
    case 'connecting':
      feedback.value = {
        text: 'Connecting to camera stream...',
        type: 'info',
      };
      break;
    case 'stale':
      feedback.value = {
        text: isRecording.value
          ? 'Frames stale — capture disabled; Stop recording remains available.'
          : 'Camera frames stale — capture and recording disabled.',
        type: 'warning',
      };
      break;
    case 'unavailable':
      feedback.value = {
        text: 'Camera unavailable — device disconnected or not responding.',
        type: 'warning',
      };
      break;
    case 'pending':
      feedback.value = {
        text: 'Camera operation request in progress...',
        type: 'info',
      };
      break;
    case 'finalizing':
      feedback.value = {
        text: 'Finalizing video stream...',
        type: 'info',
      };
      break;
    case 'error':
      feedback.value = {
        text: 'Camera error: Device communication failed.',
        type: 'error',
      };
      break;
  }
});

onUnmounted(() => {
  if (timerId) {
    clearInterval(timerId);
    timerId = null;
  }
});
</script>

<style scoped>
.camera-panel {
  background: #1e293b;
  border: 2px solid #334155;
  border-radius: 0.75rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  box-sizing: border-box;
  color: #f8fafc;
  outline: none;
}

.camera-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.header-left {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.panel-title {
  font-size: 1rem;
  font-weight: 700;
  margin: 0;
  color: #f1f5f9;
}

.preview-tag {
  font-size: 0.7rem;
  color: #94a3b8;
  background: #0f172a;
  padding: 0.15rem 0.4rem;
  border-radius: 0.25rem;
  border: 1px solid #334155;
}

/* Status Indicator Dot and Label */
.camera-status-indicator {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.2rem 0.5rem;
  border-radius: 0.25rem;
  background: #0f172a;
  border: 1px solid #334155;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.status-live .status-dot {
  background-color: #22c55e;
  box-shadow: 0 0 6px #22c55e;
}
.status-live .status-label {
  color: #4ade80;
}

.status-recording {
  background: rgba(220, 38, 38, 0.15);
  border-color: rgba(239, 68, 68, 0.4);
}
.status-recording .status-dot {
  background-color: #ef4444;
  box-shadow: 0 0 8px #ef4444;
  animation: pulse-dot 1s infinite alternate;
}
.status-recording .status-label {
  color: #f87171;
  font-weight: 700;
}

.status-connecting .status-dot {
  background-color: #f59e0b;
}
.status-connecting .status-label {
  color: #fbbf24;
}

.status-stale .status-dot {
  background-color: #f97316;
}
.status-stale .status-label {
  color: #fb923c;
}

.status-unavailable .status-dot {
  background-color: #64748b;
}
.status-unavailable .status-label {
  color: #94a3b8;
}

.status-finalizing .status-dot {
  background-color: #38bdf8;
}
.status-finalizing .status-label {
  color: #38bdf8;
}

.status-error .status-dot {
  background-color: #ef4444;
}
.status-error .status-label {
  color: #ef4444;
}

@keyframes pulse-dot {
  0% {
    opacity: 0.4;
  }
  100% {
    opacity: 1;
  }
}

/* Viewport Area */
.camera-viewport-wrapper {
  width: 100%;
  position: relative;
}

.camera-viewport {
  width: 100%;
  aspect-ratio: 4 / 3;
  background: #090d16;
  border-radius: 0.5rem;
  border: 1px solid #334155;
  overflow: hidden;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}

.preview-svg {
  width: 100%;
  height: 100%;
  display: block;
}

.svg-spin {
  transform-origin: 320px 240px;
  animation: svg-rotate 2s linear infinite;
}

@keyframes svg-rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.viewport-rec-badge {
  position: absolute;
  top: 0.75rem;
  left: 0.75rem;
  background: rgba(15, 23, 42, 0.85);
  border: 1px solid #ef4444;
  color: #f87171;
  font-size: 0.75rem;
  font-weight: 700;
  padding: 0.2rem 0.5rem;
  border-radius: 0.25rem;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.rec-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: #ef4444;
  animation: pulse-dot 0.8s infinite alternate;
}

.viewport-res-badge {
  position: absolute;
  bottom: 0.5rem;
  right: 0.5rem;
  background: rgba(15, 23, 42, 0.75);
  color: #64748b;
  font-size: 0.65rem;
  font-family: monospace;
  padding: 0.15rem 0.35rem;
  border-radius: 0.2rem;
}

/* Two Action Buttons */
.camera-actions-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
  width: 100%;
}

.camera-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;
  padding: 0.75rem 0.5rem;
  border-radius: 0.5rem;
  font-size: 0.9rem;
  font-weight: 700;
  cursor: pointer;
  border: 2px solid transparent;
  transition: transform 0.05s ease, background-color 0.15s ease, border-color 0.15s ease;
  user-select: none;
  -webkit-user-select: none;
  touch-action: manipulation;
}

.camera-btn:hover:not(:disabled) {
  filter: brightness(1.1);
}

.camera-btn:active:not(:disabled) {
  transform: scale(0.97);
}

.camera-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Capture button styling */
.btn-capture {
  background: #0284c7;
  color: #ffffff;
  border-color: #38bdf8;
}

.btn-capture:hover:not(:disabled) {
  background: #0369a1;
}

/* Record button (idle) */
.btn-record {
  background: #334155;
  color: #f8fafc;
  border-color: #64748b;
}

.btn-record:hover:not(:disabled) {
  background: #475569;
  border-color: #ef4444;
}

/* Stop recording button (active recording) */
.btn-stop-rec {
  background: #dc2626;
  color: #ffffff;
  border-color: #f87171;
  box-shadow: 0 0 10px rgba(239, 68, 68, 0.4);
}

.btn-stop-rec:hover:not(:disabled) {
  background: #b91c1c;
}

/* Feedback message area */
.camera-feedback {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.45rem 0.65rem;
  border-radius: 0.35rem;
  font-size: 0.8rem;
  background: #0f172a;
  border: 1px solid #334155;
}

.feedback-info {
  color: #cbd5e1;
  border-color: #334155;
}

.feedback-success {
  color: #4ade80;
  border-color: rgba(74, 222, 128, 0.3);
  background: rgba(34, 197, 94, 0.1);
}

.feedback-warning {
  color: #fbbf24;
  border-color: rgba(251, 191, 36, 0.3);
  background: rgba(245, 158, 11, 0.1);
}

.feedback-error {
  color: #f87171;
  border-color: rgba(248, 113, 113, 0.3);
  background: rgba(239, 68, 68, 0.1);
}

/* Media download link row */
.camera-media-section {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: #94a3b8;
  flex-wrap: wrap;
}

.media-label {
  font-weight: 600;
  color: #cbd5e1;
}

.media-download-link {
  color: #38bdf8;
  text-decoration: underline;
  word-break: break-all;
}

.media-download-link.disabled-example {
  color: #7dd3fc;
  cursor: not-allowed;
  opacity: 0.85;
}

.media-none {
  color: #64748b;
  font-style: italic;
}

/* Review fixtures selector toolbar */
.review-fixtures-box {
  margin-top: 0.25rem;
  padding: 0.5rem 0.75rem;
  background: #0f172a;
  border: 1px dashed #475569;
  border-radius: 0.375rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.fixtures-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.fixtures-title {
  font-size: 0.7rem;
  font-weight: 700;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.fixtures-hint {
  font-size: 0.65rem;
  color: #64748b;
}

.fixtures-control {
  width: 100%;
}

.fixture-select {
  width: 100%;
  padding: 0.35rem 0.5rem;
  border-radius: 0.25rem;
  background: #1e293b;
  border: 1px solid #475569;
  color: #f1f5f9;
  font-size: 0.75rem;
  outline: none;
  cursor: pointer;
}

.fixture-select:focus {
  border-color: #38bdf8;
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
