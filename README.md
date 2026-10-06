# OpenTalk

OpenTalk is a voice demo project for learning and validating streaming voice agents and reliable backend operations.

## Status

The meeting-room booking backend, configurable streaming LLM provider, and Soniox streaming ASR/TTS providers are implemented. Providers return native LiveKit `LLM`, `STT`, and `TTS` instances. Text tests exercise real DeepSeek tool calls against temporary SQLite databases. ASR can replay a local audio file, and TTS can stream text into a WAV output, without a frontend or LiveKit Server. Browser integration and an application-level AgentSession are not implemented yet. The booking CLI and offline tests require no API credentials.

See the [development plan](docs/开发计划.md) for the agreed architecture, module boundaries, and acceptance criteria. The development plan is written in Chinese.

## Agreed architecture

- Python 3.11 with uv for the backend.
- React/Next.js with pnpm for the browser frontend, based on the LiveKit starter.
- LiveKit Agents and WebRTC for realtime voice sessions.
- Soniox streaming ASR and TTS for the initial remote implementation.
- OpenAI or OpenAI-compatible streaming Chat Completions for the LLM, with a configurable endpoint and model.
- Silero VAD for speech activity detection.
- SQLite for meeting-room bookings, operation state, transcripts, and session events.
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
- Perform confirmed, transactional, idempotent booking operations through a deterministic business service.

## Configuration and secrets

Public backend settings are in `config/backend.toml`: database path, timezone, fixed demo user, rooms, and slot schedule. Relative configured database paths resolve from the project root. `config/llm.toml` configures the LLM endpoint, model, credential variable name, request limits, and provider-specific options. `config/asr.toml` configures the Soniox endpoint, model, language hints, sample rate, endpoint delay, and file replay settings.

The LLM, ASR, and TTS factories load secrets from the root `.env.local` file or process environment variables. Process environment variables take precedence. The Python application explicitly loads the root file, independently of the working directory. `config/tts.toml` configures the synthesis endpoint, model, voice, primary language, sample rate, speed, timeouts, and simulated text input settings.

The remote implementation requires:

- `SONIOX_API_KEY` and valid Soniox model and voice settings.
- `DEEPSEEK_API_KEY` for the current official DeepSeek endpoint, or a configurable credential variable for another compatible provider.
- A configured LLM model that supports streaming and tool calls.
- A reachable LiveKit Server and server-side authentication settings.

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

Live tests are skipped by default even when credentials exist. They use temporary databases and exercise mixed Chinese/English input, a changed proposal, confirmation, booking lookup, cancellation, and abandonment without confirmation. They assert tool calls and persisted business state rather than exact wording. LLM responses remain probabilistic; these runs are representative scenarios, not guarantees for arbitrary input.

Validation on October 7, 2026: 12 offline tests passed; all three live tests passed in separate streaming and tool-scenario runs. See the [validation record](docs/LLM验证记录.md) for observations and scope.

The tests save local reports to `logs/llm-booking-lifecycle.json` and `logs/llm-no-implicit-confirmation.json`. Reports contain role-separated chat items, tool calls and outputs, turn timings, and operation events. Each run atomically replaces its report instead of appending duplicate records. Reports are ignored by Git and do not include provider credentials.

`create_llm()` can be passed directly to a future LiveKit session. `BookingTools.get_tools()` returns native LiveKit function tools. The bounded text driver in `tests/text_harness.py` uses LiveKit's tool validation and execution helpers; it is only test scaffolding and does not create an application Agent, worker, room, or voice pipeline.

Committed writes require host authorization. `authorize_confirmation(operation_id, version)` and `authorize_cancellation(booking_id)` are Python methods, not LLM tools. The live test explicitly grants permission after a scripted user confirmation or cancellation request. This verifies the execution boundary; automatic interpretation of arbitrary confirmations and persistent conversation state are deferred to the application conversation layer. The LLM cannot authorize itself.

## Streaming ASR with a local audio file

