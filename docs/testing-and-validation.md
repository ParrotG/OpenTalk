# Testing and Validation Specification

## 1. How to use this document

This file is the single source for validation tools, automated cases, integration scenarios, manual business acceptance, and run preparation. Tables use **module → test/tool/scenario → expected result**. Stable IDs allow requirements and backlog entries to refer to them.

**Incomplete** means that a necessary case, implementation, or valid acceptance record is missing. Keep the intended operation and result visible. Existing test code does not prove it passed in the target environment; hand off actual runs according to [Documentation Guidelines](documentation-guidelines.md). Classify unit, component, and integration tests by real boundaries rather than treating every offline test as a unit test.

Python tests live in `tests/`; browser tests live in `frontend/tests/`. Run Python commands from the repository root and pnpm commands from `frontend/`. Default checks use temporary databases and do not call paid services. Real models, recordings, and deployment acceptance require explicit environment, cost, and data authorization. Never write to a user's existing database merely to make tests pass.

## 2. Project and static checks

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| Python/project | S-01: `uv run --locked ruff check backend tests frontend/tests/fixtures`; E4/E9/F/I. | No syntax, unused import/variable, or import-order errors. E402 exceptions for fixtures have a stated reason. This is neither type checking nor a repository-wide formatter. |
| Frontend/project | S-02: `pnpm lint`; ESLint 9 + eslint-config-next 15.5.18 over source and TS tests, excluding generated output. | No errors; React/Hook/TypeScript rules apply; fixture `use` callbacks are not misidentified as React Hooks. |
| Frontend/Hooks | S-03 **incomplete**: resolve four exhaustive-deps warnings in `live-conversation` and `talk-demo`; see G-13. | Do not weaken source rules. Dependencies and closure semantics are explicit; native sessions do not reconnect, end, or send twice. |
| Frontend/types/format | S-04: `pnpm typecheck`, `pnpm format:check`; aggregate `pnpm check`. | TypeScript and Prettier pass. ESLint errors fail the aggregate; existing warnings remain visible until S-03. |
| Dependencies/change scope | S-05: `uv lock --check --offline`, frozen pnpm install, `git diff --check`. | Lockfiles match declarations; no unrelated runtime upgrades, secrets, or whitespace errors; do not remove a user's unlocked extras. |
| Frontend/build | S-06: `pnpm build`. | Next.js standalone build succeeds; build success does not validate media or providers. |
| Deployment/configuration | S-07: `docker compose config --quiet` plus verification overlay. | Dependencies, mounts, ports, and isolated volumes are valid. Report missing Docker as not run. |
| Project/CI/types | S-08 **incomplete**: place static and no-cost regressions in CI; define Python cross-module type checking. | Gates reproduce from a clean checkout and failures block delivery; an agent summary is not execution evidence. |
| Documentation | S-09: verify local links, stable IDs, code/test locations, implemented PDF/RAG scope, and change scope. | No broken links or duplicate/missing IDs; planned work is not described as implemented. Documentation-only work need not run paid tests. |

