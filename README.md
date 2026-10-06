# OpenTalk

OpenTalk is a voice demo project for learning and validating streaming voice agents and reliable backend operations.

## Status

The meeting-room booking backend is implemented with SQLite, a Python service, a CLI, and smoke tests. Voice, LLM, browser, and external API integration are not implemented yet. No API credentials are required for the booking backend.

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

Public backend settings are in `config/backend.toml`: database path, timezone, fixed demo user, rooms, and slot schedule. Relative configured database paths resolve from the project root. Future voice settings will also be centralized under `config/`.

Secrets will be loaded from the root `.env.local` file or process environment variables. Process environment variables will take precedence. The Python application will explicitly load the root file.

The remote implementation requires:

- `SONIOX_API_KEY` and valid Soniox model and voice settings.
- `OPENAI_API_KEY`, or a configurable credential variable for a compatible provider.
- A configured LLM model that supports streaming and tool calls.
- A reachable LiveKit Server and server-side authentication settings.

Provider credentials and LiveKit API secrets must remain on the backend. The browser will receive only public settings and short-lived connection tokens. Server secrets must not use `NEXT_PUBLIC_*` variables.

Ignore rules already exclude secret files and runtime data. The remote integration will add an environment example with placeholders. Do not commit real `.env.local` files, databases, transcripts, logs, or recordings. Secret loading is not needed or implemented in the current backend.

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

The service writes business state and operation events in one transaction. Confirmation conflicts and expiry persist a failed operation. This synchronous backend does not persist intermediate executing/unknown states; a caller with an unknown response can recover the committed outcome by operation ID. Slots are fixed, non-overlapping intervals per room. There is no HTTP server, authentication flow, transcript storage, or voice session recorder yet; the fixed user and session are demo context, not production authentication.

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
| Project dependencies | pytest development dependencies installed with uv on October 7, 2026; no runtime dependencies |
| Git metadata | `git status` does not recognize the current directory as a repository |

The default uv cache was not writable in the inspection sandbox. Setting `UV_CACHE_DIR=/tmp/opentalk-uv-cache` allowed installed Python discovery to complete. This is an inspection workaround, not a required project default.

Booking smoke tests run on Python 3.11. API credentials, remote account access, voice package compatibility, and microphone behavior have not been validated.

## Development conventions

- Manage Python dependencies with uv and retain `uv.lock`.
- Manage frontend dependencies with pnpm and retain `pnpm-lock.yaml`.
- Do not upgrade dependencies by default.
- Use English for code comments, UI labels, diagnostics, and errors. Preserve multilingual conversation content and transcripts.
- Deduplicate messages and events using stable identifiers. Use explicit revisions when retaining multiple versions.
- Keep business logic independent of voice frameworks and model SDKs.
- Treat interruption, multilingual input, logging, and changes of intent as module acceptance criteria rather than deferred features.