Set `SONIOX_API_KEY` in the process environment or root `.env.local`. Then replay your own recording:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay recordings/demo.wav --output logs/asr-demo.json
```

The command uses the real Soniox WebSocket API and incurs provider charges. No frontend, microphone, LiveKit Server, LLM, or TTS is required. WAV, MP3, M4A, and other formats supported by the installed PyAV build are decoded locally, downmixed to mono PCM16, and resampled to the configured rate. An external `ffmpeg` executable is not required. Input is paced at its original duration with 20 ms frames rather than uploaded as a batch transcription job.

The native Soniox plugin uses `stt-rt-v5` with Chinese and English hints. Hints are not strict language restrictions. Translation, language labels, and speaker diarization are disabled. Mixed-language text is preserved; the file input is explicitly assigned the `user` role. Interim and preflight text remain provisional, and finalization updates the same message ID. This ASR test performs no booking operations.

Text events are printed immediately as JSON lines; the last JSON object is the summary. The report contains timestamped events, provisional revisions, final user messages, processing usage, and timings from replay start. Reusing `--output` atomically replaces the report. Omitting it writes a new run under `logs/asr-<session_id>.json`. Reports retain multilingual conversation text but no credentials or authentication payloads.

The locked plugin does not terminate its remote session when `end_input()` is called. The replay utility therefore adds 3 seconds of paced silence, waits for processing usage to cover all sent audio and for provisional text to be finalized, then closes the stream. The tail is also sent to the API. The configurable drain deadline starts after the tail; an unfinalized result or missing processing acknowledgement times out and is recorded as a failure. A recording with no finalized speech also fails. This is a bounded file replay test, not a batch API or a server `finished` acknowledgement test.

Press Ctrl+C to stop replay and close its resources; the report is marked cancelled. This checks stream cancellation, while conversational barge-in requires the future VAD/TTS/session integration. Short recordings may produce only final text; use a longer recording to observe interim updates.

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay --help
uv run --locked pytest tests/test_asr_offline.py -q
```

The 18 ASR offline tests use the real native plugin with a simulated WebSocket. They cover PCM framing, WAV/MP3 decoding, resampling, mixed-language revisions, provisional preflight text, finalization, credential precedence, timeout, API errors, no-speech input, cancellation, report replacement, and resource cleanup. They make no network calls. Real Soniox accuracy and latency have not yet been validated because no credential or user recording was available. See the Chinese [ASR test guide](docs/ASR验证指南.md) for recording scenarios and report interpretation.

## Streaming TTS with text input

TTS uses the same `SONIOX_API_KEY` environment variable or root `.env.local` credential as ASR. Run a paid synthesis test with inline text or a UTF-8 file:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text "会议室 A 明天上午十点可用。Please confirm the date and time before booking." --output recordings/tts-demo.wav --report logs/tts-demo.json
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text-file recordings/tts-input.txt --language en --output recordings/tts-en.wav --report logs/tts-en.json
```

The native Soniox plugin uses the explicitly configured `tts-rt-v2` model, `Maya` voice, and primary language `zh`. Text is submitted incrementally with `push_text()`, buffered into sentences by the official plugin, and synthesized over WebSocket. PCM16 mono audio is consumed and written as it arrives, then published as a playable WAV. No frontend, LiveKit Server, LLM, external ffmpeg, or audio device is required. Open the completed WAV in your own player; this utility does not play audio in realtime.

`language` is the primary delivery language required by Soniox. It does not classify each utterance or remove foreign words. Mixed Chinese/English text is preserved. Override it with `--language en` or `--language zh`; the provider also supports native `update_options(language=...)` for subsequent streams on the same instance.

The CLI prints first-audio, input-ended, and stream-completed events immediately, then a summary. The report records one assistant message with planned and submitted text, text/audio events, native request and segment IDs, elapsed timings, frame count, duration, and `audio_before_input_end`. Longer, multi-sentence input is recommended to observe audio arriving before all text is submitted. Short input may finish submitting before the first frame. No exact `spoken_text` is claimed.

Without explicit paths, outputs use `recordings/tts-<session_id>.wav` and `logs/tts-<session_id>.json`. Successful output and reports replace their destination atomically. API errors, timeouts, and empty audio fail without replacing an existing successful WAV. Ctrl+C or `--cancel-after 2` cancels synthesis; already received audio is saved separately as `<output-stem>.partial.wav`, with a cancelled report and exit code 130. Component cancellation is covered; voice-triggered barge-in and playback queue handling remain for session integration.

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --help
uv run --locked pytest tests/test_tts_offline.py -q
```

