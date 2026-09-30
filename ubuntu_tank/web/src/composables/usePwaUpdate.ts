// Register the asset-only worker and activate updates only after confirmed control cleanup.
import { onMounted, onUnmounted, ref } from 'vue';

/** Expose offline/update state; failed shutdown leaves the current page and worker active. */
export function usePwaUpdate() {
  const isOffline = ref(typeof navigator !== 'undefined' ? !navigator.onLine : false);
  const needRefresh = ref(false);
  let registration: ServiceWorkerRegistration | null = null;
  let installing: ServiceWorker | null = null;
  let updateRequested = false;
  let disposed = false;

  function onOnline() { isOffline.value = false; }
  function onOffline() { isOffline.value = true; }
  function inspectWorker() {
    needRefresh.value = Boolean(registration?.waiting && navigator.serviceWorker.controller);
  }
  function onUpdateFound() {
    installing?.removeEventListener('statechange', inspectWorker);
    installing = registration?.installing ?? null;
    installing?.addEventListener('statechange', inspectWorker);
    inspectWorker();
  }
  function onControllerChange() {
    inspectWorker();
    if (updateRequested) window.location.reload();
  }

  async function applyUpdate(stopAndRelease: () => Promise<boolean>): Promise<void> {
    if (updateRequested) return;
    updateRequested = true;
    try {
      if (!await stopAndRelease()) {
        updateRequested = false;
        return;
      }
      if (registration?.waiting) {
        registration.waiting.postMessage({ type: 'SKIP_WAITING' });
      } else {
        window.location.reload();
      }
    } catch {
      updateRequested = false;
    }
  }

  onMounted(async () => {
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.addEventListener('controllerchange', onControllerChange);
    try {
      const registered = await navigator.serviceWorker.register('/sw.js');
      if (disposed) return;
      registration = registered;
      registration.addEventListener('updatefound', onUpdateFound);
      onUpdateFound();
    } catch {
      // Service-worker availability does not grant or remove robot authority.
    }
  });

  onUnmounted(() => {
    disposed = true;
    window.removeEventListener('online', onOnline);
    window.removeEventListener('offline', onOffline);
    registration?.removeEventListener('updatefound', onUpdateFound);
    installing?.removeEventListener('statechange', inspectWorker);
    navigator.serviceWorker?.removeEventListener('controllerchange', onControllerChange);
  });

  return { isOffline, needRefresh, applyUpdate };
}
