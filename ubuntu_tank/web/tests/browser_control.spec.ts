import { test, expect } from '@playwright/test';

test.describe('MentorPi Web Control & PWA Driving Interface (§2)', () => {
  test.beforeEach(async ({ page, request }) => {
    try {
      await request.post('/api/v1/test/reset');
    } catch {
      // Ignore if endpoint is not available
    }
    // Ensure viewport is consistent
    await page.setViewportSize({ width: 1024, height: 768 });
  });

  async function armRobot(page: any) {
    await page.goto('/');
    const takeControlBtn = page.locator('button', { hasText: 'Take control' });
    await expect(takeControlBtn).toBeEnabled();
    await takeControlBtn.click();
    await expect(page.locator('.owner-self')).toBeVisible();

    const checkbox = page.locator('.safety-checkbox');
    await expect(checkbox).toBeEnabled();
    await checkbox.check();

    const armBtn = page.locator('button.btn-arm');
    await expect(armBtn).toBeEnabled();
    await armBtn.click();
    await expect(page.locator('.guard-armed')).toBeVisible();
  }

  test('1. Initial page load and status indicators', async ({ page }) => {
    await page.goto('/');

    // Check title and brand
    await expect(page.locator('.title-text')).toHaveText('MentorPi Tank');
    await expect(page.locator('.mode-badge')).toContainText('Raised-track mode');

    // Check status badges
    await expect(page.locator('.conn-connected')).toBeVisible();
    await expect(page.locator('.service-active')).toBeVisible();
    await expect(page.locator('.guard-disarmed')).toBeVisible();

    // Check battery voltage displays number (mock returns 12.2V)
    await expect(page.locator('.battery-good')).toContainText('12.2 V');

    // Check drive buttons exist and are initially disabled when disarmed
    const forwardBtn = page.locator('button[aria-label^="Drive Forward"]');
    await expect(forwardBtn).toBeDisabled();

    // STOP button must always be visible without scrolling
    const stopBtn = page.locator('.btn-stop');
    await expect(stopBtn).toBeVisible();
    await expect(stopBtn).toBeInViewport();

    // Check manifest link exists in DOM
    const manifestLink = page.locator('link[rel="manifest"]');
    await expect(manifestLink).toHaveAttribute('href', '/manifest.webmanifest');
  });

  test('2. Ownership acquisition and multi-tab exclusivity', async ({ browser }) => {
    const context = await browser.newContext();
    const page1 = await context.newPage();
    const page2 = await context.newPage();

    // Page 1 loads and takes control
    await page1.goto('/');
    const takeControlBtn1 = page1.locator('button', { hasText: 'Take control' });
    await expect(takeControlBtn1).toBeEnabled();
    await takeControlBtn1.click();

    // Page 1 should now be owner
    await expect(page1.locator('.owner-self')).toContainText('This tab');
    await expect(page1.locator('button', { hasText: 'Release control' })).toBeVisible();

    // Page 2 loads
    await page2.goto('/');
    // Page 2 should see that another client owns the slot
    await expect(page2.locator('.owner-other')).toBeVisible();

    // Page 2 attempting to take control should fail
    const takeControlBtn2 = page2.locator('button', { hasText: 'Take control' });
    await takeControlBtn2.click();
    await expect(page2.locator('.feedback-error')).toBeVisible();

    // Page 1 releases control
    await page1.locator('button', { hasText: 'Release control' }).click();
    await expect(page1.locator('.owner-none')).toBeVisible();

    // Now Page 2 can acquire control
    await page2.reload();
    await expect(page2.locator('.owner-none')).toBeVisible();
    await page2.locator('button', { hasText: 'Take control' }).click();
    await expect(page2.locator('.owner-self')).toContainText('This tab');

    await context.close();
  });

  test('3. Safety gate: Arm requires tracks-raised confirmation checkbox', async ({ page }) => {
    await page.goto('/');

    // Acquire control
    await page.locator('button', { hasText: 'Take control' }).click();
    await expect(page.locator('.owner-self')).toBeVisible();

    // Arm button is disabled initially because checkbox is unchecked
    const armBtn = page.locator('button.btn-arm');
    await expect(armBtn).toBeDisabled();

    // Check the tracks-raised checkbox
    const checkbox = page.locator('.safety-checkbox');
    await expect(checkbox).toBeEnabled();
    await checkbox.check();
    await expect(checkbox).toBeChecked();

    // Now Arm button should be enabled
    await expect(armBtn).toBeEnabled();

    // Click Arm
    await armBtn.click();

    // Chassis should now be Armed
    await expect(page.locator('.guard-armed')).toBeVisible();
    await expect(page.locator('button', { hasText: 'Disarm' })).toBeEnabled();

    // Drive buttons are now enabled
    const forwardBtn = page.locator('button[aria-label^="Drive Forward"]');
    await expect(forwardBtn).toBeEnabled();
  });

  test('4. Pointer driving: press and hold moves, release stops', async ({ page }) => {
    await armRobot(page);

    const forwardBtn = page.locator('button[aria-label^="Drive Forward"]');
    const statusVal = page.locator('.command-status-row .status-v').first();

    // Initially zero
    await expect(statusVal).toHaveText('zero');

    // Dispatch pointerdown on Forward button
    await forwardBtn.dispatchEvent('pointerdown', { pointerId: 1, pointerType: 'mouse', isPrimary: true });
    await expect(forwardBtn).toHaveClass(/active/);
    await expect(statusVal).toContainText('forward');

    // Hold briefly (300ms)
    await page.waitForTimeout(300);

    // Dispatch pointerup
    await forwardBtn.dispatchEvent('pointerup', { pointerId: 1, pointerType: 'mouse', isPrimary: true });
    await expect(forwardBtn).not.toHaveClass(/active/);
    await expect(statusVal).toHaveText('zero');
  });

  test('5. Pointer cancellation resets direction to neutral', async ({ page }) => {
    await armRobot(page);

    const forwardBtn = page.locator('button[aria-label^="Drive Forward"]');
    const statusVal = page.locator('.command-status-row .status-v').first();

    // Pointerdown
    await forwardBtn.dispatchEvent('pointerdown', { pointerId: 1, pointerType: 'mouse', isPrimary: true });
    await expect(statusVal).toContainText('forward');

    // Pointer leaves button or receives pointercancel
    await forwardBtn.dispatchEvent('pointercancel', { pointerId: 1, pointerType: 'mouse' });
    await expect(forwardBtn).not.toHaveClass(/active/);
    await expect(statusVal).toHaveText('zero');
  });

  test('6. Keyboard driving: W/A/S/D active only when panel is focused', async ({ page }) => {
    await armRobot(page);

    const drivePanel = page.locator('.drive-panel');
    const statusVal = page.locator('.command-status-row .status-v').first();

    // Pressing KeyW before focusing panel produces NO motion
    await page.keyboard.down('KeyW');
    await expect(statusVal).toHaveText('zero');
    await page.keyboard.up('KeyW');

    // Now focus the drive panel
    await drivePanel.focus();
    await expect(drivePanel).toHaveClass(/panel-focused/);
    await expect(page.locator('.focus-indicator')).toHaveClass(/active/);

    // Press KeyW -> forward
    await page.keyboard.down('KeyW');
    await expect(statusVal).toContainText('forward');
    await page.keyboard.up('KeyW');
    await expect(statusVal).toHaveText('zero');

    // Press KeyA -> spin left
    await page.keyboard.down('KeyA');
    await expect(statusVal).toContainText('spin left');
    await page.keyboard.up('KeyA');
    await expect(statusVal).toHaveText('zero');

    // Press KeyD -> spin right
    await page.keyboard.down('KeyD');
    await expect(statusVal).toContainText('spin right');
    await page.keyboard.up('KeyD');
    await expect(statusVal).toHaveText('zero');

    // Press KeyS -> reverse
    await page.keyboard.down('KeyS');
    await expect(statusVal).toContainText('reverse');
    await page.keyboard.up('KeyS');
    await expect(statusVal).toHaveText('zero');

    // Blurring the drive panel during keyboard driving immediately halts motion
    await drivePanel.focus();
    await page.keyboard.down('KeyW');
    await expect(statusVal).toContainText('forward');

    // Focus another element outside the drive panel
    await page.locator('button.btn-disarm').focus();
    await expect(statusVal).toHaveText('zero');
    await page.keyboard.up('KeyW');
  });

  test('7. Space key has immediate STOP priority document-wide', async ({ page }) => {
    await armRobot(page);

    const drivePanel = page.locator('.drive-panel');
    await drivePanel.focus();

    // Drive forward
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // Press Space
    await page.keyboard.press('Space');

    // Must immediately disarm
    await expect(page.locator('.guard-disarmed')).toBeVisible();
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Release KeyW
    await page.keyboard.up('KeyW');

    // Also verify on-screen STOP button immediately halts and disarms
    await armRobot(page);
    await drivePanel.focus();
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // Click on-screen STOP button
    await page.locator('button.btn-stop').click();
    await expect(page.locator('.guard-disarmed')).toBeVisible();
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');
    await page.keyboard.up('KeyW');
  });

  test('8. Keys held before Arm cannot initiate motion', async ({ page }) => {
    await page.goto('/');
    await page.locator('button', { hasText: 'Take control' }).click();
    await expect(page.locator('.owner-self')).toBeVisible();
    await page.locator('.safety-checkbox').check();

    const drivePanel = page.locator('.drive-panel');
    await drivePanel.focus();

    // Hold KeyW down BEFORE arming
    await page.keyboard.down('KeyW');

    // Click Arm while KeyW is held
    await page.locator('button.btn-arm').click();
    await expect(page.locator('.guard-armed')).toBeVisible();

    // Simulate OS auto-repeat events while KeyW is held: status MUST remain zero!
    await page.evaluate(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyW', repeat: true, bubbles: true }));
    });
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Release KeyW
    await page.keyboard.up('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Now press KeyW afresh -> motion initiates
    await drivePanel.focus();
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // Disarm while KeyW is held -> motion halts
    await page.locator('button.btn-disarm').click();
    await expect(page.locator('.guard-disarmed')).toBeVisible();
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Re-arm while KeyW remains physically held
    await page.locator('.safety-checkbox').check();
    await page.locator('button.btn-arm').click();
    await expect(page.locator('.guard-armed')).toBeVisible();

    // Key held across disarm/re-arm must STILL NOT initiate motion!
    await page.evaluate(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyW', repeat: true, bubbles: true }));
    });
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Only releasing and pressing afresh moves
    await page.keyboard.up('KeyW');
    await drivePanel.focus();
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');
    await page.keyboard.up('KeyW');
  });

  test('9. Mixed input conflict immediately halts motion', async ({ page }) => {
    await armRobot(page);

    const drivePanel = page.locator('.drive-panel');
    await drivePanel.focus();

    // Hold KeyW
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // While holding KeyW, press KeyA (mixed input conflict)
    await page.keyboard.down('KeyA');

    // Conflict halts motion and triggers safety disarm
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    // Release both
    await page.keyboard.up('KeyW');
    await page.keyboard.up('KeyA');
  });

  test('10. 5-second continuous hold cap triggers automatic stop and disarm', async ({ page }) => {
    await armRobot(page);

    const drivePanel = page.locator('.drive-panel');
    await drivePanel.focus();

    // Hold KeyW
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // Wait > 5.1 seconds for hold cap to expire
    await page.waitForTimeout(5200);

    // Chassis must have automatically stopped and disarmed
    await expect(page.locator('.guard-disarmed')).toBeVisible();
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    await page.keyboard.up('KeyW');
  });

  test('11. Window blur immediately clears input and disarms', async ({ page }) => {
    await armRobot(page);

    const drivePanel = page.locator('.drive-panel');
    await drivePanel.focus();
    await page.keyboard.down('KeyW');
    await expect(page.locator('.command-status-row .status-v').first()).toContainText('forward');

    // Trigger window blur
    await page.evaluate(() => window.dispatchEvent(new Event('blur')));

    // Must immediately disarm and clear motion
    await expect(page.locator('.guard-disarmed')).toBeVisible();
    await expect(page.locator('.command-status-row .status-v').first()).toHaveText('zero');

    await page.keyboard.up('KeyW');
  });

  test('12. Incompatible protocol version banner blocks control', async ({ page }) => {
    // Intercept version endpoint to simulate incompatible major version
    await page.route('/api/v1/version', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          protocol_version: '2.0.0',
          api_version: 'v2',
          schema_version: 2,
          release_id: 'future-release',
          supported_protocols: ['2.0.0'],
        }),
      });
    });

    await page.goto('/');

    // Incompatible banner must be visible
    await expect(page.locator('.banner-incompatible')).toBeVisible();

    // Take control button, safety checkbox, and arm button must be strictly disabled
    const takeControlBtn = page.locator('button', { hasText: 'Take control' });
    await expect(takeControlBtn).toBeDisabled();
    await expect(page.locator('.safety-checkbox')).toBeDisabled();
    await expect(page.locator('button.btn-arm')).toBeDisabled();
  });

  test('13. Arm recovers after lease expiry without resuming held input', async ({ page }) => {
    let injectedExpiry = false;
    const intents: string[] = [];
    await page.routeWebSocket('**/api/v1/control', (socket) => {
      const server = socket.connectToServer();
      socket.onMessage((message) => {
        const frame = JSON.parse(message.toString());
        if (frame.action === 'intent') intents.push(frame.payload.direction);
        server.send(message);
      });
      server.onMessage((message) => {
        const frame = JSON.parse(message.toString());
        // Exercise the real UI stop and explicit Arm paths after a protocol fault.
        if (!injectedExpiry && frame.type === 'ack' &&
            frame.payload.action === 'intent' && frame.payload.direction === 'forward') {
          injectedExpiry = true;
          socket.send(JSON.stringify({
            type: 'error',
            payload: { error: 'LEASE_EXPIRED', message: 'Injected input lease expiry' },
          }));
        } else {
          socket.send(message);
        }
      });
    });

    await armRobot(page);
    await page.locator('.drive-panel').focus();
    await page.keyboard.down('KeyW');
    await expect.poll(() => injectedExpiry).toBe(true);
    const forward = page.locator('button[aria-label^="Drive Forward"]');
    await expect(forward).toBeDisabled();
    await expect(page.locator('.guard-disarmed')).toBeVisible();

    const recoveryStart = intents.length;
    await page.locator('button.btn-arm').click();
    await expect(forward).toBeEnabled();
    await expect.poll(() => intents.length - recoveryStart).toBeGreaterThan(6);
    expect(intents.slice(recoveryStart).every((direction) => direction === 'neutral')).toBe(true);
    await page.keyboard.up('KeyW');

    for (const [label, direction] of [['Drive Forward', 'forward'], ['Drive Reverse', 'reverse']]) {
      const button = page.locator(`button[aria-label^="${label}"]`);
      await button.hover();
      const start = intents.length;
      await page.mouse.down();
      await expect.poll(() => intents.slice(start).includes(direction)).toBe(true);
      await page.mouse.up();
      await expect.poll(() => intents.at(-1)).toBe('neutral');
      await expect(button).toBeEnabled();
    }
    await expect(page.locator('.guard-armed')).toBeVisible();
  });

});