## 3. Configuration and model modules

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| Configuration/credentials | U-01: configuration cases in `test_llm_offline`, `test_asr_offline`, `test_tts_offline`, and invalid voice configuration in `test_voice_session`. | Environment wins over files; invalid configuration fails explicitly; credentials never enter reports/errors. |
| LLM | I-03: `test_llm_offline`; native plugin receives fragmented tool arguments, invokes test `echo`, and resumes. | One invocation after complete arguments, matching call/result IDs and correct history roles; repeated persistence does not duplicate exchanges. |
| LLM/reasoning | U-05 and scoped-reasoning integration in `test_voice_session`, including progress-notice language and language constraints across tools. | Main model generates a relevant progress notice in the current response language; application validates and speaks it unchanged. Strong analysis has no tools; both models retain the turn language; later simple requests use the default model. |
| LLM/reasoning | U-07 **incomplete**: directly cover repeated escalation in one turn, missing strong model, empty analysis, and invalidation during escalation. | Stable `already_escalated`, `reasoning_unavailable`, `empty_analysis`, and `stale_response`; no second analysis. |
| ASR/audio | U-02: decode and empty-recording cases in `test_asr_offline`; compressed/stereo input, resampling, tail fragments. | Correct mono PCM rate/sample count; empty input opens no paid connection. |
| ASR/transcript | U-03: preflight cases in `test_asr_offline`; interim revisions, repeated text, final output. | Interim text revises the same ID; final messages do not duplicate; preflight is not a final turn. |
| ASR/stream | I-04A: native, failure, cancellation in `test_asr_offline`; simulated WebSocket with EOF/timeout/cancel. | Finalization covers transmitted audio; errors do not report success; connections/tasks close; reports replace atomically. |
| TTS/stream | I-04T: native, failure, cancellation, language switching in `test_tts_offline`, plus bounded long-reply requests in `test_room_control`. | Audio may arrive before all input; text/voice/language semantics hold; long replies cross provider requests without losing text or audio; failure preserves successful WAV; cancellation may write partial output; connection closes. |
| LLM/real service | I-11L **incomplete**: explicit live pytest or `llm.smoke` with valid credentials and isolated data. | Real streaming/escalation succeeds with auditable usage/timing. It costs money; absence of run evidence is not a pass. |
| ASR/real service | I-11A **incomplete**: replay recorded Chinese/English/mixed corrections. | Facts/language survive and EOF finalizes; accuracy/latency have input/output evidence; tail silence may be billed. |
| TTS/real service | I-11T **incomplete**: synthesize long, mixed-language, and cancellation samples; human listening. | Verify audio-before-input-end, pronunciation, partial output, and terminal status. Receiving frames is not playback. |

## 4. Agent and realtime session modules

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| Agent/language | U-08: language-preference case in `test_voice_session`; mixed text after a standalone preference. | One foreign term does not overwrite the explicit preference; no business-specific `detected_language`. |
| VAD | U-09: Silero model load and silent inference. | Inference events occur without false speech-start events; this does not prove human barge-in quality. |
| Agent/interruption | I-05: interruption, obsolete-tool, and started-tool cases. | Obsolete audio stops permanently; stale calls are rejected; started guarded work finishes and records; a new turn responds. |
| RoomAudioMode | I-06: `test_room_control`; text, voice, cancel during reply, re-enable. | Text replies make no TTS call; cancel keeps the same LLM text stream and does not repeat tools; enabling mid-text does not force replay. |
| RoomAudioMode/RPC | U-10: unrelated caller, non-boolean input, and not-ready cases. | Unauthorized/invalid requests fail explicitly; after successful RPC, RoomIO can receive first text. |
| Session/I/O | I-12: audio transcription and text entrypoint cases in `test_voice_session`. | Correct user message, assistant text/audio, actual saved CLI history, and resource cleanup. |
| Text degradation/mode | I-13 **incomplete**: missing Soniox, text sent during voice, UI vs microphone/TTS state; see G-11. | Define rejection or degradation before implementing it; no false availability or lost first message; UI matches actual mode. |
| Tool/provider failure | I-14 **incomplete**: hung tool, cancellation, provider failure; see G-10. | Approved timeout/retry/cost policy; no unlimited shield, duplicate side effect, or unexplained failure. |

## 5. Session storage, API, and telemetry

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| SessionStore | U-04: `test_session_store`; same-key reserve, concurrent claim, expired owner, invalid checkpoint. | One session/owner; failed transactions roll back; history/final state commit atomically in one DB; old attempts cannot write. |
| TelemetryStore | U-06: telemetry cases with same event ID and row/time retention. | Upsert and retention bounds hold; telemetry never enters history archives. |
| Control API | I-01: `test_session_api`; create, retry, token, paging, end, resume. | Room/dispatch/attempt agree; no room-admin token; old attempt cannot end a new one; incomplete final save is not `completed`. |
| API idempotency | I-01M **incomplete**: same request ID with changed `voice_enabled`, retry active attempt; see G-04. | Same semantics replay; changed semantics conflict or use an approved stable metadata strategy. |
| API/frontend proxy | I-02: API tests and proxy cases in `demo.spec.ts`. | Allowlist/same-origin boundaries work; errors are understandable and do not expose sensitive exceptions. |
| API/error boundaries | I-02E **incomplete**: null/list/object/extreme fields, corrupt or unwritable DB; see G-05. | No unexplained 500 or invalid session row; status/error compatibility is explicit. |
| Journal/resume | I-07: `test_session_resume`, `test_session_store`; shutdown, cross-process resume, cancellation, DB/provider-close failure. | Same session/new attempt with history/preference and no duplicate/tool replay; failure is not resumable; no claim of cross-DB atomicity. |
| Tracing | U-11: optional native traces. | Native span relationships, usage, and duration persist without copying conversation/prompt text. |
| Journal/diagnostic failure | I-15 **incomplete**: unwritable telemetry, full queue, sensitive custom span; see G-09. | Approved archive/diagnostic policy, observable redaction/event loss, no claim of zero PII. |
| Schema/history governance | I-16 **incomplete**: database-copy migration, rollback, retention, policy deletion; see G-07. | Compatibility/rollback proven; no silent loss; stable IDs and ownership retained. |

