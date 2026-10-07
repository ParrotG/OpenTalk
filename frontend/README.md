# OpenTalk browser demo

A minimal LiveKit starter adaptation: text/voice chat, audio waveform, a minimal transcript, and persisted sessions. The page follows the system color scheme. See [UPSTREAM.md](UPSTREAM.md) for provenance and [LICENSE](LICENSE) for the upstream MIT license.

Use Node.js 24 and pnpm 10.13.1:

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Start LiveKit, the Python session API, and the Python room worker first, as described in [the browser validation guide](../docs/网页语音验证指南.md). Open http://localhost:3000. `pnpm build` and `pnpm start` provide a production build.

Optional server-only settings in `frontend/.env.local`:

```dotenv
OPENTALK_API_URL=http://127.0.0.1:8080
OPENTALK_AGENT=booking
```

The browser receives only short-lived room tokens. LiveKit API secrets and model credentials belong in the repository-root `.env.local`, never in this directory or `NEXT_PUBLIC_*` variables. The proxy reads the backend address on the server and disables response caching. Resuming uses the persisted session's agent key; the UI has no booking controls.

Recent sessions and Services are collapsible header panels and adapt to narrow screens. Type a message and press Enter to send (Shift+Enter adds a line), or use the waveform icon to start voice mode. Cancel voice closes the microphone and stops backend TTS while keeping the same session connected in text mode. Text mode streams LLM output directly, without audio synthesis or playback pacing. Audio mode uses the native `opentalk.set_voice_mode` RPC, restricted to the linked participant, alongside the native `lk.chat` stream for text input. Canceling an active voice reply preserves the original LLM generation and tool executions.

Native messages checkpoint when committed, with a short coalescing delay rather than a database write for every token. New chat and history selection automatically save and close the current connection; page close sends a best-effort normal-close request. The browser remembers only the selected session ID, attempt ID and close intent. Reopening loads that history without enabling the microphone; the next text or voice input resumes a completed session automatically. Failed sessions remain read-only. There are no Resume session or End conversation buttons. Consecutive user fragments share a display bubble until assistant text arrives; revisions overwrite the same `lk.segment_id`, while native stored messages remain separate. Icons use [Lucide React](https://lucide.dev/guide/react), pinned to the original starter resolution.

The service panel polls every 10 seconds. It checks SQLite, an authenticated LiveKit server request, the worker's native HTTP health endpoint and agent name, and provider credential/configuration presence. It does not probe paid provider APIs, media network reachability, or guarantee job dispatch capacity.

```bash
pnpm typecheck
pnpm exec playwright install chromium
pnpm test
```

Default browser tests start a frontend development server on port 3000, use mocked control responses for UI checks, and make a malformed request through the real proxy to verify same-origin handling. They do not call paid providers. Set `OPENTALK_TEST_EXTERNAL_SERVER=1` to use an already-running frontend, and optionally `OPENTALK_TEST_URL` to change its URL. Live voice tests are opt-in; see the guide for audio fixtures and isolated databases.

For actual SDK/WebRTC checks without model calls or existing database writes:

```bash
OPENTALK_TEST_RTC=1 pnpm test
```

This requires the repository's locked Python environment (`uv sync --locked` at the repository root) and the `livekit-server` binary on PATH. The protocol fixture uses ports 18180–18183 and verifies native text delivery, interim transcript revisions, microphone enable/mute, text/voice RPC switching and automatic reopen. A second fixture uses ports 18280–18283 with the real session API, native AgentSession/RoomIO, temporary SQLite databases and deterministic providers. It verifies committed-history persistence, normal page-close completion, automatic same-ID/new-attempt resume, and zero TTS requests for text replies. All eight ports must be free. Fixtures clean up their own services without calling paid models or writing existing databases; provider response quality remains an opt-in check.
