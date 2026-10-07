import { test, expect, Page } from '@playwright/test';

const saved = (id: string, status: string) => ({
  session_id: id,
  attempt_id: `attempt-${id}`,
  status,
  created_at: 1780000000,
  updated_at: 1780000000,
  end_requested: false,
});
async function mockControl(page: Page, available = true) {
  await page.route('**/api/control/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path.endsWith('/health/services'))
      body = {
        status: available ? 'ok' : 'degraded',
        services: {
          sessions: { status: 'ok' },
          livekit: { status: available ? 'ok' : 'unavailable' },
          worker: { status: 'ok' },
          providers: { status: 'configured' },
        },
      };
    else if (path.endsWith('/sessions'))
      body = [saved('normal-session', 'completed'), saved('failed-session', 'failed')];
    else if (path.endsWith('/history'))
      body = {
        items: [
          {
            id: 'first',
            type: 'message',
            role: 'user',
            content: ['Hello <script>unsafe()</script>'],
          },
          { id: 'tool', type: 'function_call', name: 'query' },
          {
            id: 'second',
            type: 'message',
            role: 'assistant',
            content: ['Welcome back.'],
            interrupted: true,
          },
        ],
      };
    else
      body = {
        session: saved(
          path.split('/').pop()!,
          path.includes('failed-session') ? 'failed' : 'completed',
        ),
      };
    await route.fulfill({ json: body });
  });
}

test('history displays native messages as plain text and only completed sessions resume', async ({
  page,
}) => {
  await mockControl(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start conversation' })).toBeEnabled();
  await page.getByRole('button', { name: /normal-sessi/ }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeEnabled();
  await expect(page.getByRole('log')).toContainText('Hello <script>unsafe()</script>');
  await expect(page.locator('.message')).toHaveCount(2);
  await expect(page.getByRole('log')).toContainText('Interrupted');
  await page.getByRole('button', { name: /failed-sessi/ }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toHaveCount(0);
  await expect(page.getByLabel('Session details')).toContainText('cannot be resumed');
  await expect(page.getByText(/Booking|Reservation/)).toHaveCount(0);
});

test('unavailable dependencies prevent voice allocation and are shown clearly', async ({
  page,
}) => {
  await mockControl(page, false);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start conversation' })).toBeDisabled();
  await expect(page.getByLabel('Backend services')).toContainText('unavailable');
});

test('microphone denial does not create a session and mobile layout fits', async ({ page }) => {
  await mockControl(page);
  let creates = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST') creates++;
  });
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException('Denied', 'NotAllowedError');
    };
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start conversation' }).click();
  await expect(page.locator('.error[role="alert"]')).toContainText('Allow microphone access');
  expect(creates).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('the real proxy accepts the browser host and rejects a foreign origin', async ({
  page,
  request,
}) => {
  await page.goto('/');
  const status = await page.evaluate(
    async () =>
      (
        await fetch('/api/control/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        })
      ).status,
  );
  expect(status).not.toBe(403);
  const rejected = await request.post('/api/control/sessions', {
    headers: { Origin: 'https://foreign.example' },
    data: {},
  });
  expect(rejected.status()).toBe(403);
});