## 6. Frontend and deployment

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| Browser UI | I-08: `demo.spec.ts`; mocked API/history, permission denial, responsive layout, theme, transcript revisions. | No accidental session or ending another attempt; plain text renders safely; revisions replace rather than stack. Does not prove media. |
| Browser/LiveKit | I-09 **incomplete**: `OPENTALK_TEST_RTC=1 pnpm test`; real media/RoomIO with test providers. | Text, microphone, RPC, normal close, new-attempt resume, final SQLite history, and audio tracks are verifiable in target environment. |
| Browser/real models | I-11V **incomplete**: `live.spec.ts`, controlled audio, real providers. | Media/history evidence for recognition, playback, acoustic interruption, and resumed memory. Synthetic WAV cannot replace human listening. |
| Docker/real entrypoint | I-10 **incomplete**: verification Compose plus `docker.spec.ts`, isolated project/ports/volumes, test provider. | Production worker dispatch/media/resume/history without default-volume writes or model cost. |
| Docker/persistence | I-10R **incomplete**: record completed summary, down/up containers, recheck; verify API/worker share mount UID/GID. | Volumes unchanged; completed sessions resume; both create WAL/checkpoints. Abrupt attempts are not promised resumable. |
| Backup/restore | I-10B **incomplete**: SQLite backup or full copy with writers stopped into a new isolated volume. | IDs/summaries match and completed sessions resume. Never copy only the main file of an active WAL database. |
| Public service | I-17 **incomplete**: cross-user access/end/resume, rate limits, TLS, secrets, media, rollback; see G-12. | Unauthorized access denied with quota/security evidence. Local development configuration is not public production. |
| Performance/soak | I-18 **incomplete**: owner defines environment, load, cost, and latency thresholds before measurement; see NFR-05. | Separate turn end, first text, first audio, and playback with monotonic clocks; no cross-host subtraction or sustained leak. |

## 7. Manual business acceptance

Use isolated data. A human records browser/network, recording source, microphone, and listening conditions. Automation supplies objective evidence; semantic quality, human acoustics, and experience require human sign-off. “Incomplete” means this specification lacks valid evidence, not necessarily that the feature is absent.

| Module | Scenario | Expected result |
| --- | --- | --- |
| Text conversation | B-01 **incomplete**: deny microphone and ask for a two-sentence explanation of realtime voice assistants. | Progressive text, no audio/TTS request, useful answer. |
| ASR/TTS/transcript | B-02 **incomplete**: speak Chinese, English, and mixed passages with numbers/corrections; listen. | Final transcript preserves facts/language/roles; revisions do not duplicate; numbers and foreign words are intelligible. |
| Streaming/interruption | B-03 **incomplete**: during a long answer say “stop and answer briefly.” | Streaming is observable; old audio stops permanently; new answer follows intent; record timing without inventing thresholds. |
| Mode switching | B-04 **incomplete**: cancel voice during a long answer, then enable voice next turn. | Audio stops, text continues, no duplicate generation/tool; next voice turn works. |
| Save/resume | B-05 **incomplete**: provide a project name, close/switch normally, reopen and ask; then New chat. | History view starts no microphone/model; input resumes same session/new attempt; new chat inherits no history. |
| Failure recovery | B-06 **incomplete**: compare normal close, network loss, forced worker stop. | Only backend-completed sessions resume; failed sessions show saved history and explainable state; beacon delivery is not guaranteed. |
| Language/strong analysis | B-07 **incomplete**: request each language, use mixed input, escalate once, then ask something simple. | Preference survives turns/resume; escalation returns a conclusion and model-generated same-language notice; normal request uses default profile. |
| Availability/deployment | B-08 **incomplete**: configured but unreachable provider, media failure, restart, restored backup. | “Configured” is not provider/media success; evidence explains failure and preserved data is inspectable. |

