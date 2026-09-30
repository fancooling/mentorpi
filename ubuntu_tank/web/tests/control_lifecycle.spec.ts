import { test, expect, type Page } from '@playwright/test';

// Real HTTP/WS/IPC with simulated controller telemetry; no robot hardware.
const status = async (page: Page) => (await page.request.get('/api/v1/status')).json();
const start = (page: Page) => page.getByRole('button', { name: 'Start', exact: true });
const stop = (page: Page) => page.getByRole('button', { name: 'Stop', exact: true });
const take = async (page: Page) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await expect(start(page)).toBeEnabled();
};
test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/reset'); });

test('Take, Start, direction/release, Stop, Start, Release confirms shutdown', async ({ page }) => {
  await take(page);
  await expect(page.getByRole('button', { name: /Arm Chassis|Disarm Chassis|Stop Controller/ })).toHaveCount(0);
  await start(page).click();
  const forward = page.getByRole('button', { name: 'Drive Forward (Hold W)' });
  await expect(forward).toBeEnabled();
  await forward.dispatchEvent('pointerdown', { pointerId: 1 });
  await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
  await forward.dispatchEvent('pointerup', { pointerId: 1 });
  await expect.poll(async () => (await status(page)).operator_state).toBe('ARMED_IDLE');
  await stop(page).click();
  await expect(start(page)).toBeEnabled();
  expect((await status(page)).active_owner).not.toBeNull();
  await start(page).click();
  await expect(forward).toBeEnabled();
  await forward.dispatchEvent('pointerdown', { pointerId: 2 });
  await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-banner')).toContainText('controller stopped');
  expect((await status(page)).service_state).toBe('inactive');
  await expect(stop(page)).toBeEnabled();
});

test('Space on focused Start never starts; double-click cannot rearm after Stop', async ({ page }) => {
  await take(page);
  let starts = 0;
  page.on('request', req => { if (req.url().endsWith('/control/start')) starts++; });
  await start(page).focus();
  await page.keyboard.press('Space');
  await page.waitForTimeout(300);
  expect(starts).toBe(0);
  expect((await status(page)).guard_armed).toBe(false);
  await start(page).dblclick();
  await expect.poll(async () => (await status(page)).guard_armed).toBe(false);
  expect(starts).toBe(1);
});

test('late Start response cannot defeat Stop', async ({ page }) => {
  await take(page);
  let unblock!: () => void;
  const gate = new Promise<void>(resolve => { unblock = resolve; });
  await page.route('**/api/v1/control/start', async route => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await start(page).click();
  await expect(stop(page)).toBeEnabled();
  await stop(page).click();
  unblock();
  await expect(start(page)).toBeEnabled();
  expect((await status(page)).guard_armed).toBe(false);
});

test('inactivity releases despite status polls and neutral traffic; another tab can take', async ({ page, request, context }) => {
  await request.post('/api/v1/test/lifecycle', { data: { idle_timeout: 2 } });
  await take(page);
  await start(page).click();
  await expect(page.locator('.guard-armed')).toBeVisible();
  await expect.poll(async () => (await status(page)).service_state, { timeout: 7000 }).toBe('inactive');
  await expect(page.locator('.owner-none')).toBeVisible();
  await expect(page.locator('.feedback-banner')).toContainText(/inactivity/);
  await expect(start(page)).toHaveCount(0);
  const other = await context.newPage();
  await take(other);
  expect((await status(other)).guard_armed).toBe(false);
});

test('failed polling drops control and preserves observer Stop', async ({ page }) => {
  await take(page);
  await start(page).click();
  await page.route('**/api/v1/status', route => route.abort());
  await expect(page.locator('.conn-disconnected')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Drive Forward (Hold W)' })).toBeDisabled();
  await expect(start(page)).toHaveCount(0);
  await expect(stop(page)).toBeEnabled();
  await page.unroute('**/api/v1/status');
  await expect(page.locator('.conn-connected')).toBeVisible();
  await expect(page.locator('.owner-none')).toBeVisible();
  expect((await status(page)).guard_armed).toBe(false);
});

test('out-of-order status cannot restore expired ownership', async ({ page, request }) => {
  await request.post('/api/v1/test/lifecycle', { data: { idle_timeout: 2 } });
  await take(page);
  let delayed = false;
  let unblock!: () => void;
  const gate = new Promise<void>(resolve => { unblock = resolve; });
  await page.route('**/api/v1/status', async route => {
    const response = await route.fetch();
    if (!delayed) { delayed = true; await gate; }
    await route.fulfill({ response });
  });
  await expect(page.locator('.owner-none')).toBeVisible({ timeout: 7000 });
  unblock();
  await page.waitForTimeout(200);
  await expect(start(page)).toHaveCount(0);
  await expect(page.locator('.owner-none')).toBeVisible();
});

test('hidden page stops polling; resume polls immediately without reacquisition', async ({ page }) => {
  await take(page);
  await start(page).click();
  let polls = 0;
  page.on('request', req => { if (req.url().endsWith('/status')) polls++; });
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(300);
  const hiddenPolls = polls;
  await page.waitForTimeout(1200);
  expect(polls).toBe(hiddenPolls);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(page.locator('.conn-connected')).toBeVisible();
  expect(polls).toBeGreaterThan(hiddenPolls);
  await expect(start(page)).toHaveCount(0);
  expect((await status(page)).guard_armed).toBe(false);
});

test('Release waits for a delayed acquisition response and shutdown', async ({ page, request }) => {
  await request.post('/api/v1/test/lifecycle', { data: { active: false, delay: 1 } });
  let unblock!: () => void;
  const gate = new Promise<void>(resolve => { unblock = resolve; });
  await page.route('**/api/v1/control/acquire', async route => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-banner')).toContainText('waiting for controller shutdown');
  await expect(stop(page)).toBeEnabled();
  await page.waitForTimeout(300);
  await expect(page.locator('.feedback-banner')).not.toContainText('controller stopped');
  unblock();
  await expect(page.locator('.feedback-banner')).toContainText('controller stopped');
  expect((await status(page)).service_state).toBe('inactive');
});

test('runtime revision reset recovers telemetry without restoring the bound session', async ({ page, request }) => {
  await take(page);
  await start(page).click();
  await expect(page.locator('.guard-armed')).toBeVisible();
  await request.post('/api/v1/test/reset');
  await page.route('**/api/v1/status', async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, status_revision: 0 } });
  });
  await expect(page.locator('.owner-none')).toBeVisible();
  await expect(page.locator('.conn-connected')).toBeVisible();
  await expect(start(page)).toHaveCount(0);
  await expect(stop(page)).toBeEnabled();
  await page.unroute('**/api/v1/status');
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await expect(start(page)).toBeEnabled();
});

