# OpenTalk

OpenTalk is a voice demo project for learning and validating streaming voice agents and reliable backend operations.

## Status

The meeting-room booking backend, configurable streaming LLM provider, and Soniox streaming ASR/TTS providers are implemented. Providers return native LiveKit `LLM`, `STT`, and `TTS` instances. Text tests exercise real DeepSeek tool calls against temporary SQLite databases. ASR can replay a local audio file, and TTS can stream text into a WAV output, without a frontend or LiveKit Server. A native LiveKit AgentSession now connects the providers, Silero VAD, and booking tools. Local microphone console and headless text entrypoints are available; a minimal LiveKit starter browser frontend is available with text/voice switching, waveform, transcript, collapsible session/service controls, and system-following dark mode. Independent SQLite sessions, normal-completion resume, text session commands, and a local control/token API are available. The booking CLI and offline tests require no API credentials.

See the [development plan](docs/开发计划.md) for the agreed architecture, module boundaries, and acceptance criteria. The development plan is written in Chinese.

## Agreed architecture

- Python 3.11 with uv for the backend.
- React/Next.js with pnpm for the browser frontend, based on the LiveKit starter.
- LiveKit Agents and WebRTC for realtime voice sessions.
- Soniox streaming ASR and TTS for the initial remote implementation.
- OpenAI or OpenAI-compatible streaming Chat Completions for the LLM, with a configurable endpoint and model.
- Silero VAD for speech activity detection.
- Separate SQLite stores for bookings, resumable session history/userdata, and bounded diagnostics/metrics.
- In-memory active session state, without Redis in the first version.

ASR, LLM, and TTS providers will be independently configurable. A local implementation is planned after the remote baseline; local models have not been selected.

## Required behavior

- Stream ASR results, LLM text, and TTS audio independently.
- Begin audio playback before the complete assistant response has been generated.
- Support interruptions and changes of intent without reviving stale responses or repeating writes.
- Preserve mixed Chinese and English input within an utterance and across turns.
- Maintain only `preferred_response_language` as application-level language state.
- Distinguish user, assistant, system, and tool content by source.
- Record transcripts and session events throughout development.
- Ask for natural-language confirmation before reservation edits; keep edits transactional and retries idempotent.

## Configuration and secrets

Public backend settings are in `config/backend.toml`: database path, timezone (default `Asia/Singapore`), fixed demo user and seed schedule. `config/booking_demo.json` describes users, resources and initial occupancy; resource discovery comes from SQLite rather than a configured room allowlist. `config/booking_prompt.md` contains the booking instructions; `config/booking_agent.toml` contains read-query resource limits. Relative configured database paths resolve from the project root. `config/llm.toml` configures the LLM endpoint, model, credential variable name, request limits, and provider-specific options. `config/asr.toml` configures the Soniox endpoint, model, language hints, sample rate, endpoint delay, and file replay settings.

The LLM, ASR, and TTS factories load secrets from the root `.env.local` file or process environment variables. Process environment variables take precedence. The Python application explicitly loads the root file, independently of the working directory. `config/tts.toml` configures the synthesis endpoint, model, voice, primary language, sample rate, speed, timeouts, and simulated text input settings.

The remote implementation requires:

- `SONIOX_API_KEY` and valid Soniox model and voice settings.
- `DEEPSEEK_API_KEY` for the current official DeepSeek endpoint, or a configurable credential variable for another compatible provider.
- A configured LLM model that supports streaming and tool calls.
- Room worker mode needs a reachable LiveKit Server. Both room and local console modes need `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` with this SDK setup. For `livekit-server --dev`, use `devkey` / `secret` and `LIVEKIT_URL=ws://localhost:7880`. Headless text needs no LiveKit settings.

Provider credentials and LiveKit API secrets must remain on the backend. The browser will receive only public settings and short-lived connection tokens. Server secrets must not use `NEXT_PUBLIC_*` variables.