## 8. Run preparation and commands

### 8.1 Environment and isolation

Backend: Python 3.11/uv. Frontend: Node.js 24/pnpm 10.13.1. Use strict sync only in a clean dedicated environment; preserve extras in an existing user environment:

```bash
uv sync --locked --python 3.11 --inexact
cd frontend
pnpm install --frozen-lockfile
cd ..
```

Credentials come from process environment or root `.env.local`, with environment winning. Never print secrets or expose them as `NEXT_PUBLIC_*`. `DEEPSEEK_API_KEY` supplies the current LLM and `SONIOX_API_KEY` supplies ASR/TTS. Real sessions use isolated database paths in a dedicated `sessions.toml` shared through `OPENTALK_SESSION_CONFIG`; recordings/reports also use isolated paths. **New chat** does not clear databases.

```bash
uv run --locked ruff check backend tests frontend/tests/fixtures
uv run --locked pytest -q
cd frontend
pnpm check
pnpm test
cd ..
```

Default pytest/browser suites do not call paid models. UI tests start port 3000 without reusing an instance; an existing test service can use `OPENTALK_TEST_EXTERNAL_SERVER=1` and `OPENTALK_TEST_URL`. First Chromium install may require network. RTC fixtures need `livekit-server`, the locked Python environment, and free ports 18180–18183 and 18280–18283.

### 8.2 Full local stack

Run the first three from the root in separate terminals. LiveKit URLs, key, and secret must match the server; local defaults are `devkey` / `secret`.

```bash
# Terminal 1: media transport
livekit-server --config config/livekit-local.yaml
# Terminal 2: session control API
PYTHONPATH=backend uv run --locked python -m opentalk.sessions.api
# Terminal 3: agent worker
PYTHONPATH=backend uv run --locked python -m opentalk.voice.worker dev --no-reload --log-level info
# Terminal 4: browser frontend
cd frontend
pnpm dev
```

Open http://localhost:3000. The text CLI is `PYTHONPATH=backend uv run --locked python -m opentalk.voice.text`; administrative commands make no model call, while normal input needs the LLM. Console voice needs PortAudio/local devices and LiveKit credentials; browser/Docker do not. API uses 8080 and worker health 8081. Backend code changes require a process restart.

### 8.3 Explicitly paid components and browser

