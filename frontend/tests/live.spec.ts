import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

declare global {
  interface Window {
    feedTestAudio: (url: string) => Promise<void>;
    testPeerConnections: RTCPeerConnection[];
  }
}

// This test is explicitly opt-in because it uses the configured paid providers.
test('real voice: input, playback, interruption, checkpoint and resumed context', async ({
  page,
  request,
}) => {
  test.skip(
    !process.env.OPENTALK_LIVE_AUDIO_DIR,
    'Set OPENTALK_LIVE_AUDIO_DIR and start the real services to run paid voice tests.',
  );
  test.setTimeout(180000);
  const directory = process.env.OPENTALK_LIVE_AUDIO_DIR!;
  for (const name of ['input', 'interrupt', 'resume']) {
    await page.route(`**/test-${name}.wav`, async (route) =>
      route.fulfill({
        contentType: 'audio/wav',
        body: await readFile(join(directory, `${name}.wav`)),
      }),
    );
  }
  await page.addInitScript(() => {
    // Replace only the microphone source; the production WebRTC/ASR/LLM/TTS flow is unchanged.
    let input: { context: AudioContext; destination: MediaStreamAudioDestinationNode };
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const destination = context.createMediaStreamDestination();
      input = { context, destination };
      const track = destination.stream.getAudioTracks()[0];
      const stop = track.stop.bind(track);
      track.stop = () => {
        stop();
        void context.close();
      };
      return destination.stream;
    };
    window.feedTestAudio = async (url) => {
      const bytes = await (await fetch(url)).arrayBuffer();
      const audio = await input.context.decodeAudioData(bytes);
      await input.context.resume();
      const source = input.context.createBufferSource();
      source.buffer = audio;
      source.connect(input.destination);
      source.start(input.context.currentTime + 0.5);
    };
    const NativePeerConnection = window.RTCPeerConnection;
    window.testPeerConnections = [];
    window.RTCPeerConnection = new Proxy(NativePeerConnection, {
      construct(target, args) {
        const connection = new target(...args);
        window.testPeerConnections.push(connection);
        return connection;
      },
    });
  });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start conversation' })).toBeEnabled({
    timeout: 15000,
  });
  await page.screenshot({ path: 'test-results/demo-desktop.png', fullPage: true });
  await page.getByRole('button', { name: 'Start conversation' }).click();
  await expect(page.getByRole('button', { name: 'Mute mic' })).toBeEnabled({ timeout: 50000 });
  const sessionId = (await page
    .getByLabel('Session details')
    .locator(':scope > code')
    .textContent())!;
  await expect(page.locator('.message.assistant')).toContainText('Hello', { timeout: 20000 });
  await expect
    .poll(
      () =>
        page.evaluate(async () => {
          let energy = 0;
          for (const connection of window.testPeerConnections) {
            const stats = await connection.getStats();
            stats.forEach((report) => {
              if (report.type === 'inbound-rtp' && report.kind === 'audio')
                energy += report.totalAudioEnergy || 0;
            });
          }
          return energy;
        }),
      { timeout: 20000 },
    )
    .toBeGreaterThan(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        [...document.querySelectorAll('audio')].some(
          (audio) => !audio.paused && audio.readyState >= 2,
        ),
      ),
    )
    .toBe(true);
  await expect(page.getByRole('status')).toHaveText('listening', { timeout: 15000 });
  await page.getByRole('button', { name: 'Mute mic' }).click();
  await expect(page.getByRole('button', { name: 'Unmute mic' })).toBeEnabled();
  await page.getByRole('button', { name: 'Unmute mic' }).click();
  await page.evaluate(() => window.feedTestAudio('/test-input.wav'));
  await expect(page.getByRole('log')).toContainText(/alice/i, { timeout: 30000 });
  await expect(page.getByRole('status')).toHaveText('speaking', { timeout: 30000 });
  await page.evaluate(() => window.feedTestAudio('/test-interrupt.wav'));
  await expect(page.locator('.message.user').last()).toContainText(/stop the story/i, {
    timeout: 25000,
  });
  await expect(page.locator('.message.assistant').last()).toContainText(/ready/i, {
    timeout: 30000,
  });
  await page.screenshot({ path: 'test-results/demo-active.png', fullPage: true });
  await page.getByRole('button', { name: 'End conversation' }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeEnabled({
    timeout: 25000,
  });
  const first = await (await request.get(`/api/control/sessions/${sessionId}`)).json();
  const archive = await (
    await request.get(`/api/control/sessions/${sessionId}/history?tail=true&limit=200`)
  ).json();
  expect(first.session.status).toBe('completed');
  expect(archive.items.some((item: { interrupted?: boolean }) => item.interrupted)).toBe(true);
  const attemptId = first.session.attempt_id;
  await page.getByRole('button', { name: 'Resume session' }).click();
  await expect(page.getByRole('button', { name: 'Mute mic' })).toBeEnabled({ timeout: 50000 });
  expect(await page.getByLabel('Session details').locator(':scope > code').textContent()).toBe(
    sessionId,
  );
  await page.evaluate(() => window.feedTestAudio('/test-resume.wav'));
  await expect(page.locator('.message.user').last()).toContainText(/what is my name/i, {
    timeout: 25000,
  });
  await expect(page.locator('.message.assistant').last()).toContainText(/alice/i, {
    timeout: 30000,
  });
  await page.getByRole('button', { name: 'End conversation' }).click();
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeEnabled({
    timeout: 25000,
  });
  const final = await (await request.get(`/api/control/sessions/${sessionId}`)).json();
  expect(final.session.status).toBe('completed');
  expect(final.session.attempt_id).not.toBe(attemptId);
  expect(final.attempts).toHaveLength(2);
  const finalHistory = await (
    await request.get(`/api/control/sessions/${sessionId}/history?tail=true&limit=200`)
  ).json();
  const ids = finalHistory.items.map((item: { id: string }) => item.id);
  expect(new Set(ids).size).toBe(ids.length);
  for (const item of archive.items) expect(ids).toContain(item.id);
  expect(errors).toEqual([]);
});

test('real voice: fresh sessions are isolated and abnormal disconnect cannot resume', async ({
  page,
  request,
}) => {
  test.skip(
    !process.env.OPENTALK_LIVE_AUDIO_DIR,
    'Start real services and opt in to paid provider tests.',
  );
  test.setTimeout(90000);
  // Simulate loss of the browser without the best-effort normal-end beacon.
  await page.addInitScript(() => {
    navigator.sendBeacon = () => false;
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start conversation' }).click();
  await expect(page.getByRole('button', { name: 'Mute mic' })).toBeEnabled({ timeout: 50000 });
  const sessionId = (await page
    .getByLabel('Session details')
    .locator(':scope > code')
    .textContent())!;
  await expect(page.getByRole('log')).not.toContainText('Alice');
  await page.goto('about:blank');
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/control/sessions/${sessionId}`)).json()).session.status,
      { timeout: 30000, intervals: [1000] },
    )
    .toBe('failed');
  const rejected = await request.post('/api/control/sessions', {
    data: { request_id: `invalid-resume-${sessionId}`, resume_session_id: sessionId },
  });
  expect(rejected.status()).toBe(409);
  await page.goto('/');
  await page.getByRole('button', { name: new RegExp(sessionId.slice(0, 12)) }).click();
  await expect(page.getByLabel('Session details')).toContainText('cannot be resumed');
  await expect(page.getByRole('button', { name: 'Resume session' })).toHaveCount(0);
});