All 18 TTS offline tests passed against the native plugin with a simulated WebSocket. The complete offline suite passed with 48 tests and 3 paid LLM tests skipped. Actual Soniox TTS synthesis has not been run in the development environment because the credential was unavailable. The user reported that the earlier manual ASR cases passed. See the Chinese [TTS test guide](docs/TTS验证指南.md) for text input, cancellation, and listening checks.

## Run the booking backend

Run these commands from the project root:

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

Reuse the operation ID and unchanged arguments when retrying. Query `operation OPERATION_ID` after a lost response. Successful operations retain their original result snapshot, even if the booking is subsequently cancelled; use `booking BOOKING_ID` for its current state. Cancellation is an explicit write command and must be invoked deliberately by a future conversation layer.

The service writes business state and operation events in one transaction. Confirmation conflicts and expiry persist a failed operation. This synchronous backend does not persist intermediate executing/unknown states; a caller with an unknown response can recover the committed outcome by operation ID. Slots are fixed, non-overlapping intervals per room. There is no HTTP server, authentication flow, SQLite transcript storage, or full voice session recorder yet; the fixed user and session are demo context, not production authentication. LLM tests and ASR replay currently write local JSON reports.

## Environment inspection

Checked on October 6, 2026:

| Tool | Observed version or status |
| --- | --- |
| uv | 0.9.21 |
| pnpm | 10.13.1; executable resolves under `/mnt/c/nvm4w/nodejs/` |
| Node.js | v24.15.0 |
| Python 3.11 | 3.11.14 at `/usr/bin/python3.11` |
| Default `python3` | 3.10.12; project setup must explicitly select Python 3.11 |
| LiveKit CLI and Server | Not found on the current PATH |
| Project dependencies | pytest, LiveKit Agents/OpenAI/Soniox plugins, PyAV, and dotenv installed with uv on October 7, 2026 |
| Git metadata | `git status` does not recognize the current directory as a repository |

The default uv cache was not writable in the inspection sandbox. Setting `UV_CACHE_DIR=/tmp/opentalk-uv-cache` allowed installed Python discovery to complete. This is an inspection workaround, not a required project default.

The current restricted sandbox can also stall during `asyncio.run()` thread-pool shutdown, including a minimal `asyncio.to_thread()` example. The complete offline suite passed outside that sandbox; no application workaround was introduced. Real API tests require network access to the configured endpoint.

Booking tests run on Python 3.11. The configured DeepSeek credential and official endpoint have passed a real streaming smoke test and two real text-tool scenarios. Native Soniox plugin compatibility and file replay have passed offline tests. Real Soniox connectivity, recognition accuracy, LiveKit Server connectivity, and microphone behavior have not been validated.

## Development conventions

- Manage Python dependencies with uv and retain `uv.lock`.
- Manage frontend dependencies with pnpm and retain `pnpm-lock.yaml`.
- Do not upgrade dependencies by default.
- Use English for code comments, UI labels, diagnostics, and errors. Preserve multilingual conversation content and transcripts.
- Deduplicate messages and events using stable identifiers. Use explicit revisions when retaining multiple versions.
- Keep business logic independent of voice frameworks and model SDKs.
- Treat interruption, multilingual input, logging, and changes of intent as module acceptance criteria rather than deferred features.