Prepare isolated recordings, output paths, configuration, and valid credentials:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.llm.smoke
PYTHONPATH=backend uv run --locked python -m opentalk.asr.replay /tmp/opentalk-audio/input.wav --output /tmp/opentalk-audio/asr.json
PYTHONPATH=backend uv run --locked python -m opentalk.tts.smoke --text "Hello, welcome to OpenTalk." --output /tmp/opentalk-audio/tts.wav --report /tmp/opentalk-audio/tts.json
```

ASR uses PyAV, 20 ms realtime chunks, 3 s tail silence, and bounded waiting; this is not the same as receiving a finished acknowledgement. TTS buffers incremental text at sentence boundaries, writes WAV rather than playing it, and supports `--cancel-after`. First-result timing includes connection/replay or segmentation, not pure model latency.

Real browser input uses `input.wav`, `interrupt.wav`, and `resume.wav` for memory + long-answer request, interruption + short-answer request, and recall. Run from `frontend`:

```bash
OPENTALK_TEST_EXTERNAL_SERVER=1 OPENTALK_LIVE_AUDIO_DIR=/tmp/opentalk-browser-audio pnpm test
```

### 8.4 Isolated Docker validation

Normal GPU-enabled startup is `docker compose up -d --build --wait`; health does not prove media,
real providers, or CUDA execution. CPU-only startup uses
`docker compose -f compose.yaml -f compose.cpu.yaml up -d --build --wait`. Offline container tests
mount no user data:

```bash
docker compose -p opentalk-offline-tests --profile test run --build --rm --no-deps backend-tests
```

Set an isolated project and ports in one terminal:

```bash
export COMPOSE_PROJECT_NAME=opentalk-verify
export COMPOSE_FILE=compose.yaml:compose.verify.yaml
export OPENTALK_FRONTEND_PORT=19400
export OPENTALK_LIVEKIT_PORT=19480
export OPENTALK_RTC_TCP_PORT=19481
export OPENTALK_RTC_UDP_PORT=19482
docker compose config --quiet
docker compose up -d --build --wait
cd frontend
OPENTALK_TEST_EXTERNAL_SERVER=1 OPENTALK_TEST_URL=http://127.0.0.1:19400 OPENTALK_TEST_DOCKER=1 pnpm exec playwright test tests/docker.spec.ts
cd ..
```

The overlay replaces providers only, retaining the production worker entrypoint and real LiveKit/SQLite. Use `down` without `-v` to preserve volumes. Rebuild/backup follows I-10R/I-10B. WSL needs Docker integration; public environments additionally require identity, TLS, reachable media IP, and possibly TURN.

### 8.5 Environment limitations

If uv/Playwright caches are unwritable, choose a writable cache and use it consistently for install/run. Verify actual executables when Node/pnpm resolves through Windows paths. Diagnose ports, browsers, permissions, and stalled asyncio thread pools in a normal terminal instead of bypassing them with application exceptions. When API health passes but audio fails, inspect ICE, TCP 7881 / UDP 7882, and playback permission. A same-host LiveKit configuration does not support a remote device automatically.

## 9. PDF parsing and RAG

| Module | Test / tool / scenario | Expected result |
| --- | --- | --- |
| PDF/configuration | P-01: parser configuration/input cases; relative roots, limits, non-PDF, empty, oversized, accelerator devices, positive thread/batch counts, and queue capacity. | Resolve paths from project root; reject invalid accelerator/concurrency settings; fail invalid input before Docling and create no document directory. |
| PDF/artifacts | P-02: deterministic Docling document with headings, picture, table, formula. | Source stored by hash; independent version; consistent manifest, JSON, Markdown, JSONL, crops, HTML/CSV references. |
| PDF/failure | P-03: injected conversion failure, Docling partial success, and success with a source/processed page-count mismatch. | Command fails and writes a diagnostic failed manifest; partial document artifacts are never published as completed. |
| PDF/large document | P-03L **manual rerun pending**: configuration boundary plus 600-1,000 page text/image-heavy PDF; monitor host RSS/swap and cgroup `oom_kill`. | Accept at most 1,000 pages and 500 MiB; allow up to two hours for Docling; completed manifest reports equal positive source and processed page counts. A killed child becomes a failed stage manifest with an explicit memory or signal error rather than remaining `parsing`. |
| PDF/real parsing | P-04 **manual pending**: normal text, scan, complex table, formula, picture, bilingual PDFs. | Inspect order, hierarchy, pages/coordinates, OCR, table structure, formula text, picture descriptions/crops; record version/time/warnings. File existence is not quality. |
| PDF/heading hierarchy | P-04H **manual pending**: numbered, unnumbered, bookmarked, and printed-TOC PDFs. | Compare heading-level distributions and parent paths with the rendered PDF; record false nesting and false headings. Parsed-page style inference is enabled, but incorrect structure is not accepted by configuration alone. |
| PDF/CUDA throughput | P-04C **manual pending**: parse the same representative image-heavy PDF once with `device = "cpu"` and once with `device = "cuda"`; monitor `nvidia-smi` and record elapsed time, peak GPU memory, pages, warnings, and artifact hashes/counts. | CUDA run shows a Docling/Python process using the GPU, especially during picture description; both manifests report the selected device and identical source/processed page counts. Concurrency improves or preserves throughput without missing/reordered artifacts. Tune batches downward on out-of-memory failure. |
| PDF/Docker | P-04D **incomplete**: one-shot parser with read-only input and mounted host output. | Only global output changes; session/telemetry volumes unchanged; model cache separate. |
| Preparation/structure | I-01: fixed inventory with nested headings, long text, picture/table/formula, false heading inside picture. | Document/section/context/leaf hierarchy, bounded chunks, complete path/parent/neighbor/page/coordinate/element references; picture heading does not change section. |
| Preparation/fallback | I-02: headingless body and undescribed picture. | Synthetic section, retrievable body, asset-only picture without invented embedding text. |
| Preparation/input | I-03: failed parse, duplicate profile, invalid limits. | Reject incomplete parse, never overwrite profile, fail invalid configuration. |
| Preparation/picture fields | I-03A: description/semantic fields plus model source, all classification candidates, and control tokens. | Caption/explicit semantics included; source/candidates/confidence/kind/control tokens excluded. |
| Preparation/furniture | I-03B: explicit furniture/header/footer descendants plus repeated body text. | Explicit furniture excluded; repeated body retained; no positional/repetition inference. |
| Preparation/real artifacts | I-04: run preparation/inspection on a P-04-approved parse. | `valid=true`, no structure/missing-asset error; manually sample sections, overlap, parent bounds, non-text description, pages. |
| Preparation/catalog | I-05: nested section fixture with direct/subtree content, cross-page nodes, empty sections, and assets. | `structure.json` has valid stable references, compact section/page maps, distinct heading/direct/subtree ranges, and no copied body text. |
| Embedding/contract | E-01: fake passage/query provider with wrong count/dimension/non-finite values and oversized input. | Correct methods; invalid vectors fail before write; oversized text fails visibly. |
| Vector/idempotency | E-02: deterministic provider + in-memory Qdrant, index same JSONL twice. | Indexable nodes only; stable upsert count; complete parent/neighbor/page/provenance/sparse/assets. |
| Vector/schema | E-03: reuse collection with different dimension/dense-only schema/profile/model. | Reject incompatible named dense/lexical schema and non-empty embedding-space mismatch before write. |
| Embedding/real model | E-04 **manual pending**: local Qdrant and mixed-language/table/formula/described-picture artifacts. | Model may download once; manifest dimension/count exact; no truncation/non-indexable points. |
| Retrieval/prewarm | R-00: warmup/idempotence, concurrent wait, worker background startup, shared service. | Process readiness does not wait; no Qdrant access; one initialization; query waits for it; agents do not close shared service; elapsed time logged. |
| Dense/manual quality | E-05 **manual pending**: factual, paraphrase, cross-language, table, formula, picture, and irrelevant queries. | Relevant leaves enter candidates; payload locates parent/asset; record top-k/scores/misses/false positives. |
| Lexical/hybrid | R-01: multilingual terms, identifiers, named vectors, Qdrant RRF. | Stable encoding/idempotence; exact and semantic candidates both participate. |
| Retrieval/hierarchy | R-02: valid parent, neighbor fallback, image, invalid limits. | Parent first; neighbors only without parent; complete pages/section/provenance/safe image; visible bounds errors/truncation. |
| Retrieval/query variant | R-03: original, duplicate, and distinct alternate. | Original always retained; blank/duplicate removed; distinct alternate adds dense/lexical candidates. |
| Agent/document tool | R-04: discovery, argument forwarding, result logging, close, unavailable service. | All three document tools are exposed; blocking work stays off the event loop; results preserve locators; failures and ownership are stable. |
| File access | R-04A: outline pagination, section direct/subtree cursors, page spans, large-node offsets, safe images, and missing artifacts. | Deterministic document order, enforced total bounds, stable continuation, page metadata rather than page-text matching, and no path escape. |
| Exact/fuzzy text | R-04B: raw/normalized substring, Chinese/English text, title, identifier, OCR typo, short query, overlap duplicate, section/page filters, and excess matches. | Exact precedes fuzzy; `auto` falls back only without exact hits; cutoff, deduplication, per-section cap, result/character limits, and stable ordering hold. |
| Retrieval/real quality | R-05 **manual pending**: fixed factual/paraphrase/cross-language/ID/table/formula/picture/irrelevant set. | Compare dense-only/hybrid top-k and expansion; record misses/false positives; verify requested image reaches multimodal model. |
| Agent/end-to-end | R-06 **manual pending**: text CLI with fact, follow-up, chart, and out-of-document questions. | Agent calls tool, cites page-locatable evidence, does not invent. Casual chat does not initialize retrieval; image support depends on LLM. |
| PDF/management API | P-05: streaming upload, signature/size, unwritable root, recovery after failure, global list/manifests. | Only global temp files; stable JSON errors; one rejection does not block next upload; queued/stage/failed/ready are distinct; no real model. |
| Deletion/backend | P-05D: session/document/vector deletion cases. | Active resources conflict; terminal session removes related rows/telemetry; document removes vectors before files; missing is 404. |
| PDF/frontend | P-06: document drawer/upload/retry-after-failure/delete/close in `demo.spec.ts`. | Existing responsive style; queued then polling; failure restores upload and later success clears error; active delete disabled; file never enters session request. |

Run automated P-01..P-03/P-05, I-01..I-03B, E-01..E-03, and R-01..R-04:

```bash
uv sync --locked --inexact --group rag
uv run --locked pytest -q \
  tests/test_document_parser.py \
  tests/test_document_indexing.py \
  tests/test_document_access.py \
  tests/test_document_vector_index.py \
  tests/test_document_retrieval.py \
  tests/test_document_library.py \
  tests/test_agent_retrieval.py