Ignore rules exclude secret files and runtime data. `.env.example` contains placeholders; copy it to `.env.local` or set the credential in your process environment. Do not commit real `.env.local` files, databases, transcripts, logs, or recordings. Booking-only commands do not load or require model credentials.

## Streaming LLM and text tool tests

The current configuration uses `deepseek-flash` at `https://api.deepseek.com`, with `DEEPSEEK_API_KEY` and thinking disabled. Model identifiers and provider-specific request options live in configuration, not business code. Changing providers requires checking streaming and tool-call support and adjusting `extra_body` as appropriate.

Run the paid streaming smoke test after configuring the credential:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.llm.smoke
```

The smoke test consumes the actual LiveKit `LLM.chat()` stream and reports non-empty text chunk count, time to first content, elapsed time, output, and usage. It requires multiple text chunks and makes no booking writes.

Run offline tests, or explicitly opt into paid network tests:

```bash
uv run --locked pytest -q
uv run --locked pytest tests/test_llm_live.py --live-llm -q
```

Live tests are skipped by default even when credentials exist. Current tests use the native AgentSession and temporary SQLite databases to exercise SQL room/range queries, natural confirmation, booking creation, updates, cancellation, abandonment without edits, and scoped reasoning. They assert tool calls and persisted state instead of exact wording.

## Agent tools and reasoning

`ConversationAgent` in `backend/opentalk/agents/base.py` owns native session hooks, language preference, stale-response checks, logging, and the generic `escalate_reasoning` tool. `BookingAgent` subclasses it and adds exactly two business tools. System instructions require concise spoken language and exclude tables, emoji, Markdown, code blocks, and visual lists from user-facing replies. Runtime context supplies the trusted current UID and business timezone:

- `query(sql)`: one read-only SQLite statement, including SELECT, joins, CTEs, aggregates, schema discovery, and date-range searches. A read-only connection and SQLite authorizer reject writes, ATTACH, PRAGMA, transaction control, and extension loading. Configurable row, execution-step, and time limits bound queries; truncated results are explicitly marked.
- `edit(action, resource, starts_at, ends_at, new_resource?, new_starts_at?, new_ends_at?, uid?)`: add a reservation, cancel a matching reservation, or atomically move it to a replacement interval. It can create an interval that was not seeded. Cancellation retains historical records. Updates preserve the booking ID. Offset-free timestamps use the configured timezone; timestamps are stored in UTC.

The prompt asks the assistant to describe the exact change and obtain agreement in a later user turn before calling edit. Ordinary agreement is accepted; there are no fixed confirmation words, host-generated recaps, proposal-version grants, or separate confirmation tools. Confirmation is an LLM conversation policy, not a deterministic authorization guarantee. All local business data is queryable. There is no login flow; the configured fixed UID determines ownership. The backend independently rejects edits of another user’s bookings, including attempts to supply a different UID.

Edits validate future intervals, known resources, caller ownership, own overlapping bookings and peak concurrent resource capacity. Each reservation consumes one capacity unit, and adjacent intervals do not conflict. A request key binds the user turn and native tool call; a retry returns the original outcome, and changed arguments with the same key are rejected. Business changes and operation outcomes commit together; failed moves preserve the original booking. Already started writes finish when speech is interrupted, and stale calls are rejected before execution.

The default profile disables thinking. `escalate_reasoning(task, message?)` delivers a brief acknowledgement and makes one isolated streaming analysis request with the configurable `[reasoning]` profile in `config/llm.toml`. The current profile uses the same model, thinking enabled, `reasoning_effort = "high"`, an 8192-token limit, and a 60-second timeout. It returns the answer content, not the reasoning trace, and the default agent continues with query/edit. It is limited to one escalation per user turn and never changes the shared/default provider settings.

The stronger request has no tools and receives a self-contained task with relevant facts. This avoids requiring a provider-specific reasoning-history extension to the native tool loop. DeepSeek requires reasoning history to be passed back for thinking requests with tools; see the [official thinking-mode documentation](https://api-docs.deepseek.com/guides/thinking_mode/). Other providers can replace the configured reasoning body or omit the profile to disable escalation.

## Streaming ASR with a local audio file

Set `SONIOX_API_KEY` in the process environment or root `.env.local`. Then replay your own recording:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay recordings/demo.wav --output logs/asr-demo.json
```

