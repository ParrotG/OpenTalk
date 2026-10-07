"""Checkpoint native history and userdata without duplicating conversation content."""

import asyncio
import time
from collections import deque


class SessionJournal:
    def __init__(self, store, telemetry, state, owner, *, pending_event_limit=512):
        self.store, self.telemetry = store, telemetry
        self.state, self.owner = state, owner
        self.session = None
        self.started = time.perf_counter()
        self.failed = False
        self.error_code = None
        self._save_lock = asyncio.Lock()
        self._pending = deque(maxlen=pending_event_limit)
        self._sequence = 0
        self.dropped_events = 0
        self._checkpoint_task = None
        self._checkpoint_requested = False
        self._accept_checkpoints = True
        # This bounded view is for diagnostics and tests, not conversation persistence.
        self.report = {"events": deque(maxlen=pending_event_limit)}

    def record(self, kind, **fields):
        self._sequence += 1
        entry = {"event_id": f"{self.state.attempt_id}:{self._sequence}",
                 "session_id": self.state.session_id, "attempt_id": self.state.attempt_id,
                 "type": kind, "timestamp": time.time(),
                 "elapsed_seconds": time.perf_counter() - self.started, **fields}
        if len(self._pending) == self._pending.maxlen:
            self.dropped_events += 1
        self._pending.append(entry)
        self.report["events"].append(entry)

    def attach(self, session):
        self.session = session

        def closed(ev):
            if ev.error is not None or ev.reason.value in {"error", "job_shutdown", "participant_disconnected"}:
                self.failed = True
                self.error_code = ev.reason.value
            self.record("session_closed", reason=ev.reason.value)

        def error(ev):
            recoverable = getattr(ev.error, "recoverable", False)
            if not recoverable:
                self.failed = True
                self.error_code = "provider_error"
            self.record("error", error_type=type(getattr(ev.error, "error", ev.error)).__name__,
                        recoverable=recoverable)

        def speech_created(ev):
            handle = ev.speech_handle
            self.record("response_started", response_id=handle.id)

            def finished(speech):
                if speech.exception():
                    self.failed = True
                    self.error_code = "response_failed"
                self.record("response_finished", response_id=speech.id, interrupted=speech.interrupted,
                            error_type=type(speech.exception()).__name__ if speech.exception() else None)
            handle.add_done_callback(finished)

        def tools_executed(ev):
            for call, output in ev.zipped():
                # Arguments and outputs already exist once in native history.
                self.record("tool_result", call_id=call.call_id, name=call.name, is_error=output.is_error)

        session.on("speech_created", speech_created)
        session.on("function_tools_executed", tools_executed)
        session.on("error", error)
        session.on("close", closed)
        session.on("conversation_item_added", lambda event: self.request_checkpoint())
        for provider in (session.llm, session.stt, session.tts):
            if provider is not None:
                provider.on("metrics_collected", lambda metrics: self.record(
                    "model_metrics", data=metrics.model_dump(mode="json")))

    def request_checkpoint(self):
        if not self._accept_checkpoints:
            return
        self._checkpoint_requested = True
        if self._checkpoint_task is None or self._checkpoint_task.done():
            self._checkpoint_task = asyncio.create_task(self._checkpoint())

    async def _checkpoint(self):
        try:
            while self._checkpoint_requested:
                await asyncio.sleep(0.05)
                self._checkpoint_requested = False
                await self.save()
        except Exception as error:
            self.failed, self.error_code = True, "persistence_failed"
            self.record("checkpoint_error", error_type=type(error).__name__)
            await self.session.aclose()

    async def flush(self):
        self._accept_checkpoints = False
        if self._checkpoint_task is not None:
            await self._checkpoint_task

    async def save(self, *, status=None, error_code=None):
        async with self._save_lock:
            history = self.session.history.to_dict(exclude_timestamp=False, exclude_metrics=True,
                                                   exclude_config_update=True)
            history["items"] = [item for item in history["items"] if item["type"] != "agent_handoff"]
            events = list(self._pending)
            pending_ids = {event["event_id"] for event in events}
            for item in self.session.history.items:
                if item.type == "message" and item.metrics:
                    events.append({"event_id": f"{self.state.attempt_id}:metrics:{item.id}",
                                   "session_id": self.state.session_id, "attempt_id": self.state.attempt_id,
                                   "type": "turn_metrics", "timestamp": item.created_at,
                                   "message_id": item.id, "data": dict(item.metrics)})
            usage = self.session.usage
            events.append({"event_id": f"{self.state.attempt_id}:usage", "session_id": self.state.session_id,
                           "attempt_id": self.state.attempt_id, "type": "usage", "timestamp": time.time(),
                           "data": [item.model_dump(mode="json") for item in usage.model_usage],
                           "dropped_events": self.dropped_events})

            def write():
                self.telemetry.write(events)
                self.store.checkpoint(self.state.session_id, self.state.attempt_id, self.owner,
                                      history, self.state.to_dict(), status=status, error_code=error_code)

            task = asyncio.create_task(asyncio.to_thread(write))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
                raise
            while self._pending and self._pending[0]["event_id"] in pending_ids:
                self._pending.popleft()
