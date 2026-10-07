import { test, expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { resolve } from 'node:path';

let fixture: ChildProcess | undefined;
const fixtureUrl = 'http://127.0.0.1:18183';

test.beforeAll(async () => {
  if (!process.env.OPENTALK_TEST_RTC) return;
  fixture = spawn(resolve('../.venv/bin/python'), [resolve('tests/fixtures/rtc_agent.py')], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  await new Promise<void>((ready, reject) => {
    let output = '';
    const timeout = setTimeout(() => reject(new Error(`RTC fixture timed out: ${output}`)), 15000);
    fixture!.stdout!.on('data', (chunk) => {
      if (String(chunk).includes('RTC fixture ready')) {
        clearTimeout(timeout);
        ready();
      }
    });
    fixture!.stderr!.on('data', (chunk) => (output += chunk));
    fixture!.once('error', (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    fixture!.once('exit', (code) => {
      clearTimeout(timeout);
      reject(new Error(`RTC fixture exited (${code}): ${output}`));
    });
  });
});

test.afterAll(async () => {
  if (!fixture || fixture.exitCode !== null) return;
  await new Promise<void>((done) => {
    fixture!.once('exit', () => done());
    fixture!.kill('SIGTERM');
  });
});

test('native text transport, user grouping, voice cancellation and resume share one session', async ({
  page,
  request,
}) => {
  test.skip(
    !process.env.OPENTALK_TEST_RTC,
    'Set OPENTALK_TEST_RTC=1 for isolated LiveKit protocol tests. No providers are called.',
  );
  test.setTimeout(60000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let sessionId = '';
  let creates = 0;
  await page.route('**/api/control/**', async (route) => {
    const browserRequest = route.request();
    const path = browserRequest.url().split('/api/control')[1];
    const response = await request.fetch(fixtureUrl + path, {
      method: browserRequest.method(),
      headers: { 'Content-Type': 'application/json' },
      data: browserRequest.postData() || undefined,
    });
    const json = await response.json();
    if (browserRequest.method() === 'POST' && path === '/sessions') {
      sessionId = json.session.session_id;
      creates++;
    }
    await route.fulfill({ status: response.status(), json });
  });
  await page.addInitScript(() => {
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    Object.assign(window, { microphoneRequests: 0 });
    navigator.mediaDevices.getUserMedia = (constraints) => {
      (window as unknown as { microphoneRequests: number }).microphoneRequests++;
      return getUserMedia(constraints);
    };
  });
  await page.goto('/');
  const input = page.getByRole('textbox', { name: 'Message', exact: true });
  await input.fill('First fragment');
  await input.press('Enter');
  const probe = async () => (await request.get(`${fixtureUrl}/sessions/${sessionId}/probe`)).json();
  await expect.poll(async () => (await probe()).received).toEqual(['First fragment']);
  expect(
    await page.evaluate(
      () => (window as unknown as { microphoneRequests: number }).microphoneRequests,
    ),
  ).toBe(0);
  await input.fill('Second fragment');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect
    .poll(async () => (await probe()).received)
    .toEqual(['First fragment', 'Second fragment']);
  await expect(page.locator('.message.user')).toHaveCount(1);
  await expect(page.locator('.message.user')).toHaveText('First fragment\nSecond fragment');
  const reply = await request.post(`${fixtureUrl}/sessions/${sessionId}/reply`, {
    data: { text: 'An actual reply.' },
  });
  expect(reply.ok()).toBe(true);
  await expect(page.locator('.message.assistant')).toHaveText('An actual reply.');
  await input.fill('Another turn');
  await input.press('Shift+Enter');
  await input.press('t');
  await input.press('Enter');
  await expect
    .poll(async () => (await probe()).received)
    .toEqual(['First fragment', 'Second fragment', 'Another turn\nt']);
  await expect(page.locator('.message.user')).toHaveCount(2);
  await page.getByRole('button', { name: 'Start voice' }).click();
  await expect(page.getByRole('button', { name: 'Cancel voice' })).toBeEnabled();
  await expect(input).toHaveCount(0);
  await expect(page.getByRole('status')).toHaveText('listening');
  await expect.poll(async () => (await probe()).microphones).toEqual([false]);
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: 'test-results/demo-voice-dark.png' });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/demo-voice-dark-mobile.png' });
  await page.getByRole('button', { name: 'Cancel voice' }).click();
  await expect(input).toBeEnabled();
  await expect.poll(async () => (await probe()).microphones).toEqual([true]);
  expect(creates).toBe(1);
  await page.getByRole('button', { name: 'End conversation' }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeEnabled();
  await expect(page.locator('.message.user')).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const originalId = sessionId;
  await input.fill('Continue from history');
  await input.press('Enter');
  await expect.poll(() => creates).toBe(2);
  expect(sessionId).toBe(originalId);
  await expect.poll(async () => (await probe()).received.at(-1)).toBe('Continue from history');
  await expect(page.locator('.message.assistant')).toHaveText('An actual reply.');
  await page.getByRole('button', { name: 'End conversation' }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeEnabled();
  expect(errors).toEqual([]);
  await page.goto('about:blank');
  await page.unrouteAll({ behavior: 'wait' });
});
