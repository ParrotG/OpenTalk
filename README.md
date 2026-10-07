# OpenTalk

OpenTalk is a voice demo project for learning and validating streaming voice agents and reliable backend operations.

## Status

The meeting-room booking backend, configurable streaming LLM provider, and Soniox streaming ASR/TTS providers are implemented. Providers return native LiveKit `LLM`, `STT`, and `TTS` instances. Text tests exercise real DeepSeek tool calls against temporary SQLite databases. ASR can replay a local audio file, and TTS can stream text into a WAV output, without a frontend or LiveKit Server. A native LiveKit AgentSession now connects the providers, Silero VAD, and booking tools. Local microphone console and headless text entrypoints are available; browser integration is pending. The booking CLI and offline tests require no API credentials.

See the [development plan](docs/开发计划.md) for the agreed architecture, module boundaries, and acceptance criteria. The development plan is written in Chinese.

## Agreed architecture

- Python 3.11 with uv for the backend.
- React/Next.js with pnpm for the browser frontend, based on the LiveKit starter.
- LiveKit Agents and WebRTC for realtime voice sessions.
- Soniox streaming ASR and TTS for the initial remote implementation.
- OpenAI or OpenAI-compatible streaming Chat Completions for the LLM, with a configurable endpoint and model.
- Silero VAD for speech activity detection.
- SQLite for meeting-room bookings and operation audit; local JSON for transcripts and session events.
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

Public backend settings are in `config/backend.toml`: database path, timezone (default `Asia/Singapore`), fixed demo user, rooms, and seed schedule. `config/booking_prompt.md` contains the booking instructions; `config/booking_agent.toml` contains read-query resource limits. Relative configured database paths resolve from the project root. `config/llm.toml` configures the LLM endpoint, model, credential variable name, request limits, and provider-specific options. `config/asr.toml` configures the Soniox endpoint, model, language hints, sample rate, endpoint delay, and file replay settings.

The LLM, ASR, and TTS factories load secrets from the root `.env.local` file or process environment variables. Process environment variables take precedence. The Python application explicitly loads the root file, independently of the working directory. `config/tts.toml` configures the synthesis endpoint, model, voice, primary language, sample rate, speed, timeouts, and simulated text input settings.

The remote implementation requires:

- `SONIOX_API_KEY` and valid Soniox model and voice settings.
- `DEEPSEEK_API_KEY` for the current official DeepSeek endpoint, or a configurable credential variable for another compatible provider.
- A configured LLM model that supports streaming and tool calls.
- For room worker mode only: a reachable LiveKit Server, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`. Local console and headless text modes require no LiveKit Server.

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

`ConversationAgent` in `backend/opentalk/agents/base.py` owns native session hooks, language preference, stale-response checks, logging, and the generic `escalate_reasoning` tool. `BookingAgent` subclasses it and adds exactly two business tools:

- `query(sql)`: one read-only SQLite statement, including SELECT, joins, CTEs, aggregates, schema discovery, and date-range searches. A read-only connection and SQLite authorizer reject writes, ATTACH, PRAGMA, transaction control, and extension loading. Configurable row, execution-step, and time limits bound queries; truncated results are explicitly marked.
- `edit(action, room, starts_at, ends_at, new_room?, new_starts_at?, new_ends_at?)`: add a reservation, cancel a matching reservation, or atomically move it to a replacement interval. It can create an interval that was not seeded. Cancellation retains historical records. Updates preserve the booking ID. Offset-free timestamps use the configured timezone; timestamps are stored in UTC.

The prompt asks the assistant to describe the exact change and obtain agreement in a later user turn before calling edit. Ordinary agreement is accepted; there are no fixed confirmation words, host-generated recaps, proposal-version grants, or separate confirmation tools. Confirmation is an LLM conversation policy, not a deterministic authorization guarantee. All local demo data is queryable; there is no authentication or per-user restriction in these tools.

Edits validate intervals and reject overlapping active reservations. A host-generated request key combines session, user turn, and normalized edit arguments, so repeated identical calls within a turn return the committed result. Writes and audit events commit together. Already started writes finish and are recorded when speech is interrupted; stale calls are rejected before execution. SQLite schema and legacy booking CLI remain compatible, but the old seven-tool interface is not exposed to the current agent.

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
PYTHONPATH=backend uv run --locked python -m opentalk seed --date 2030-01-02

# Headless text: requires only the configured LLM credential, on Linux/WSL.
PYTHONPATH=backend uv run --locked python -m opentalk.voice.text

# Local microphone and speaker: requires LLM/Soniox credentials and PortAudio.
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker console --list-devices
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker console
```

The text entrypoint uses the same native agent and tool execution, reads standard input, and exits on `/quit`, EOF, or Ctrl+C. It does not test ASR/TTS or acoustic interruptions. The microphone console is provided by the locked SDK and needs no browser or LiveKit Server. Its Python CLI is marked deprecated by LiveKit but remains available in the locked version; it does not require installing the separate `lk` CLI.

