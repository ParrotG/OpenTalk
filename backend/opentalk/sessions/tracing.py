"""Optional native OpenTelemetry spans in the bounded diagnostics database."""

import logging
import asyncio
import threading
from contextlib import asynccontextmanager

from livekit.agents.telemetry import set_tracer_provider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import use_span

from opentalk.sessions.telemetry import TelemetryStore


logger = logging.getLogger(__name__)
_providers = {}
_lock = threading.Lock()


class SQLiteSpanExporter(SpanExporter):
    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.lock = threading.Lock()

    def export(self, spans):
        events = []
        with self.lock:
            for span in spans:
                state = self.sessions.get(span.context.trace_id)
                if state is None:
                    continue
                events.append({
                    "event_id": f"span:{span.context.trace_id:032x}:{span.context.span_id:016x}",
                    "session_id": state.session_id, "attempt_id": state.attempt_id,
                    "type": "trace", "timestamp": span.start_time / 1e9,
                    "name": span.name, "trace_id": f"{span.context.trace_id:032x}",
                    "span_id": f"{span.context.span_id:016x}",
                    "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
                    "duration_seconds": (span.end_time - span.start_time) / 1e9,
                    "status": span.status.status_code.name,
                    # LiveKit strips content through allow_pii=False before exporting.
                    "attributes": dict(span.attributes or {}),
                })
        try:
            self.store.write(events)
            return SpanExportResult.SUCCESS
        except Exception:
            logger.error("Trace persistence failed.")
            return SpanExportResult.FAILURE


@asynccontextmanager
async def session_trace(config, state):
    if not config.tracing_enabled:
        yield
        return
    path = str(config.telemetry_database_path.resolve())
    with _lock:
        if path not in _providers:
            store = TelemetryStore(path, max_events=config.telemetry_max_events,
                                   retention_days=config.telemetry_retention_days)
            exporter = SQLiteSpanExporter(store)
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=config.pending_event_limit,
                                                           max_export_batch_size=min(128, config.pending_event_limit)))
            _providers[path] = provider, exporter
        provider, exporter = _providers[path]
        set_tracer_provider(provider, allow_pii=False)
    span = provider.get_tracer("opentalk.sessions").start_span(
        "opentalk.session", attributes={"opentalk.session_id": state.session_id,
                                         "opentalk.attempt_id": state.attempt_id},
    )
    with use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
        trace_id = span.get_span_context().trace_id
        with exporter.lock:
            exporter.sessions[trace_id] = state
        try:
            yield
        finally:
            # Keep root and child spans mapped until the bounded queue is flushed.
            span.end()
            if not await asyncio.to_thread(provider.force_flush, timeout_millis=5000):
                logger.error("Trace flush timed out.")
            with exporter.lock:
                exporter.sessions.pop(trace_id, None)