```

Run P-06 from `frontend`:

```bash
pnpm exec playwright test tests/demo.spec.ts --grep "document drawer"
```

For E-04/E-05, use manually reviewed artifacts and fixed Qdrant:

```bash
docker compose --profile rag up -d --wait qdrant
INDEX_PATH=data/documents/<document-id>/parses/<parse-version>/indexing/hierarchical-v3
PYTHONPATH=backend uv run --locked --group rag python -m opentalk.documents.index_vectors "$INDEX_PATH"
PYTHONPATH=backend uv run --locked --group rag python -m opentalk.documents.search_vectors \
  "<evaluation-query>" --document-id <document-id> --parse-version <parse-version> \
  --index-profile hierarchical-v3 --limit 10
```

If indexing reports token overflow, retain the failed manifest and create a new preparation profile with smaller chunks. Never raise the configured limit past the real model limit. Record query, expected nodes/pages, actual top-k/scores, misses, and false positives. The low-level dense command does not accept hybrid retrieval, parent expansion, or agent-answer quality.

R-05 uses the hybrid collection. Keep the old `opentalk_documents_multilingual_mpnet_v1`; rerun `index_vectors` to idempotently populate `opentalk_documents_multilingual_mpnet_hybrid_v1`, then:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.documents.retrieve \
  "<standalone-query>" --alternate-query "<optional-alternate-query>" \
  --document-id <document-id> --parse-version <parse-version> \
  --index-profile hierarchical-v3 --limit 6
```

