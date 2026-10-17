# Requirements Specification

## 1. Objective and scope

**Current state:** OpenTalk is a browser-based realtime voice and text assistant built around a generic `ConversationAgent`. Users can hold continuous conversations, switch input modes, inspect history, and resume normally completed sessions. There is no separate business-domain module.

The current operating scope is a trusted, single-user environment on one machine. Services can run locally or in Docker. Public hosting, multi-tenant isolation, cross-device synchronization, and high availability are not promised. Engineering rules support maintainable, testable, transferable work; they do not make the system production-ready for public deployment.

## 2. User and functional requirements

Users provide input, grant microphone access, and judge whether answers meet their intent. Maintainers configure, operate, back up, and validate the system. An AI coding assistant implements authorized work and evidence but does not decide business semantics for the requirement owner.

| ID | Function and expected result | Current boundary |
| --- | --- | --- |
| FR-01 | Browser text input receives a progressively rendered response without requesting microphone access or starting TTS for that response. | The browser still connects through LiveKit; the Python text CLI prints after the user turn completes. |
| FR-02 | Voice mode publishes microphone audio, displays recognized text, and plays the answer; recognition, generation, and synthesis use streaming interfaces. | Requires browser permission, reachable media networking, and valid provider credentials. Very short utterances may not visibly overlap stages. |
| FR-03 | Speaking during playback interrupts obsolete generation and audio; the next response handles the new input. | Uses native VAD interruption. Stopping playback cannot undo tool work that has already started. |
| FR-04 | One session supports text/voice switching. **Cancel voice** stops audio while the existing text generation continues and tools are not rerun. | Disabling voice differs from speech interruption. Enabling voice during a text answer does not force that answer to be read from the beginning. |
| FR-05 | User and assistant transcripts are distinct; revisions replace the same recognition segment instead of duplicating it. | The UI may merge adjacent user bubbles; persistence keeps native messages. System and tool details remain hidden. |
| FR-06 | Messages are saved after submission. **New chat** and history selection first end the current session. Normally completed sessions can resume across processes. | Same logical session ID with a new attempt and room. Failed sessions cannot resume. Page-close requests are best effort. |
| FR-07 | Resume restores bounded history and reply-language preference without replaying old tools, audio, or running tasks. | The model does not receive the full archive. Only the `conversation` agent is registered. |
| FR-08 | Chinese, English, and mixed content retain the original text; explicit preferences affect later model output and TTS. | Persistent preference currently recognizes four standalone Chinese/English commands, not arbitrary natural-language preference statements. |
| FR-09 | A difficult turn may run one isolated stronger analysis, speak a progress notice, then continue with the default model using the returned conclusion. | At most once per user turn; analysis has no tools, database access, or external information access. |
| FR-10 | Users can inspect service state, sessions, and history; CLI administration requires no model call. | Status checks configuration and basic services, not paid-provider validity or media reachability. |
| FR-11 | Model, endpoint, voice, and lifecycle settings are centralized and support local/container execution. | The compatible LLM endpoint is configurable. ASR/TTS factories currently target Soniox; another vendor requires an adapter. |
| FR-12 | The frontend can delete completed or failed sessions with their history and telemetry. | Pending/active sessions must end first. The trusted local UI asks for confirmation and provides no undo or recycle bin. |

## 3. Quality requirements

These are delivery requirements. Unimplemented parts are listed in [Improvement Backlog](improvement-backlog.md).