The command uses the real Soniox WebSocket API and incurs provider charges. No frontend, microphone, LiveKit Server, LLM, or TTS is required. WAV, MP3, M4A, and other formats supported by the installed PyAV build are decoded locally, downmixed to mono PCM16, and resampled to the configured rate. An external `ffmpeg` executable is not required. Input is paced at its original duration with 20 ms frames rather than uploaded as a batch transcription job.

The native Soniox plugin uses `stt-rt-v5` with Chinese and English hints. Hints are not strict language restrictions. Translation, language labels, and speaker diarization are disabled. Mixed-language text is preserved; the file input is explicitly assigned the `user` role. Interim and preflight text remain provisional, and finalization updates the same message ID. This ASR test performs no booking operations.

Text events are printed immediately as JSON lines; the last JSON object is the summary. The report contains timestamped events, provisional revisions, final user messages, processing usage, and timings from replay start. Reusing `--output` atomically replaces the report. Omitting it writes a new run under `logs/asr-<session_id>.json`. Reports retain multilingual conversation text but no credentials or authentication payloads.

The locked plugin does not terminate its remote session when `end_input()` is called. The replay utility therefore adds 3 seconds of paced silence, waits for processing usage to cover all sent audio and for provisional text to be finalized, then closes the stream. The tail is also sent to the API. The configurable drain deadline starts after the tail; an unfinalized result or missing processing acknowledgement times out and is recorded as a failure. A recording with no finalized speech also fails. This is a bounded file replay test, not a batch API or a server `finished` acknowledgement test.

Press Ctrl+C to stop replay and close its resources; the report is marked cancelled. This checks component cancellation. The voice session separately handles interruptions and playback cancellation. Short recordings may produce only final text; use a longer recording to observe interim updates.

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay --help
uv run --locked pytest tests/test_asr_offline.py -q
```

The 18 ASR offline tests use the real native plugin with a simulated WebSocket. They cover PCM framing, WAV/MP3 decoding, resampling, mixed-language revisions, provisional preflight text, finalization, credential precedence, timeout, API errors, no-speech input, cancellation, report replacement, and resource cleanup. They make no network calls. The user subsequently reported that the manual ASR cases passed; combined live conversation still needs microphone validation. See the Chinese [ASR test guide](docs/ASR验证指南.md) for recording scenarios and report interpretation.

## Streaming TTS with text input

TTS uses the same `SONIOX_API_KEY` environment variable or root `.env.local` credential as ASR. Run a paid synthesis test with inline text or a UTF-8 file:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text "会议室 A 明天上午十点可用。Please confirm the date and time before booking." --output recordings/tts-demo.wav --report logs/tts-demo.json
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text-file recordings/tts-input.txt --language en --output recordings/tts-en.wav --report logs/tts-en.json
```

The native Soniox plugin uses the explicitly configured `tts-rt-v2` model, `Maya` voice, and primary language `zh`. Text is submitted incrementally with `push_text()`, buffered into sentences by the official plugin, and synthesized over WebSocket. PCM16 mono audio is consumed and written as it arrives, then published as a playable WAV. No frontend, LiveKit Server, LLM, external ffmpeg, or audio device is required. Open the completed WAV in your own player; this utility does not play audio in realtime.

`language` is the primary delivery language required by Soniox. It does not classify each utterance or remove foreign words. Mixed Chinese/English text is preserved. Override it with `--language en` or `--language zh`; the provider also supports native `update_options(language=...)` for subsequent streams on the same instance.

