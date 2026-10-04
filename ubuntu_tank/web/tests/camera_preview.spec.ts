import { test, expect } from '@playwright/test';

test.describe('CAM-1 Camera UI Preview and Standalone Review Mode', () => {
  test('1. Standalone review mode loads without backend and makes zero robot requests', async ({ page }) => {
    const robotApiRequests: string[] = [];

    page.on('request', (req) => {
      const url = req.url();
      if (url.includes('/api/v1/') || url.startsWith('ws://') || url.startsWith('wss://')) {
        robotApiRequests.push(url);
      }
    });

    await page.goto('/?review=camera');

    // Review banner is prominently displayed
    const reviewBanner = page.locator('.review-banner');
    await expect(reviewBanner).toBeVisible();
    await expect(reviewBanner).toContainText('Review Mode');
    await expect(reviewBanner).toContainText('Teleoperation controls are inert');

    // Camera panel title and UI preview badge
    const cameraPanel = page.locator('.camera-panel');
    await expect(cameraPanel).toBeVisible();
    await expect(cameraPanel.locator('.panel-title')).toHaveText('Camera');
    await expect(cameraPanel.locator('.preview-tag')).toHaveText('UI preview — camera disconnected');

    // Initial status badge shows Live in default preview
    await expect(cameraPanel.locator('.camera-status-indicator')).toContainText('Live');

    // Viewport SVG shows live preview placeholder text
    const previewSvg = cameraPanel.locator('.preview-svg');
    await expect(previewSvg).toBeVisible();
    await expect(previewSvg).toContainText('Live preview');
    await expect(previewSvg).toContainText('640 × 480 @ 15 fps');

    // Exactly two action buttons exist
    const actionButtons = cameraPanel.locator('.camera-btn');
    await expect(actionButtons).toHaveCount(2);

    const captureBtn = cameraPanel.locator('.btn-capture');
    const recordBtn = cameraPanel.locator('.btn-record');
    await expect(captureBtn).toBeVisible();
    await expect(captureBtn).toBeEnabled();
    await expect(recordBtn).toBeVisible();
    await expect(recordBtn).toBeEnabled();
    await expect(recordBtn).toContainText('Record');

    // Example download link is disabled and identified as example
    const downloadLink = cameraPanel.locator('.media-download-link');
    await expect(downloadLink).toBeVisible();
    await expect(downloadLink).toHaveAttribute('aria-disabled', 'true');
    await expect(downloadLink).toContainText('(example)');

    // Ensure zero robot API or WebSocket requests were triggered
    expect(robotApiRequests).toEqual([]);
  });

  test('2. Responsive layouts: beside drive on desktop, above drive on narrow screens', async ({ page }) => {
    // Desktop layout (1024x768)
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto('/?review=camera');

    const cameraPanel = page.locator('.console-camera');
    const drivePanel = page.locator('.console-drive');
    await expect(cameraPanel).toBeVisible();
    await expect(drivePanel).toBeVisible();

    const cameraBox = await cameraPanel.boundingBox();
    const driveBox = await drivePanel.boundingBox();
    expect(cameraBox).not.toBeNull();
    expect(driveBox).not.toBeNull();

    if (cameraBox && driveBox) {
      // Side-by-side: Drive panel is to the right of Camera panel
      expect(driveBox.x).toBeGreaterThan(cameraBox.x + cameraBox.width * 0.8);
      // Drive panel starts at roughly the same top row (within 30px)
      expect(Math.abs(driveBox.y - cameraBox.y)).toBeLessThan(40);
    }

    // Motion Stop button is visible in viewport without scrolling
    const stopBtn = page.locator('.btn-stop');
    await expect(stopBtn).toBeVisible();
    await expect(stopBtn).toBeInViewport();

    // Narrow layout (375x667)
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto('/?review=camera');

    const cameraBoxNarrow = await cameraPanel.boundingBox();
    const driveBoxNarrow = await drivePanel.boundingBox();
    expect(cameraBoxNarrow).not.toBeNull();
    expect(driveBoxNarrow).not.toBeNull();

    if (cameraBoxNarrow && driveBoxNarrow) {
      // Vertical stacking: Drive panel is positioned below Camera panel
      expect(driveBoxNarrow.y).toBeGreaterThan(cameraBoxNarrow.y + cameraBoxNarrow.height * 0.8);
    }
  });

  test('3. Camera action button transitions and simulated recording timer', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto('/?review=camera');

    const cameraPanel = page.locator('.camera-panel');
    const captureBtn = cameraPanel.locator('.btn-capture');
    const recordBtn = cameraPanel.locator('.camera-btn').nth(1);
    const feedback = cameraPanel.locator('.camera-feedback');
    const downloadLink = cameraPanel.locator('.media-download-link');

    // 1. Click Capture -> displays no-op feedback and updates example image link
    await captureBtn.click();
    await expect(feedback).toContainText('Preview only — no image saved.');
    await expect(downloadLink).toContainText('capture_');
    await expect(downloadLink).toContainText('(example)');
    await expect(downloadLink).toHaveAttribute('aria-disabled', 'true');

    // 2. Click Record -> changes button to 'Stop recording', shows red indicator and timer
    await recordBtn.click();
    await expect(recordBtn).toContainText('Stop recording');
    await expect(recordBtn).toHaveClass(/btn-stop-rec/);

    // Indicator changes to red REC with timer
    const statusIndicator = cameraPanel.locator('.camera-status-indicator');
    await expect(statusIndicator).toContainText('REC 00:');
    await expect(statusIndicator).toHaveClass(/status-recording/);

    // Viewport REC badge appears
    await expect(cameraPanel.locator('.viewport-rec-badge')).toBeVisible();

    // Wait 1.1s for timer tick
    await page.waitForTimeout(1100);
    await expect(statusIndicator).toContainText('REC 00:0');

    // 3. Capture during recording works without stopping recording
    await captureBtn.click();
    await expect(feedback).toContainText('Preview only — no image saved.');
    await expect(recordBtn).toContainText('Stop recording');
    await expect(statusIndicator).toHaveClass(/status-recording/);

    // 4. Click 'Stop recording' -> ends recording, finalizes, reverts to 'Record'
    await recordBtn.click();
    await expect(feedback).toContainText('Simulated recording ended — no file created.');
    await expect(recordBtn).toContainText('Record');
    await expect(statusIndicator).toContainText('Live');
    await expect(cameraPanel.locator('.viewport-rec-badge')).not.toBeVisible();

    // Download link updated with example recording
    await expect(downloadLink).toContainText('recording_');
    await expect(downloadLink).toContainText('(example)');
    await expect(downloadLink).toHaveAttribute('aria-disabled', 'true');
  });

  test('4. Review fixtures: inspect connecting, stale, unavailable, finalizing, and error states', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto('/?review=camera');

    const cameraPanel = page.locator('.camera-panel');
    const fixtureSelect = cameraPanel.locator('#camera-fixture-select');
    const captureBtn = cameraPanel.locator('.btn-capture');
    const recordBtn = cameraPanel.locator('.camera-btn').nth(1);
    const statusIndicator = cameraPanel.locator('.camera-status-indicator');
    const feedback = cameraPanel.locator('.camera-feedback');
    const previewSvg = cameraPanel.locator('.preview-svg');

    // 1. Connecting fixture
    await fixtureSelect.selectOption('connecting');
    await expect(statusIndicator).toContainText('Connecting');
    await expect(captureBtn).toBeDisabled();
    await expect(recordBtn).toBeDisabled();
    await expect(previewSvg).toContainText('Connecting to camera');

    // 2. Stale frames fixture: Capture and Record disabled
    await fixtureSelect.selectOption('stale');
    await expect(statusIndicator).toContainText('Stale');
    await expect(captureBtn).toBeDisabled();
    await expect(recordBtn).toBeDisabled();
    await expect(feedback).toContainText('Camera frames stale — capture and recording disabled.');

    // 3. Unavailable fixture
    await fixtureSelect.selectOption('unavailable');
    await expect(statusIndicator).toContainText('Unavailable');
    await expect(captureBtn).toBeDisabled();
    await expect(recordBtn).toBeDisabled();
    await expect(previewSvg).toContainText('Camera unavailable');

    // 4. Error fixture
    await fixtureSelect.selectOption('error');
    await expect(statusIndicator).toContainText('Error');
    await expect(captureBtn).toBeDisabled();
    await expect(recordBtn).toBeDisabled();
    await expect(previewSvg).toContainText('Camera error');

    // 5. Finalizing fixture
    await fixtureSelect.selectOption('finalizing');
    await expect(statusIndicator).toContainText('Finalizing');
    await expect(captureBtn).toBeDisabled();
    await expect(recordBtn).toBeDisabled();
    await expect(previewSvg).toContainText('Finalizing video');

    // 6. Stale frames during active recording: Stop recording remains available
    await fixtureSelect.selectOption('interactive');
    await expect(statusIndicator).toContainText('Live');
    await recordBtn.click();
    await expect(recordBtn).toContainText('Stop recording');

    // Switch to stale while recording
    await fixtureSelect.selectOption('stale');
    await expect(statusIndicator).toContainText('Stale');
    // Capture is disabled due to stale frames
    await expect(captureBtn).toBeDisabled();
    // BUT Stop recording remains available and enabled!
    await expect(recordBtn).toContainText('Stop recording');
    await expect(recordBtn).toBeEnabled();
    await expect(feedback).toContainText('Stop recording remains available');

    // Clicking Stop recording works
    await recordBtn.click();
    await expect(feedback).toContainText('Simulated recording ended — no file created.');
  });

  test('5. Driving controls are inert in review mode and preserve separation from motion Stop', async ({ page }) => {
    const apiCalls: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/v1/')) {
        apiCalls.push(req.url());
      }
    });

    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto('/?review=camera');

    // Take control button click does not make API call and informs user
    const takeControlBtn = page.locator('button', { hasText: 'Take control' });
    await takeControlBtn.click();
    await expect(page.locator('.feedback-banner')).toContainText('Driving controls are inert');
    expect(apiCalls).toEqual([]);

    // Drive buttons remain disabled
    const forwardBtn = page.locator('button[aria-label^="Drive Forward"]');
    await expect(forwardBtn).toBeDisabled();

    // Clicking motion Stop in review mode does not send API requests
    const motionStopBtn = page.locator('.btn-stop');
    await motionStopBtn.click();
    expect(apiCalls).toEqual([]);

    // Keyboard Space key retains Stop priority and does not trigger camera buttons
    const captureBtn = page.locator('.btn-capture');
    await captureBtn.focus();
    await expect(captureBtn).toBeFocused();

    // Press Space while focused on capture button
    await page.keyboard.press('Space');

    // Capture button should NOT have been activated (feedback remains initial or inert)
    await expect(page.locator('.camera-feedback')).not.toContainText('Preview only — no image saved.');
    expect(apiCalls).toEqual([]);
  });
});
