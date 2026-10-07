import { test, expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { resolve } from 'node:path';

const api = 'http://127.0.0.1:18283';
let fixture: ChildProcess | undefined;
let fixtureOutput = '';

test.beforeAll(async () => {
  if (!process.env.OPENTALK_TEST_RTC) return;
  fixture = spawn(resolve('../.venv/bin/python'), [resolve('tests/fixtures/native_session.py')], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  await new Promise<void>((ready, reject) => {
    let output = '';
    const timeout = setTimeout(
      () => reject(new Error(`Native fixture timed out: ${output}`)),
      15000,
    );
    fixture!.stdout!.on('data', (chunk) => {
      if (String(chunk).includes('Native fixture ready')) {
        clearTimeout(timeout);
        ready();
      }
    });
    fixture!.stderr!.on('data', (chunk) => {
      output += chunk;
      fixtureOutput = output;
    });
    fixture!.once('error', (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    fixture!.once('exit', (code) => {
      clearTimeout(timeout);
      reject(new Error(`Native fixture exited (${code}): ${output}`));
    });
  });
});

test.afterEach(async ({}, info) => {
  if (info.status !== info.expectedStatus)
    await info.attach('Native fixture logs', { body: fixtureOutput, contentType: 'text/plain' });
});

test.afterAll(async () => {
  if (!fixture || fixture.exitCode !== null) return;
  await new Promise<void>((done) => {
    fixture!.once('exit', () => done());
    fixture!.kill('SIGTERM');
  });
});

test('real RoomIO and SQLite automatically save, reopen, resume and skip TTS in text mode', async ({
  page,
  request,
}) => {
  test.skip(
    !process.env.OPENTALK_TEST_RTC,
    'Set OPENTALK_TEST_RTC=1 for native offline integration tests.',
  );
  test.setTimeout(60000);
  let id = '';
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/control/**', async (route) => {
    const browser = route.request();
    const path = browser.url().split('/api/control')[1];
    let data: string | undefined = browser.postData() || undefined;
    if (browser.method() === 'POST' && path === '/sessions')
      data = JSON.stringify({ ...JSON.parse(data!), agent: 'conversation' });
    const response = await request.fetch(api + path, {
      method: browser.method(),
      headers: { 'Content-Type': 'application/json' },
      data,
    });
    const json = await response.json();
    if (browser.method() === 'POST' && path === '/sessions' && response.ok())
      id = json.session.session_id;
    await route.fulfill({ status: response.status(), json });
  });
  await page.goto('/');
  const input = page.getByRole('textbox', { name: 'Message', exact: true });
  await input.fill('Remember this conversation');
  await input.press('Enter');
  await expect(page.locator('.message.assistant')).toHaveText(
    'I checked the request. Please review the result.',
    { timeout: 15000 },
  );
  expect((await (await request.get(`${api}/probe`)).json())[id]).toBe(0);
  await expect
    .poll(
      async () => (await (await request.get(`${api}/sessions/${id}/history`)).json()).items.length,
    )
    .toBe(2);
  const first = (await (await request.get(`${api}/sessions/${id}`)).json()).session;
  await page.reload();
  await expect(input).toBeEnabled({ timeout: 15000 });
  await expect(page.getByRole('log')).toContainText('Remember this conversation');
  expect((await (await request.get(`${api}/sessions/${id}`)).json()).session.status).toBe(
    'completed',
  );
  const original = id;
  await input.fill('Continue automatically');
  await input.press('Enter');
  await expect(page.locator('.message.assistant')).toHaveCount(2, { timeout: 15000 });
  expect(id).toBe(original);
  const resumed = (await (await request.get(`${api}/sessions/${id}`)).json()).session;
  expect(resumed.attempt_id).not.toBe(first.attempt_id);
  expect((await (await request.get(`${api}/probe`)).json())[id]).toBe(0);
  await page.getByRole('button', { name: 'New chat' }).click();
  await expect(page.getByRole('log')).toBeEmpty();
  expect((await (await request.get(`${api}/sessions/${id}`)).json()).session.status).toBe(
    'completed',
  );
  await expect(page.getByRole('button', { name: /Resume session|End conversation/ })).toHaveCount(
    0,
  );
  expect(errors).toEqual([]);
  await page.goto('about:blank');
  await page.unrouteAll({ behavior: 'wait' });
});