The CLI prints first-audio, input-ended, and stream-completed events immediately, then a summary. The report records one assistant message with planned and submitted text, text/audio events, native request and segment IDs, elapsed timings, frame count, duration, and `audio_before_input_end`. Longer, multi-sentence input is recommended to observe audio arriving before all text is submitted. Short input may finish submitting before the first frame. No exact `spoken_text` is claimed.

Without explicit paths, outputs use `recordings/tts-<session_id>.wav` and `logs/tts-<session_id>.json`. Successful output and reports replace their destination atomically. API errors, timeouts, and empty audio fail without replacing an existing successful WAV. Ctrl+C or `--cancel-after 2` cancels synthesis; already received audio is saved separately as `<output-stem>.partial.wav`, with a cancelled report and exit code 130. Component cancellation is covered; session tests also exercise interrupted synthesis and playback queue clearing.

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --help
uv run --locked pytest tests/test_tts_offline.py -q
```

All 18 TTS offline tests passed against the native plugin with a simulated WebSocket. The user subsequently reported that manual ASR and TTS tests passed. The complete conversation still needs live microphone validation; offline session checks are described below. See the Chinese [TTS test guide](docs/TTS验证指南.md) for text input, cancellation, and listening checks.

## Realtime conversation without a frontend

The native `BookingAgent` and `AgentSession` compose the existing streaming providers. Soniox endpointing controls completed user turns; local CPU Silero VAD detects speech and triggers interruptions. Native session handling cancels obsolete LLM/TTS output and clears playback. Preemptive generation and automatic false-interruption resume are disabled for this demo.

Seed a future date before testing, then choose either entrypoint:

```bash
uv sync --locked --python 3.11
PYTHONPATH=backend uv run --locked python -m opentalk admin init --start-date 2030-01-02

# Headless text: requires only the configured LLM credential, on Linux/WSL.
PYTHONPATH=backend uv run --locked python -m opentalk.voice.text

# Local microphone and speaker: requires LLM/Soniox credentials and PortAudio.
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker console --list-devices
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker console
```

The text entrypoint uses the same native agent and tool execution, reads standard input, and exits on `/quit`, EOF, or Ctrl+C. It does not test ASR/TTS or acoustic interruptions. The microphone console is provided by the locked SDK and needs no browser or active room connection. Because this worker configures a server URL, the locked SDK initializes a LiveKitAPI client even in console mode; configure the local development API key/secret from `.env.example` to avoid the missing-credentials error. Its Python CLI is marked deprecated by LiveKit but remains available in the locked version; it does not require installing the separate `lk` CLI.

Ask for all known rooms or available intervals across dates. To reserve, move, or cancel a reservation, describe the request and answer the assistant’s confirmation question naturally. The agent should ask again after changed details and should not interpret a refusal, question, or interrupted explanation as confirmation. Multilingual user input and generated LLM responses are preserved. `请用英语回复` / `please reply in English` and `请用中文回复` / `please reply in Chinese` set the explicit reply preference and subsequent TTS language.

Public VAD settings are in `config/voice.toml`; persistence and lifecycle limits are in `config/sessions.toml`. Native `session.history` and typed `session.userdata` are checkpointed to `data/sessions.sqlite3`, independently of booking storage. Metrics, bounded diagnostics, and optional content-free OpenTelemetry traces are kept in `logs/telemetry.sqlite3`. Interim ASR revisions and full generated responses are no longer duplicated into session JSON snapshots. Text saves after each turn; all modes periodically checkpoint and save on close. Already started transactions retain their result even if speech is cancelled.

For a LiveKit room worker, configure the server URL and backend credentials, then run:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker dev
# Use start instead of dev for worker operation without development reload.
```

The agent registers as `opentalk-booking`; a room client must explicitly dispatch that agent name. The local session API supplies tokens and named dispatch metadata for this worker; a browser and LiveKit Server deployment are still pending.