Ask for all known rooms or available intervals across dates. To reserve, move, or cancel a reservation, describe the request and answer the assistant’s confirmation question naturally. The agent should ask again after changed details and should not interpret a refusal, question, or interrupted explanation as confirmation. Multilingual user input and generated LLM responses are preserved. `请用英语回复` / `please reply in English` and `请用中文回复` / `please reply in Chinese` set the explicit reply preference and subsequent TTS language.

Public session/VAD settings are in `config/voice.toml`; backend and provider settings remain independent. Reports under `logs/voice/<session_id>.json` upsert messages by native message ID and record tool results separately. Room/console mode saves snapshots every configured interval (one second by default); text mode saves after each turn. Both save on close. Snapshots replace files atomically, and generated text is not claimed to be exact spoken text. Already started transactions retain their result even if speech is cancelled.

For a LiveKit room worker, configure the server URL and backend credentials, then run:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker dev
# Use start instead of dev for worker operation without development reload.
```

The agent registers as `opentalk-booking`; a room client must explicitly dispatch that agent name. This entrypoint is ready for a later browser client, but this change does not add a browser, token service, or LiveKit Server.

Validation records and current test coverage are listed in the realtime conversation guide. Offline tests use the real native session and tool runner with scripted streaming provider doubles. They cover multiple SQL queries per turn, edits, language preference, audio input/output, interruption, stale tool calls, transaction completion after speech cancellation, atomic reports, scoped reasoning, and headless standard-input interaction. Silero also performs actual local inference on silence. Acoustic barge-in, provider timing, and microphone quality require manual testing. See the Chinese [realtime conversation guide](docs/实时语音验证指南.md).

## Legacy booking CLI

This CLI retains the original prepare/confirm workflow for existing backend tests and manual inspection; it is not the current agent tool interface. Run these commands from the project root:

```bash
uv sync --locked --python 3.11
uv run --locked pytest -q
PYTHONPATH=backend uv run --locked python -m opentalk --database /tmp/opentalk-demo.sqlite3 smoke
```

The smoke command creates a future slot in a dedicated room, prepares and confirms a booking, retries confirmation, and cancels the booking. It performs real writes and retains the operation audit. Repeated smoke runs create distinct completed operations and reuse the slot.

To exercise individual steps, replace the date with a future date:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk seed --date 2030-01-02
PYTHONPATH=backend uv run --locked python -m opentalk available --date 2030-01-02
PYTHONPATH=backend uv run --locked python -m opentalk prepare SLOT_ID --operation-id proposal-1
PYTHONPATH=backend uv run --locked python -m opentalk confirm proposal-1 --version 1
PYTHONPATH=backend uv run --locked python -m opentalk booking BOOKING_ID
PYTHONPATH=backend uv run --locked python -m opentalk cancel BOOKING_ID --operation-id cancel-1
PYTHONPATH=backend uv run --locked python -m opentalk operation proposal-1
PYTHONPATH=backend uv run --locked python -m opentalk events
```

Replace `SLOT_ID` and `BOOKING_ID` with IDs returned by earlier commands. CLI output is JSON; business errors have a stable `error` code and an English message. Global `--config`, `--database`, and `--session` options precede the command.

Preparing a proposal does not reserve a slot. To change an unconfirmed proposal, prepare another slot with a new operation ID and `--supersedes proposal-1`; the returned version must be used for confirmation. `invalidate OPERATION_ID` discards a pending proposal.

Reuse the operation ID and unchanged arguments when retrying. Query `operation OPERATION_ID` after a lost response. Successful operations retain their original result snapshot, even if the booking is subsequently cancelled; use `booking BOOKING_ID` for its current state. Cancellation is an explicit CLI write command. The current conversation agent instead uses edit after natural-language confirmation.

The service writes business state and operation events in one transaction. Confirmation conflicts and expiry persist a failed operation. This synchronous backend does not persist intermediate executing/unknown states; a caller with an unknown response can recover the committed outcome by operation ID. Slots are fixed, non-overlapping intervals per room. There is no HTTP server, authentication flow, or SQLite transcript storage; the fixed user and session are demo context, not production authentication. The voice session writes role-separated transcripts, response status, provider usage, and tool/business events to local JSON reports.

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

Booking tests run on Python 3.11. The configured DeepSeek credential and official endpoint have passed a real streaming smoke test and two real text-tool scenarios. Native Soniox plugin compatibility and file replay have passed offline tests. The user reported successful manual Soniox ASR/TTS tests. LiveKit Server connectivity and microphone conversation have not been validated in this environment.

## Development conventions

- Manage Python dependencies with uv and retain `uv.lock`.
- Manage frontend dependencies with pnpm and retain `pnpm-lock.yaml`.
- Do not upgrade dependencies by default.
- Use English for code comments, UI labels, diagnostics, and errors. Preserve multilingual conversation content and transcripts.
- Deduplicate messages and events using stable identifiers. Use explicit revisions when retaining multiple versions.
- Keep business logic independent of voice frameworks and model SDKs.
- Treat interruption, multilingual input, logging, and changes of intent as module acceptance criteria rather than deferred features.
