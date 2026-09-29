import { test, expect } from '@playwright/test';

// Old cached clients must be rejected before receiving ownership.
test('protocol-2 acquisition is rejected by the protocol-3 runtime', async ({ page, request }) => {
  const version = await (await request.get('/api/v1/version')).json();
  expect(version.protocol_version).toBe('3.0.0');
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Take Control Authority' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Arm Chassis Motors', exact: true })).toBeDisabled();
  const response = await request.post('/api/v1/control/acquire', {
    headers: { Origin: new URL(page.url()).origin },
    data: { request_id: 'obsolete-browser', operator_id: 'obsolete-browser', protocol_version: '2.0.0' },
  });
  expect(response.status()).toBe(422);
  const state = await (await request.get('/api/v1/status')).json();
  expect(state.active_owner).toBeNull();
  expect(state.guard_armed).toBe(false);
});