Validation records and current test coverage are listed in the realtime conversation guide. Offline tests use the real native session and tool runner with scripted streaming provider doubles. They cover multiple SQL queries per turn, edits, language preference, audio input/output, interruption, stale tool calls, transaction completion after speech cancellation, atomic reports, scoped reasoning, and headless standard-input interaction. Silero also performs actual local inference on silence. Acoustic barge-in, provider timing, and microphone quality require manual testing. See the Chinese [realtime conversation guide](docs/实时语音验证指南.md).

## Session management and resume

Use the same text entrypoint, or select the business-free conversation agent:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.voice.text
PYTHONPATH=backend uv run --locked python -m opentalk.voice.text --agent conversation
```

No models are initialized until a conversation starts. `/session`, `/session list`, `/session show ID`, `/session history [ID]`, and `/session help` inspect local state without credentials or API charges. `/session end` finishes normally; `/session new` starts another conversation; `/session resume ID` restores a completed session with the same session ID and a new attempt ID. `/quit` and normal EOF also finish normally. Cancellation, provider/persistence failures, and expired worker leases cannot be resumed. A resumed agent receives bounded native history and language preference; stored tool calls are never replayed.

The separate SQLite session service has no booking dependency. The booking adapter lives in `voice/factories.py`; the conversation adapter uses no booking database. Default context is limited to 120 items, diagnostics to 512 queued / 10000 stored events and 7 days, and each running attempt to one hour. Actual conversation history is retained once per native item ID and grows with actual conversation, independently of diagnostic retention.

Start the local control API for the browser:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.sessions.api
```

It listens on `127.0.0.1:8080` and provides session creation/resume, listing, paginated history, signed room tokens, and idempotent end requests. API and worker share the session database. `LIVEKIT_URL` is the internal server address; `LIVEKIT_PUBLIC_URL` overrides the browser-facing address. Web clients must request normal end and wait for the final checkpoint; unsolicited participant disconnects are conservatively failed.

See the Chinese [session validation guide](docs/会话管理验证指南.md) for CLI acceptance steps, configuration, lifecycle semantics, API payloads, and official references. Existing `logs/voice` JSON artifacts are historical and are not automatically imported.

## Browser demo

The trimmed [LiveKit starter frontend](frontend/README.md) contains voice controls, agent waveform, transcript, and persisted session selection/resume. It has no booking-specific UI. Run these in separate terminals from the project root after configuring root `.env.local`:

```bash
livekit-server --config config/livekit-local.yaml
PYTHONPATH=backend uv run --locked python -m opentalk.sessions.api
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker dev --no-reload --log-level info
```

Then start the frontend:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Open http://localhost:3000 and allow microphone access. Use Node.js 24 and pnpm 10.13.1. The frontend proxies the session API on the server; default `OPENTALK_API_URL` is `http://127.0.0.1:8080`. `OPENTALK_AGENT=conversation` selects the generic talkbot; default `booking` retains booking tools in the agent without adding business UI. Secrets stay in the Python backend.

The local LiveKit configuration advertises only `127.0.0.1` ICE candidates and enables loopback media binding. This is required for Windows browsers accessing WSL through localhost when the automatically selected LAN address is unreachable. It uses development credentials and is intended for browsers and workers on the same computer. Remote browsers need a separate reachable media-address configuration.

The Services panel checks session storage, authenticated LiveKit connectivity, worker HTTP health on port 8081, and provider configuration every 10 seconds. It does not make paid provider calls. Native messages checkpoint automatically when committed. New chat and history selection save and close the current connection internally; page close sends a best-effort normal-close request. Reopening loads the last selected history, and the next text or voice input resumes a completed session without a separate button. Unexpected failures remain read-only. Text mode disables backend audio input/output and skips TTS; canceling voice also stops active synthesis while continuing the original LLM text stream.

