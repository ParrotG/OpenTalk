import { test as base, expect, chromium } from '@playwright/test';

const test = base.extend({
  browser: async ({ browser }, use) => {
    if (!process.env.OPENTALK_TEST_CDP_URL) return use(browser);
    const remote = await chromium.connectOverCDP(process.env.OPENTALK_TEST_CDP_URL);
    try {
      await use(remote);
    } finally {
      await remote.close();
    }
  },
});

test('container dispatch, SQL tools, media, automatic resume and SQLite persistence', async ({
  page,
  request,
}) => {
  test.skip(!process.env.OPENTALK_TEST_DOCKER, 'Start the isolated compose.verify.yaml stack.');
  test.setTimeout(90000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const peers: RTCPeerConnection[] = [];
    Object.assign(window, { dockerTestPeers: peers });
    const NativePeerConnection = window.RTCPeerConnection;
    window.RTCPeerConnection = new Proxy(NativePeerConnection, {
      construct(target, args) {
        const peer = new target(...args);
        peers.push(peer);
        return peer;
      },
    });
  });
  const services = await request.get('/api/control/health/services');
  expect((await services.json()).status).toBe('ok');
  await page.goto('/');
  const input = page.getByRole('textbox', { name: 'Message', exact: true });
  await expect(input).toBeEnabled();
  await input.fill('List resources');
  await input.press('Enter');
  await expect(page.locator('.message.assistant').last()).toContainText(
    'I checked the request. Please review the result.',
    { timeout: 30000 },
  );
  const sessions = await (await request.get('/api/control/sessions?limit=20')).json();
  const id: string = sessions[0].session_id;
  const snapshot = async () =>
    (await (await request.get(`/api/control/sessions/${id}`)).json()).session;
  const first = await snapshot();
  const history = async () =>
    (await (await request.get(`/api/control/sessions/${id}/history`)).json()).items;
  await expect
    .poll(
      async () =>
        (await history()).filter((item: { type: string }) => item.type === 'function_call').length,
    )
    .toBe(1);
  const calls = (await history()).filter((item: { type: string }) => item.type === 'function_call');
  expect(calls[0].name).toBe('query');
  await page.goto('about:blank');
  await expect.poll(async () => (await snapshot()).status, { timeout: 20000 }).toBe('completed');
  await page.goto('/');
  await expect(page.locator('.message.user')).toContainText('List resources');
  await input.fill('Continue automatically');
  await input.press('Enter');
  await expect
    .poll(async () => (await snapshot()).attempt_id, { timeout: 20000 })
    .not.toBe(first.attempt_id);
  await expect(page.locator('.message.assistant').last()).toContainText(
    'Please review the result.',
  );
  await page.getByRole('button', { name: 'Start voice' }).click();
  await expect(page.getByRole('button', { name: 'Cancel voice' })).toBeEnabled({ timeout: 20000 });
  await expect
    .poll(
      async () =>
        (await history()).some(
          (item: { type: string; role?: string; content?: string[] }) =>
            item.type === 'message' && item.role === 'user' && item.content?.includes('hello'),
        ),
      { timeout: 20000 },
    )
    .toBe(true);
  await expect
    .poll(
      () =>
        page.evaluate(async () => {
          const peers = (window as unknown as { dockerTestPeers: RTCPeerConnection[] })
            .dockerTestPeers;
          let packets = 0;
          for (const peer of peers) {
            const stats = await peer.getStats();
            stats.forEach((item) => {
              if (item.type === 'inbound-rtp' && item.kind === 'audio')
                packets += item.packetsReceived || 0;
            });
          }
          return packets;
        }),
      { timeout: 20000 },
    )
    .toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Cancel voice' }).click();
  await expect(input).toBeVisible();
  await page.goto('about:blank');
  await expect.poll(async () => (await snapshot()).status, { timeout: 20000 }).toBe('completed');
  expect(
    (await history()).filter((item: { type: string }) => item.type === 'function_call'),
  ).toHaveLength(1);
  expect(errors).toEqual([]);
});
