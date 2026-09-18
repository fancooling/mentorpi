// Composable for PWA lifecycle, service worker updates, and offline status
import { onMounted, onUnmounted, ref } from 'vue';

export function usePwaUpdate() {
  const isOffline = ref(typeof navigator !== 'undefined' ? !navigator.onLine : false);
  const needRefresh = ref(false);
  const updateSW = ref<((reloadPage?: boolean) => Promise<void>) | null>(null);

  function onOnline() {
    isOffline.value = false;
  }

  function onOffline() {
    isOffline.value = true;
  }

  async function applyUpdate(disarmAndRelease: () => Promise<any>): Promise<void> {
    // Crucial safety constraint: Must stop/disarm and relinquish ownership before updating!
    try {
      await disarmAndRelease();
    } catch (err) {
      console.warn('Failed to disarm before update:', err);
    }

    if (updateSW.value) {
      await updateSW.value(true);
    } else if (typeof window !== 'undefined') {
      window.location.reload();
    }
  }

  onMounted(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('online', onOnline);
      window.addEventListener('offline', onOffline);

      // Register service worker update check if supported
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.addEventListener('controllerchange', () => {
          // New service worker activated
        });
      }
    }
  });

  onUnmounted(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
    }
  });

  return {
    isOffline,
    needRefresh,
    applyUpdate,
  };
}