See the Chinese [browser validation guide](docs/网页语音验证指南.md) for staged tests, independent test databases, actual browser integration results, configuration overrides, and physical microphone checks. Container startup and isolated tests are documented in [Docker validation](docs/Docker验证指南.md).

## Docker demo

Configure the root `.env.local` with provider credentials, then run from the project root:

```bash
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 worker api
```

Open http://localhost:3000. Docker Desktop users should enable WSL integration for the project distro. The Compose stack includes LiveKit 1.13.8, the independent SQLite session API, the agent worker, and the standalone Next.js frontend. A separate idempotent `booking-init` job initializes the demo data before the booking worker starts. Python 3.11.14, uv 0.9.21, Node.js 24.15.0 and pnpm 10.13.1 retain the existing application lockfiles.

This configuration serves browsers on the same computer, including Windows browsers accessing WSL localhost. Signaling uses TCP 7880; media uses TCP 7881 and UDP 7882. LiveKit advertises both the container address for internal peers and loopback for local host browsers. Its media sockets bind the container network interface, so Docker can forward host traffic correctly. The API is internal, and the frontend alone proxies its requests. Local development LiveKit credentials are set consistently by Compose; provider credentials enter the API/worker at runtime and are excluded from image build contexts.

Named volumes `booking-data`, `session-data` and `telemetry-data` keep separate SQLite stores. They are independent of host `data/` and `logs/`; local databases are not imported automatically. `docker compose down` preserves these volumes. Container logs rotate at 10 MB with three files per service. Frontend and Python application services run as UID 10001. `OPENTALK_AGENT=conversation docker compose up -d` selects the generic talkbot.

```bash
# Inspect the container's business database without calling any provider.
docker compose run --rm --no-deps booking-init python -m opentalk admin inspect
docker compose run --rm --no-deps booking-init python -m opentalk admin check
# Run the complete offline backend suite in a separate test image.
docker compose --profile test run --build --rm --no-deps backend-tests
# Stop the demo while preserving its data.
docker compose down
```

See the Chinese [Docker guide](docs/Docker验证指南.md) for staged verification, port overrides, database administration, persistence and WebRTC tests.

## Booking database administration

The business database now has five tables, independent of LiveKit or agent sessions:

| Table | Purpose |
| --- | --- |
| `users(uid, name, department)` | User directory; the demo always acts as configured `demo_user_id`. |
| `resources(rid, name, type, location, capacity, metadata)` | Resource directory, per-person concurrent capacity and JSON descriptions. |
| `slots(sid, rid, starts_at, ends_at)` | Time intervals stored in canonical UTC. |
| `slot_users(booking_id, sid, uid, operation_id, status, created_at, updated_at)` | User reservations; cancellation retains the row, and moving preserves `booking_id`. |
| `operations(...)` | Durable requests, versions, successful snapshots and failed outcomes. |

The `reservations` view joins user/resource descriptions. `slot_availability` reports peak occupancy and remaining capacity over each interval; it accounts for partial overlaps. Read queries may inspect everyone’s bookings, while edits only affect the configured current user. An individual booking consumes one capacity unit; metadata may describe physical seating separately. Slots are discoverable examples, not an opening-hours restriction, so an edit may create a different future interval on an existing resource.

Run local administrator commands from the project root; they need no model credentials:

```bash
# Initialize a seven-day period starting tomorrow in the business timezone.
PYTHONPATH=backend uv run --locked python -m opentalk admin init
# Or choose an explicit future period and optional JSON fixture.
PYTHONPATH=backend uv run --locked python -m opentalk admin init --start-date 2030-01-02 --days 7
PYTHONPATH=backend uv run --locked python -m opentalk admin inspect
PYTHONPATH=backend uv run --locked python -m opentalk admin inspect --table users
PYTHONPATH=backend uv run --locked python -m opentalk admin inspect --table resources
PYTHONPATH=backend uv run --locked python -m opentalk admin inspect --table reservations --limit 200
PYTHONPATH=backend uv run --locked python -m opentalk admin query "SELECT resource_name, uid, starts_at, ends_at FROM reservations WHERE status='active' ORDER BY starts_at"
PYTHONPATH=backend uv run --locked python -m opentalk admin check
```

