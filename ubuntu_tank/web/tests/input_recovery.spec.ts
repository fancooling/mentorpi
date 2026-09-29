import { test, expect, type Page } from '@playwright/test';
const forward = (page: Page) => page.getByRole('button', { name: 'Drive Forward (Hold W)' });
async function arm(page: Page) {
    await page.goto('/');
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await expect(page.locator('.btn-arm')).toBeEnabled();
    await page.locator('.btn-arm').click();
    await expect(forward(page)).toBeEnabled();
}
async function status(page: Page) { return (await page.request.get('/api/v1/status')).json(); }
test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/reset'); });
test('combined Take control starts once without arming; Stop works during setup', async ({ page, request }) => {
    await request.post('/api/v1/test/lifecycle', { data: { active: false, delay: 0.8 } });
    let acquisitions = 0, starts = 0, arms = 0;
    page.on('request', req => {
        if (req.url().endsWith('/control/acquire'))
            acquisitions++;
        if (req.url().endsWith('/controller/start'))
            starts++;
        if (req.url().endsWith('/control/start'))
            arms++;
    });
    await page.goto('/');
    await expect(page.getByRole('button', { name: 'Start Controller Service' })).toHaveCount(0);
    const take = page.getByRole('button', { name: 'Take Control Authority' });
    await expect(take).toBeEnabled();
    await take.evaluate(button => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
    await expect(take).toBeDisabled();
    await page.getByRole('button', { name: 'Stop Controller Service' }).click();
    await page.waitForTimeout(1100);
    expect(acquisitions).toBe(1);
    expect(starts).toBe(0);
    expect(arms).toBe(0);
    await expect.poll(async () => (await status(page)).service_state).toBe('inactive');
    await expect.poll(async () => (await status(page)).active_owner).toBeNull();
    await take.click();
    await expect(page.locator('.btn-arm')).toBeEnabled();
    expect((await status(page)).guard_armed).toBe(false);
    expect(arms).toBe(0);
});
test('late acquisition response after Stop cannot restore ownership', async ({ page }) => {
    await page.route('**/control/acquire', async (route) => {
        const response = await route.fetch();
        await new Promise(resolve => setTimeout(resolve, 700));
        await route.fulfill({ response });
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await page.locator('.btn-stop').click();
    await page.waitForTimeout(1000);
    await expect.poll(async () => (await status(page)).active_owner).toBeNull();
    await expect(page.locator('.btn-arm')).toBeDisabled();
});
for (const failure of ['startup', 'binding']) {
    test(`${failure} failure releases setup and permits retry`, async ({ page, request }) => {
        if (failure === 'startup')
            await request.post('/api/v1/test/lifecycle', { data: { active: false, fail: true } });
        else
            await page.routeWebSocket('**/api/v1/control', socket => {
                const server = socket.connectToServer();
                socket.onMessage(message => {
                    const frame = JSON.parse(message.toString());
                    if (frame.action === 'bind')
                        frame.payload.bind_token = 'invalid';
                    server.send(JSON.stringify(frame));
                });
            });
        await page.goto('/');
        await page.getByRole('button', { name: 'Take Control Authority' }).click();
        await expect(page.locator('.feedback-error')).toBeVisible();
        await expect.poll(async () => (await status(page)).active_owner).toBeNull();
        await expect(page.locator('.btn-arm')).toBeDisabled();
        if (failure === 'startup') {
            await request.post('/api/v1/test/lifecycle', { data: { active: false, fail: false } });
            await page.getByRole('button', { name: 'Take Control Authority' }).click();
            await expect(page.locator('.btn-arm')).toBeEnabled();
        }
    });
}
test('450ms network bursts retain Arm and keep intent bounded', async ({ page }) => {
    let delayed = false, sent = 0, outstanding = 0, maximum = 0;
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        socket.onMessage(message => {
            const frame = JSON.parse(message.toString());
            if (frame.action === 'intent' && delayed) {
                sent++;
                maximum = Math.max(maximum, ++outstanding);
                setTimeout(() => { outstanding--; server.send(message); }, 450);
            }
            else
                server.send(message);
        });
    });
    await arm(page);
    delayed = true;
    for (let i = 0; i < 3; i++) {
        await forward(page).dispatchEvent('pointerdown', { pointerId: 1 });
        await page.waitForTimeout(500);
        await forward(page).dispatchEvent('pointerup', { pointerId: 1 });
        await page.waitForTimeout(500);
        expect((await status(page)).guard_armed).toBe(true);
    }
    expect(sent).toBeGreaterThan(2);
    expect(maximum).toBe(1);
});
for (const method of ['keyboard', 'touch']) {
    test(`repeated 1.1–1.5s gaps preserve Arm and require release/new ${method} press`, async ({ page }) => {
        let drop = false, arms = 0;
        const delivered: string[] = [];
        page.on('request', req => { if (req.url().endsWith('/control/start'))
            arms++; });
        await page.routeWebSocket('**/api/v1/control', socket => {
            const server = socket.connectToServer();
            socket.onMessage(message => {
                const frame = JSON.parse(message.toString());
                if (frame.action === 'intent') {
                    if (drop)
                        return;
                    delivered.push(frame.payload.direction);
                }
                server.send(message);
            });
        });
        await arm(page);
        for (let cycle = 0; cycle < 2; cycle++) {
            await page.locator('.drive-panel').focus();
            if (method === 'keyboard')
                await page.keyboard.down('KeyW');
            else
                await forward(page).dispatchEvent('pointerdown', { pointerId: 5, pointerType: 'touch' });
            await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
            drop = true;
            await page.waitForTimeout(cycle === 0 ? 1100 : 1500);
            await expect(page.locator('.recovery-status')).toContainText('release controls');
            expect((await status(page)).operator_state).toBe('INPUT_PAUSED');
            expect((await status(page)).guard_armed).toBe(true);
            drop = false;
            const start = delivered.length;
            if (method === 'keyboard') {
                await page.keyboard.down('KeyW'); // OS repeat cannot revive a held key.
                await page.waitForTimeout(150);
                await page.keyboard.up('KeyW');
            }
            else
                await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 5, pointerType: 'touch' })));
            await expect(forward(page)).toBeEnabled();
            await expect.poll(async () => (await status(page)).operator_state).toBe('ARMED_IDLE');
            expect(delivered.slice(start).every(dir => dir === 'neutral')).toBe(true);
        }
        expect(arms).toBe(1);
    });
}
test('release while stalled and delayed neutral ack cannot reuse an early press', async ({ page }) => {
    let drop = false, delayAck = false;
    const delivered: string[] = [];
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        socket.onMessage(message => {
            const frame = JSON.parse(message.toString());
            if (frame.action === 'intent') {
                if (drop)
                    return;
                delivered.push(frame.payload.direction);
            }
            server.send(message);
        });
        server.onMessage(message => {
            const frame = JSON.parse(message.toString());
            if (delayAck && frame.type === 'ack' && frame.payload.action === 'intent') {
                setTimeout(() => socket.send(message), 400);
            }
            else
                socket.send(message);
        });
    });
    await arm(page);
    await page.locator('.drive-panel').focus();
    await page.keyboard.down('KeyW');
    await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
    drop = true;
    await page.keyboard.up('KeyW');
    await page.waitForTimeout(1300);
    await expect(page.locator('.recovery-status')).toContainText('confirming neutral');
    delayAck = true;
    drop = false;
    await page.waitForTimeout(80);
    await page.keyboard.down('KeyW');
    const start = delivered.length;
    await page.waitForTimeout(650);
    expect(delivered.slice(start).every(dir => dir === 'neutral')).toBe(true);
    await page.keyboard.up('KeyW');
    delayAck = false;
    await expect(forward(page)).toBeEnabled();
    await page.keyboard.down('KeyW');
    await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
    await page.keyboard.up('KeyW');
});
test('PWA hide/resume requires explicit Arm and fresh input', async ({ page }) => {
    await arm(page);
    await page.locator('.drive-panel').focus();
    await page.keyboard.down('KeyW');
    await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
    await expect.poll(async () => (await status(page)).guard_armed).toBe(false);
    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    await page.keyboard.down('KeyW');
    expect((await status(page)).guard_armed).toBe(false);
    await page.keyboard.up('KeyW');
});
test('lost neutral acknowledgments recover without another Arm', async ({ page }) => {
    let loseAcks = false;
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        server.onMessage(message => {
            const frame = JSON.parse(message.toString());
            if (loseAcks && frame.type === 'ack' && frame.payload.action === 'intent')
                return;
            socket.send(message);
        });
    });
    await arm(page);
    loseAcks = true;
    await page.waitForTimeout(1400);
    loseAcks = false;
    await expect(forward(page)).toBeEnabled();
    await expect.poll(async () => (await status(page)).operator_state).toBe('ARMED_IDLE');
    expect((await status(page)).guard_armed).toBe(true);
});
test('guard fault during recovery disarms instead of resuming', async ({ page, request }) => {
    let drop = false;
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        socket.onMessage(message => {
            if (drop && JSON.parse(message.toString()).action === 'intent')
                return;
            server.send(message);
        });
    });
    await arm(page);
    await page.locator('.drive-panel').focus();
    await page.keyboard.down('KeyW');
    drop = true;
    await page.waitForTimeout(1300);
    await request.post('/api/v1/test/lifecycle', { data: { guard_failure: true } });
    await expect.poll(async () => (await status(page)).operator_state).toBe('FAULT');
    drop = false;
    await page.keyboard.up('KeyW');
    expect((await status(page)).guard_armed).toBe(false);
    await expect(page.locator('.guard-fault')).toBeVisible();
    await expect(forward(page)).toBeDisabled();
});
test('socket replacement is ownerless and disarmed until explicit reacquisition', async ({ page }) => {
    let closeConnection = () => { };
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        closeConnection = () => { socket.close(); server.close(); };
    });
    await arm(page);
    closeConnection();
    await expect.poll(async () => (await status(page)).active_owner).toBeNull();
    await expect(page.getByRole('button', { name: 'Take Control Authority' })).toBeEnabled();
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await expect(page.locator('.btn-arm')).toBeEnabled();
    expect((await status(page)).guard_armed).toBe(false);
    await expect(forward(page)).toBeDisabled();
});
test('operation polling timeout cancels setup without enabling Arm', async ({ page }) => {
    test.setTimeout(35000);
    await page.route('**/operations/*', async (route) => {
        const url = new URL(route.request().url());
        await route.fulfill({ json: { operation_id: url.pathname.split('/').at(-1), status: 'pending', success: true } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await expect(page.locator('.feedback-error')).toContainText('timed out', { timeout: 26000 });
    await expect(page.locator('.btn-arm')).toBeDisabled();
    await expect.poll(async () => (await status(page)).active_owner).toBeNull();
});
test('delayed server frames cannot revive buffered motion after expiry', async ({ page }) => {
    let stalled = false;
    let flush = () => { };
    const buffered: (string | Buffer)[] = [];
    const sent: string[] = [];
    await page.routeWebSocket('**/api/v1/control', socket => {
        const server = socket.connectToServer();
        socket.onMessage(message => {
            const frame = JSON.parse(message.toString());
            if (frame.action === 'intent')
                sent.push(frame.payload.direction);
            server.send(message);
        });
        server.onMessage(message => {
            if (stalled)
                buffered.push(message);
            else
                socket.send(message);
        });
        flush = () => { stalled = false; for (const message of buffered.splice(0))
            socket.send(message); };
    });
    await arm(page);
    await page.locator('.drive-panel').focus();
    await page.keyboard.down('KeyW');
    await expect.poll(async () => (await status(page)).operator_state).toBe('DRIVING');
    stalled = true;
    await page.waitForTimeout(1200);
    await page.keyboard.up('KeyW');
    const boundary = sent.length;
    flush();
    await expect.poll(async () => (await status(page)).operator_state).toBe('ARMED_IDLE');
    await expect(forward(page)).toBeEnabled();
    expect(sent.slice(boundary).every(direction => direction === 'neutral')).toBe(true);
    expect((await status(page)).guard_armed).toBe(true);
});

test('protocol-3 release failure preserves the bound session for retry', async ({ page, request }) => {
    const retired: string[] = [];
    page.on('request', req => {
        if (req.url().endsWith('/control/arm') || req.url().endsWith('/controller/stop'))
            retired.push(req.url());
    });
    await arm(page);
    await request.post('/api/v1/test/lifecycle', { data: { stop_fail: true } });
    await page.getByRole('button', { name: 'Release Control Authority' }).click();
    await expect(page.locator('.feedback-error')).toBeVisible();
    expect((await status(page)).release_progress).toBe('shutdown_failed');
    await expect(page.locator('.owner-self')).toBeVisible();
    await request.post('/api/v1/test/lifecycle', { data: { stop_fail: false } });
    await page.getByRole('button', { name: 'Release Control Authority' }).click();
    await expect.poll(async () => (await status(page)).service_state).toBe('inactive');
    await expect(page.locator('.owner-none')).toBeVisible();
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await expect(page.locator('.btn-arm')).toBeEnabled();
    expect(retired).toEqual([]);
});

test('canceling slow setup reports shutdown only after completion', async ({ page, request }) => {
    await request.post('/api/v1/test/lifecycle', { data: { active: false, delay: 1.5 } });
    await page.goto('/');
    await page.getByRole('button', { name: 'Take Control Authority' }).click();
    await expect.poll(async () => (await status(page)).service_state).toBe('inactive');
    await page.getByRole('button', { name: 'Stop Controller Service' }).click();
    await expect(page.locator('.feedback-banner')).not.toContainText('controller stopped');
    await expect(page.locator('.feedback-banner')).toContainText('controller stopped', { timeout: 8000 });
    expect((await status(page)).service_state).toBe('inactive');
    expect((await status(page)).active_owner).toBeNull();
});
