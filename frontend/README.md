# OpenTalk browser demo

A minimal LiveKit starter adaptation: voice, audio waveform, role-separated transcript, and persisted sessions. See [UPSTREAM.md](UPSTREAM.md) for provenance and [LICENSE](LICENSE) for the upstream MIT license.

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

The service panel polls every 10 seconds. It checks SQLite, an authenticated LiveKit server request, the worker's native HTTP health endpoint and agent name, and provider credential/configuration presence. It does not probe paid provider APIs, media network reachability, or guarantee job dispatch capacity.

```bash
pnpm typecheck
pnpm exec playwright install chromium
pnpm test
```

Default browser tests start a frontend development server on port 3000, use mocked control responses for UI checks, and make a malformed request through the real proxy to verify same-origin handling. They do not call paid providers. Set `OPENTALK_TEST_EXTERNAL_SERVER=1` to use an already-running frontend, and optionally `OPENTALK_TEST_URL` to change its URL. Live voice tests are opt-in; see the guide for audio fixtures and isolated databases.