| ID | Required property | Evidence |
| --- | --- | --- |
| NFR-01 Consistency | Retries must not create semantically duplicate messages, events, or requests with the same ID. History and final `completed` state commit in one session-database transaction. | Idempotency, race, and transaction-failure tests; see G-04 for API audio-mode retry behavior. |
| NFR-02 Resource bounds | Bound model context, telemetry queue/retention, and session duration; close streams and owned background tasks. | Configuration, cancellation, and cleanup tests. User history has no automatic retention limit yet. |
| NFR-03 Privacy and authorization | Credentials stay server-side; tokens are room-scoped; RPC is limited to the associated participant; diagnostics do not copy sensitive content by default. | Build/configuration and interface tests. Archives contain conversation and tool text; public authentication is not implemented. |
| NFR-04 Explainable failure | Never report failure, skip, timeout, or mere configuration presence as success. Persist failures; cleanup failure must not produce a normally resumable state. | Disconnect, disk, provider, and shutdown-error tests. |
| NFR-05 Measurable performance | Distinguish end of user turn, first text, first audio, and actual playback. Use monotonic clocks for latency and do not subtract timestamps across machines. | Defined measurement method and representative scenarios. No approved p95, concurrency, or availability SLO exists. |
| NFR-06 Maintainability | Preserve layers/interfaces, locked dependencies, and scoped changes; code and documentation must be transferable. | Static checks, contract tests, and documentation review. Additional engineering gates remain pending. |
| NFR-07 Reproducible deployment | Pin the runtime, preserve volumes, isolate test data, and define rollback-capable deployment steps. | Locked install, build/rebuild, and backup/restore acceptance. Only local deployment is currently specified. |

The owner must define performance targets, target user count, history retention/deletion policy, and public-deployment scope before they become measurable requirements.

## 4. Out of scope

Login, multi-tenant isolation, telephone access, distributed state, high availability, automatic provider fallback, long-term semantic memory, and new business tools are out of scope. The PDF module includes parsing, index preparation, dense/lexical embedding, Qdrant persistence, hybrid retrieval, an agent search tool, and local document management. Document retry and public access control remain undefined.

## 5. PDF parsing and RAG query module

### 5.1 Business objective and usage

A maintainer can supply a PDF through the CLI or local frontend, parse it automatically with Docling, inspect stage status and structured artifacts, store retrievable nodes in global Qdrant, and query them through a standalone command or the conversation agent. Each processing stage remains independently runnable and testable.

### 5.2 Functional and data requirements

