"""Record native session events without duplicating ASR and conversation messages."""

import asyncio
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


class SessionJournal:
    def __init__(self, session_id: str, path: Path):
        self.path = path
        self.started = time.perf_counter()
        self._save_lock = asyncio.Lock()
        self.report = {"session_id": session_id, "started_at": datetime.now(UTC).isoformat(),
                       "events": [], "messages": {}, "responses": {}, "operation_events": []}

    def record(self, kind: str, **fields) -> None:
        self.report["events"].append({
            "event_id": f"{self.report['session_id']}:{len(self.report['events']) + 1}",
            "type": kind, "received_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": time.perf_counter() - self.started, **fields,
        })

    def attach(self, session) -> None:
        def message_added(ev):
            item = ev.item
            if item.type != "message":
                return
            self.report["messages"][item.id] = {
                "message_id": item.id, "role": item.role, "text": item.text_content,
                "interrupted": item.interrupted, "created_at": item.created_at,
                "metrics": dict(item.metrics),
            }
            self.record("conversation_item_added", message_id=item.id, role=item.role)

        def transcript(ev):
            # Final user messages come only from conversation_item_added.
            self.record("asr_transcript", text=ev.transcript, is_final=ev.is_final, item_id=ev.item_id)

        def speech_created(ev):
            handle = ev.speech_handle
            self.record("response_started", response_id=handle.id, source=ev.source)

            def finished(speech):
                self.report["responses"].setdefault(speech.id, {})["status"] = (
                    "interrupted" if speech.interrupted else "failed" if speech.exception() else "completed"
                )
                self.record("response_finished", response_id=speech.id, interrupted=speech.interrupted,
                            error_type=type(speech.exception()).__name__ if speech.exception() else None)
            handle.add_done_callback(finished)

        def tools_executed(ev):
            for call, output in ev.zipped():
                self.record("tool_result", call_id=call.call_id, name=call.name,
                            arguments=call.arguments, output=output.output, is_error=output.is_error)

        session.on("conversation_item_added", message_added)
        session.on("user_input_transcribed", transcript)
        session.on("speech_created", speech_created)
        session.on("function_tools_executed", tools_executed)
        session.on("user_state_changed", lambda ev: self.record("user_state", old=ev.old_state, new=ev.new_state))
        session.on("agent_state_changed", lambda ev: self.record("agent_state", old=ev.old_state, new=ev.new_state))
        session.on("error", lambda ev: self.record(
            "error", error_type=type(getattr(ev.error, "error", ev.error)).__name__,
            source=getattr(ev.source, "provider", type(ev.source).__name__),
            recoverable=getattr(ev.error, "recoverable", False),
        ))
        session.on("session_usage_updated", lambda ev: self.record(
            "usage", data={"model_usage": [item.model_dump(mode="json") for item in ev.usage.model_usage]},
        ))
        session.on("close", lambda ev: self.record("session_closed", reason=str(ev.reason)))

    async def save(self, service) -> None:
        async with self._save_lock:
            events = await asyncio.to_thread(service.list_events)
            self.report["operation_events"] = events
            self.report["saved_at"] = datetime.now(UTC).isoformat()
            # Serialize on the event loop so callbacks cannot mutate a snapshot in the writer.
            payload = json.dumps(self.report, ensure_ascii=False, indent=2)
            task = asyncio.create_task(asyncio.to_thread(self._write, payload))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish an older snapshot before a shutdown snapshot may replace it.
                await asyncio.shield(task)
                raise

    def _write(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(payload)
                file.write("\n")
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