`admin check` checks SQLite integrity, foreign keys, capacity and overlapping bookings of the same user/resource. `admin query` is read-only. These are trusted local administrator commands, not an authentication mechanism or agent tools. Add `--database /tmp/opentalk-case.sqlite3` before `admin` to inspect or initialize an isolated database; the text agent’s `--config` can point to a matching copied backend configuration.

On a fresh database, the default profile creates Alice (`demo-user`), Bob, Chen and Dina; Room A (capacity 1), Room B (2), Desk Zone (3) and Projector 1 (1). It initializes the configured 09:00, 10:00, 14:00 and 15:00 intervals each day with partial occupancy, including one reservation of Alice’s. A seven-day fixture contains 112 slots and 49 reservations/operations. Repeating initialization of the same period neither duplicates reservations nor resurrects cancelled ones; existing bookings are not replaced, and conflicting fixture entries are reported in `skipped`. Use a fresh database file for repeatable test baselines.

The service accepts the resource-model schema. The previous booking tools, two-step CLI and schema migration entrypoint have been removed. Already migrated business records and operation outcomes remain readable; session history and telemetry use independent stores.

See [Booking Service natural-language test cases](docs/BookingService自然语言测试用例.md) for normal, complex, ambiguous, unreasonable and multilingual scenarios, setup and state assertions.

## Environment inspection

Checked on October 6, 2026:

| Tool | Observed version or status |
| --- | --- |
| uv | 0.9.21 |
| pnpm | 10.13.1; executable resolves under `/mnt/c/nvm4w/nodejs/` |
| Node.js | v24.15.0 |
| Python 3.11 | 3.11.14 at `/usr/bin/python3.11` |
| Default `python3` | 3.10.12; project setup must explicitly select Python 3.11 |
| Local LiveKit Server | User reports installation and manually starts `livekit-server --dev`; Cloud is not required |
| Project dependencies | pytest, LiveKit Agents/OpenAI/Soniox/Silero plugins, PyAV, and dotenv installed with uv on October 7, 2026 |
| Git metadata | Repository present during the October 7 implementation |
| PortAudio system library | Not found during the October 7 inspection; required for local microphone console |

The default uv cache was not writable in the inspection sandbox. Setting `UV_CACHE_DIR=/tmp/opentalk-uv-cache` allowed installed Python discovery to complete. This is an inspection workaround, not a required project default.

The current restricted sandbox can also stall during `asyncio.run()` thread-pool shutdown, including a minimal `asyncio.to_thread()` example. The complete offline suite passed outside that sandbox; no application workaround was introduced. Real API tests require network access to the configured endpoint.

Booking tests run on Python 3.11. The configured DeepSeek credential and official endpoint have passed a real streaming smoke test and two real text-tool scenarios. Native Soniox plugin compatibility and file replay have passed offline tests. The user reported successful manual Soniox ASR/TTS tests. Local LiveKit connectivity and the browser media pipeline have passed real Soniox / DeepSeek tests using simulated browser microphone input. Physical microphone quality and acoustic echo remain manual checks.

## Development conventions

- Manage Python dependencies with uv and retain `uv.lock`.
- Manage frontend dependencies with pnpm and retain `pnpm-lock.yaml`.
- Do not upgrade dependencies by default.
- Use English for code comments, UI labels, diagnostics, and errors. Preserve multilingual conversation content and transcripts.
- Deduplicate messages and events using stable identifiers. Use explicit revisions when retaining multiple versions.
- Keep business logic independent of voice frameworks and model SDKs.
- Treat interruption, multilingual input, logging, and changes of intent as module acceptance criteria rather than deferred features.