Inspect `queries`, `result_count`, and each evidence object's `matched_text`, `context_text`, `expansion`, `page_numbers`, `section_path`, `provenance`, `asset_references`, and `local_image_paths`. A valid parent yields `expansion=parent`; only missing hierarchy yields `neighbors`. Compare at least the same query against dense-only top-k and record both useful and false additions.

Inspect structure and deterministic text location without Qdrant:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.documents.query inspect \
  <document-id> --view outline
PYTHONPATH=backend uv run --locked python -m opentalk.documents.query inspect \
  <document-id> --view section --section-id <section-id> --section-scope subtree
PYTHONPATH=backend uv run --locked python -m opentalk.documents.query inspect \
  <document-id> --view page --page-start 12 --page-end 13
PYTHONPATH=backend uv run --locked python -m opentalk.documents.query find \
  <document-id> "exact phrase or OCR variant" --match-mode auto
```

For outline output, verify parent/child IDs, levels, heading pages, and direct/subtree page ranges. For section/page output, follow `next_cursor` until `truncated=false` and verify that the combined text stays in document order without exceeding each call's budget. For text lookup, record `requested_match_mode`, `applied_match_mode`, match class/score, section/page locator, false positives, deduplication, and whether an exact hit incorrectly triggered fuzzy results.

For R-06 keep Qdrant running and the hybrid collection indexed, then:

```bash
PYTHONPATH=backend uv run --locked python -m opentalk.voice.text --agent conversation
```

Ask an in-document fact, a paraphrased follow-up, an exact identifier, a misspelled/OCR-like phrase, an outline/section request, an explicit page/chart request, a table/formula question, a picture question, and an out-of-document question. Session diagnostics should show the intended `search_documents`, `find_document_text`, or `inspect_document` route. Answers should mention relevant pages and remain bounded by evidence. Images enter temporary multimodal context only with `include_images=true` and a capable provider; session history must not contain image base64.