test('binding failure with failed cleanup retains a working Release retry', async ({ page }) => {
  let failBind = true;
  await page.routeWebSocket('**/api/v1/control', socket => {
    const server = socket.connectToServer();
    socket.onMessage(message => {
      const frame = JSON.parse(message.toString());
      if (failBind && frame.action === 'bind') frame.payload.bind_token = 'invalid';
      server.send(JSON.stringify(frame));
    });
  });
  await page.route('**/api/v1/control/release', route => route.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await expect(page.locator('.feedback-error')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Release Control Authority' })).toBeEnabled();
  await page.unroute('**/api/v1/control/release');
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-banner')).toContainText('controller stopped');
  failBind = false;
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await expect(start(page)).toBeEnabled();
});

test('browser and terminal IPC client hand off ownership without automatic Start', async ({ page, request }) => {
  await take(page);
  expect((await (await request.post('/api/v1/test/cli', { data: { action: 'acquire' } })).json()).success).toBe(false);
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-banner')).toContainText('controller stopped');
  expect((await (await request.post('/api/v1/test/cli', { data: { action: 'acquire' } })).json()).success).toBe(true);
  await expect(page.locator('.owner-other')).toBeVisible();
  await expect(start(page)).toHaveCount(0);
  expect((await (await request.post('/api/v1/test/cli', { data: { action: 'release' } })).json()).success).toBe(true);
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await expect(start(page)).toBeEnabled();
  expect((await status(page)).guard_armed).toBe(false);
});

test('waiting PWA update retries failed shutdown, then reloads disarmed and ownerless', async ({ page, request }) => {
  await page.goto('/');
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await take(page);
  await start(page).click();
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);
  await request.post('/api/v1/test/pwa-update');
  await page.evaluate(async () => { await (await navigator.serviceWorker.getRegistration())!.update(); });
  await expect(page.locator('.banner-update')).toBeVisible();
  await request.post('/api/v1/test/lifecycle', { data: { stop_fail: true } });
  await page.getByRole('button', { name: 'Update Now' }).click();
  await expect(page.locator('.feedback-error')).toBeVisible();
  await expect(page.locator('.banner-update')).toBeVisible();
  await expect(page.locator('.owner-self')).toBeVisible();
  await request.post('/api/v1/test/lifecycle', { data: { stop_fail: false } });
  await page.getByRole('button', { name: 'Update Now' }).click();
  await expect(page.locator('.banner-update')).toHaveCount(0);
  await expect(page.locator('.owner-none')).toBeVisible();
  await expect(start(page)).toHaveCount(0);
  expect((await status(page)).service_state).toBe('inactive');
});

test('failed Release during pending startup cannot claim inactive completion', async ({ page, request }) => {
  await request.post('/api/v1/test/lifecycle', { data: { active: false, delay: 1.5 } });
  await page.route('**/api/v1/control/release', route => route.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Take Control Authority' }).click();
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-error')).toContainText('shutdown unconfirmed');
  await expect(page.locator('.feedback-banner')).not.toContainText('controller stopped');
  await page.unroute('**/api/v1/control/release');
  await page.getByRole('button', { name: 'Release Control Authority' }).click();
  await expect(page.locator('.feedback-banner')).toContainText('controller stopped');
  expect((await status(page)).service_state).toBe('inactive');
});