| ID | Function and expected result | Boundary |
| --- | --- | --- |
| PDF-01 | The CLI accepts one PDF and performs OCR, layout/reading-order analysis, table structure, picture description/classification, formula enrichment, and code enrichment. | Local PDFs only. Docling models may download on first use. Remote enrichment services are disabled. |
| PDF-02 | Original PDFs are stored by content hash in the global document directory, independent of sessions, attempts, history, and telemetry. | Identical bytes share one source file; the submitted name is manifest metadata only. |
| PDF-03 | Every parse creates an independent version with a manifest, Docling JSON, Markdown, element inventory, and non-text assets. | A version represents one processing result; one source may have multiple versions. |
| PDF-04 | Pictures are saved as referenced files and crops; tables retain structure, HTML, CSV, and crops; formulas retain recognized text/LaTeX and crops. | Derived descriptions do not replace the original element, structure, or provenance. |
| PDF-05 | The element inventory preserves Docling self references, parent/child links, hierarchy, pages, coordinates, text, metadata, and annotations. | Supports human inspection and index preparation. |
| PDF-06 | A left-side frontend panel uploads one PDF, lists global documents, and shows parse, hierarchical-index, vectorization, and failure status. | Documents are session-independent. Ready content is reused by hash. The API owns a bounded background subprocess. |
| PDF-07 | The frontend can delete completed or failed global documents, all file artifacts, and Qdrant points for that document ID. | Queued/processing documents reject deletion. Deletion is confirmed and irreversible. Qdrant failure preserves files for retry. |
| IDX-01 | A completed parse version produces an independent preparation profile containing hierarchical nodes that do not depend on an embedding model or database. | Profiles are immutable; experiments use a new profile name. |
| IDX-02 | Nodes represent document, section, bounded parent context, precise text chunks, and non-text elements. Retrievable leaves retain previous/next links. | Retrieval expands a precise hit to its parent or falls back to neighbors. |
| IDX-03 | Picture captions and explicit description, summary, keyword, topic, and entity fields enter retrieval text. Model provenance, classification candidates, confidence, annotation type, and generation control tokens do not. | Undescribed non-text elements retain asset nodes without invented semantic text. |
| IDX-04 | A structural inspection command reports parent/child and neighbor errors, chunk sizes, missing assets, node counts, page coverage, and content samples. | Structural/content-preparation inspection does not replace embedding/query evaluation. |
| IDX-05 | Preparation excludes elements and descendants explicitly labeled by Docling as furniture, page headers, or page footers. | Do not infer exclusion from margins, repetition, image hashes, or classification; false removal is worse than redundancy. |
| IDX-06 | PDF parsing recovers heading levels from Docling bookmarks, numbering, and visual style; preparation emits a compact section tree and page-to-content map. | Inference can preserve incorrect source heading labels. Parse and preparation versions remain immutable and inspectable. |
| EMB-01 | Generate local dense embeddings for nodes with `indexable=true` and non-empty retrieval text. | Default is a multilingual FastEmbed model. Enforce token limits explicitly; never silently truncate. |
| EMB-02 | Separate the embedding provider from vector storage and record provider, model, profile, dimension, and content hash. | A model change uses a new embedding profile and collection so incompatible spaces never mix. |
| VDB-01 | Upsert vectors idempotently into global Qdrant using stable point IDs. | Vectors never enter the session database; rerunning one profile does not add duplicate points. |
| VDB-02 | Point payloads retain precise leaves, parent snapshots, neighbors, sections, pages, provenance, and asset references. | Retrieval can expand context and resolve image references for multimodal input. |
| VDB-03 | Provide a low-level dense query command with document/parse/index-profile filters. | This evaluates embedding and storage only, not hybrid retrieval or final ranking. |
| RET-01 | Store a dense vector and deterministic Unicode lexical sparse vector for every retrievable node; fuse semantic and lexical candidates with Qdrant RRF. | Lexical encoding uses Chinese characters/bigrams and Unicode alphanumeric tokens elsewhere. It is not tied to English stemming or unevaluated fixed weights. |
| RET-02 | The agent transforms a question into a standalone query and may add one synonym/exact-term variant; each variant runs dense and lexical retrieval before joint fusion. | The variant adds candidates and never replaces the original query. No separate query-rewrite model call is used. |
| RET-03 | After a precise leaf match, return its parent context; when missing/empty, return the hit with adjacent retrievable leaves. | Return matched text, expansion type, section, pages, provenance, and assets. Mark configured context truncation explicitly. |
| RET-04 | The conversation agent exposes global document search. Text, table, and formula evidence enters tool context; images are injected as LiveKit `ImageContent` only when needed. | Read images only from global parse directories. Never persist base64 image content in session history. Descriptions suffice when the model need not inspect pixels. |
| RET-05 | The agent can list one outline level and read a selected section subtree or explicit page range in document order. | Calls require a document ID, use stored page metadata, enforce server-side page/item/character limits, and return a continuation cursor instead of an unbounded chapter. |
| RET-06 | The agent can locate known text, section titles, identifiers, and small spelling/OCR errors through normalized substring and optional fuzzy matching. | Exact matches rank before approximate matches. Fuzzy lookup complements rather than replaces semantic hybrid retrieval, and repeated overlap/section results are bounded. |

### 5.3 Quality, safety, and acceptance

- Public configuration bounds file size, page count, processing time, and image resolution.
- Before creating a document directory, validate a regular, non-empty, size-compliant file with a PDF signature. Corrupt, encrypted, or model failures terminate with explicit Docling errors.
- Both successful and failed parses write manifests. A parse missing core outputs cannot report success.
- Parse, preparation, and vectorization manifests are global files mounted as directories in Docker. Vectors/payloads live in global Qdrant. Neither belongs in the session database.
- This stage deliberately avoids speculative cross-store transactions, migration systems, or complex consistency machinery. Add them only when module/integration evidence establishes a need.
