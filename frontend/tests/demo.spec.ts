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
  await expect(page.getByRole('button', { name: 'Start voice' })).toBeEnabled();
  await page.getByLabel('Recent sessions', { exact: true }).first().click();
  await page.getByRole('button', { name: /normal-sessi/ }).click();
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: /Resume session|End conversation/ })).toHaveCount(
    0,
  );
  await expect(page.getByRole('log')).toContainText('Hello <script>unsafe()</script>');
  await expect(page.locator('.message')).toHaveCount(2);
  await expect(page.getByRole('log')).not.toContainText('Assistant');
  await expect(page.getByText('Transcript', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Session details')).toHaveCount(0);
  await page.getByRole('button', { name: /failed-sessi/ }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toHaveCount(0);
  await expect(page.locator('.archive-notice')).toContainText('cannot be resumed');
  await expect(page.getByText(/Booking|Reservation/)).toHaveCount(0);
});

test('unavailable dependencies prevent voice allocation and are shown clearly', async ({
  page,
}) => {
  await mockControl(page, false);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start voice' })).toBeDisabled();
  await page.getByLabel('Services', { exact: true }).click();
  await expect(page.getByLabel('Backend services')).toContainText('unavailable');
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeDisabled();
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
  await page.getByRole('button', { name: 'Start voice' }).click();
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

test('system theme changes immediately and mobile utility panels collapse', async ({ page }) => {
  await mockControl(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(await background()).toBe('rgb(255, 255, 255)');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(background).toBe('rgb(33, 33, 33)');
  const recent = page.getByLabel('Recent sessions', { exact: true }).first();
  const services = page.getByLabel('Services', { exact: true });
  await recent.click();
  await expect(page.getByRole('button', { name: /normal-sessi/ })).toBeVisible();
  await services.click();
  await expect(page.getByRole('button', { name: /normal-sessi/ })).toBeHidden();
  await expect(page.getByLabel('Backend services')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByLabel('Backend services')).toBeHidden();
  await expect(services).toBeFocused();
  await recent.click();
  await page.getByRole('textbox', { name: 'Message', exact: true }).click();
  await expect(page.getByRole('button', { name: /normal-sessi/ })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/demo-dark-mobile.png' });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: 'test-results/demo-light-desktop.png' });
});

test('user pauses share a bubble until an actual assistant message appears', async ({ page }) => {
  await mockControl(page);
  await page.route('**/api/control/sessions/normal-session/history?**', (route) =>
    route.fulfill({
      json: {
        items: [
          { id: 'u1', type: 'message', role: 'user', content: ['First fragment'] },
          { id: 'empty', type: 'message', role: 'assistant', content: ['  '] },
          { id: 'u2', type: 'message', role: 'user', content: ['Second fragment'] },
          { id: 'a1', type: 'message', role: 'assistant', content: ['An actual reply'] },
          { id: 'u3', type: 'message', role: 'user', content: ['Another turn'] },
          { id: 'u3', type: 'message', role: 'user', content: ['Another turn, revised'] },
        ],
      },
    }),
  );
  await page.goto('/');
  await page.getByLabel('Recent sessions', { exact: true }).first().click();
  await page.getByRole('button', { name: /normal-sessi/ }).click();
  await expect(page.locator('.message.user')).toHaveCount(2);
  await expect(page.locator('.message.user').first()).toHaveText('First fragment\nSecond fragment');
  await expect(page.locator('.message.user').last()).toHaveText('Another turn, revised');
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  const styles = await page.locator('.message.assistant').evaluate((element) => ({
    border: getComputedStyle(element).borderWidth,
    background: getComputedStyle(element).backgroundColor,
  }));
  expect(styles).toEqual({ border: '0px', background: 'rgba(0, 0, 0, 0)' });
  await page.locator('.message.assistant').click();
  await page.screenshot({ path: 'test-results/demo-transcript-desktop.png' });
});

test('reopening restores saved history without allocating a room or requesting a microphone', async ({
  page,
}) => {
  await mockControl(page);
  let creates = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST') creates++;
  });
  await page.addInitScript(() =>
    localStorage.setItem(
      'opentalk.currentSession',
      JSON.stringify({
        id: 'normal-session',
        attempt: 'attempt-normal-session',
        closing: false,
      }),
    ),
  );
  await page.goto('/');
  await expect(page.getByRole('log')).toContainText('Welcome back.');
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: /Resume session|End conversation/ })).toHaveCount(
    0,
  );
  expect(creates).toBe(0);
});

test('a different active attempt is never automatically ended or resumed', async ({ page }) => {
  await mockControl(page);
  await page.addInitScript(() =>
    localStorage.setItem(
      'opentalk.currentSession',
      JSON.stringify({
        id: 'normal-session',
        attempt: 'old-attempt',
        closing: true,
      }),
    ),
  );
  await page.route('**/api/control/sessions/normal-session', (route) =>
    route.fulfill({
      json: { session: { ...saved('normal-session', 'active'), attempt_id: 'another-attempt' } },
    }),
  );
  let ends = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST') ends++;
  });
  await page.goto('/');
  await expect(page.locator('.archive-notice')).toContainText('another connection');
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeDisabled();
  expect(ends).toBe(0);
  await page.getByRole('button', { name: 'New chat' }).click();
  await expect(page.getByRole('log')).toBeEmpty();
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeEnabled();
  expect(ends).toBe(0);
});
