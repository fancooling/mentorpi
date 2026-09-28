import { test, expect } from '@playwright/test';

// M14.3 changes the wire contract. Browser recovery and setup migrate in M14.4.
test('protocol-1 browser cannot acquire or arm the protocol-2 runtime', async ({ page, request }) => {
  const version = await (await request.get('/api/v1/version')).json();
  expect(version.protocol_version).toBe('2.0.0');
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Take Control Authority' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Arm Chassis Motors', exact: true })).toBeDisabled();
  const response = await request.post('/api/v1/control/acquire', {
    headers: { Origin: new URL(page.url()).origin },
    data: { request_id: 'obsolete-browser', operator_id: 'obsolete-browser', protocol_version: '1.0.0' },
  });
  expect(response.status()).toBe(422);
  const state = await (await request.get('/api/v1/status')).json();
  expect(state.active_owner).toBeNull();
  expect(state.guard_armed).toBe(false);
});
