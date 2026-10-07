"""Exercise native context restoration and text session commands without API calls."""

import asyncio
import json
import os
import sqlite3
from dataclasses import replace

import pytest
from livekit.agents import llm

from opentalk.sessions import config as session_settings
from opentalk.sessions.store import SessionError, SessionStore
from opentalk.voice.session import open_session
from voice_fakes import ScriptedLLM


def test_native_history_userdata_resume_and_no_tool_replay(tmp_path):
    config = session_settings.load_session_config()
    model = ScriptedLLM()
    async def scenario():
        async with open_session(text_only=True, agent_key="conversation", model=model) as (session, agent, journal):
            await session.start(agent=agent)
            assert session.userdata is agent.state
            await session.run(user_input="请用英语回复")
            await session.run(user_input="Remember that my project is OpenTalk.")
            await journal.save()
            session_id = session.userdata.session_id
            history_before = journal.store.history(session_id)["items"]
        assert journal.store.get(session_id)["status"] == "completed"
        seen = []
        restored_model = ScriptedLLM()
        original = restored_model.chat
        def chat(**kwargs):
            seen.extend(kwargs["chat_ctx"].items)
            return original(**kwargs)
        restored_model.chat = chat
        async with open_session(text_only=True, agent_key="conversation", resume_id=session_id,
                                model=restored_model) as (session, agent, journal):
            await session.start(agent=agent)
            assert session.userdata.preferred_response_language == "en"
            assert any(item.type == "message" and "OpenTalk" in (item.text_content or "") for item in agent.chat_ctx.items)
            await session.run(user_input="What is my project?")
        assert any(item.type == "message" and "OpenTalk" in (item.text_content or "") for item in seen)
        history_after = journal.store.history(session_id)["items"]
        assert len(history_after) == len(history_before) + 2
        assert len({item["id"] for item in history_after}) == len(history_after)
        assert all("metrics" not in item for item in history_after)
        assert len(restored_model.requests) == 1
    asyncio.run(scenario())


def test_exception_marks_failed_and_refuses_restore(tmp_path):
    async def scenario():
        with pytest.raises(RuntimeError, match="failure"):
            async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
                await session.start(agent=agent)
                await session.run(user_input="Hello")
                raise RuntimeError("failure")
        session_id = session.userdata.session_id
        assert journal.store.get(session_id)["status"] == "failed"
        assert journal.store.history(session_id)["items"]
        with pytest.raises(SessionError, match="normally completed"):
            async with open_session(text_only=True, agent_key="conversation", resume_id=session_id, model=ScriptedLLM()):
                pass
    asyncio.run(scenario())


def test_cancellation_marks_failed(tmp_path):
    async def scenario():
        ready = asyncio.Event()
        captured = []
        async def run():
            async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
                await session.start(agent=agent)
                captured.append((session, journal))
                ready.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session, journal = captured[0]
        assert journal.store.get(session.userdata.session_id)["status"] == "failed"
    asyncio.run(scenario())


def test_bounded_events_and_interim_transcripts_do_not_duplicate_history(tmp_path):
    async def scenario():
        config = replace(session_settings.load_session_config(), pending_event_limit=4)
        async with open_session(text_only=True, agent_key="conversation", session_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            for _ in range(1000):
                journal.record("state_changed")
            assert len(journal.report["events"]) == len(journal._pending) == 4
            assert journal.dropped_events >= 996
            await journal.save()
            assert len(journal._pending) == 0
            assert journal.store.history(session.userdata.session_id)["items"] == []
            assert len(journal.telemetry.read(session.userdata.session_id)) <= 5
    asyncio.run(scenario())


def test_text_commands_end_resume_and_history(tmp_path, monkeypatch, capsys):
    from opentalk.voice import text
    config = session_settings.load_session_config()
    store = SessionStore(config.database_path)
    # Reserve the identity beforehand so the pipe can refer to it deterministically.
    reservation = store.reserve(request_id="cli", agent_key="conversation")
    original = open_session
    openings = []
    def injected(**kwargs):
        kwargs["model"] = ScriptedLLM()
        if not openings:
            kwargs["reservation"] = reservation
        openings.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(text, "open_session", injected)
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer:
        writer.write(f"/session\n/session list\nHello\n/session end\n/session resume {reservation['session_id']}\n"
                     f"/session history\nHello again\n/session end\n/session show {reservation['session_id']}\n/quit\n")
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(text.sys, "stdin", reader)
        asyncio.run(text.converse(session_config=config, store=store, agent_key="conversation"))
    output = capsys.readouterr().out
    assert '"status": "idle"' in output and "Assistant>" in output
    assert len(store.attempts(reservation["session_id"])) == 2
    assert len(store.history(reservation["session_id"])["items"]) == 4
    assert store.get(reservation["session_id"])["status"] == "completed"


def test_text_inspection_needs_no_models(tmp_path, monkeypatch, capsys):
    from opentalk.voice import text
    def forbidden(**kwargs):
        raise AssertionError("Inspection must not initialize models.")
    monkeypatch.setattr(text, "open_session", forbidden)
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer:
        writer.write("/session help\n/session\n/session list\n/quit\n")
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(text.sys, "stdin", reader)
        asyncio.run(text.converse(agent_key="conversation"))
    assert '"status": "idle"' in capsys.readouterr().out


def test_restored_tool_receipts_do_not_execute_again(tmp_path):
    from datetime import UTC, datetime
    from opentalk.domain.booking_service import BookingService
    from opentalk.storage.repository import BookingRepository
    from opentalk.voice.factories import agent_factory
    service = BookingService(BookingRepository(tmp_path / "booking.sqlite3"),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    model = ScriptedLLM({"Go ahead": ("edit", {"action": "add", "room": "Room A",
                        "starts_at": "2030-01-02T09:00", "ends_at": "2030-01-02T10:00"})})
    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), model=model) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="Go ahead")
            session_id = session.userdata.session_id
        assert len(service.list_events()) == 1
        restored = ScriptedLLM()
        async with open_session(text_only=True, resume_id=session_id,
                                factory=agent_factory(service=service), model=restored) as (session, agent, journal):
            await session.start(agent=agent)
            assert any(item.type == "function_call_output" for item in agent.chat_ctx.items)
            await session.run(user_input="Hello again")
        assert len(service.list_events()) == 1
        history = journal.store.history(session_id)["items"]
        assert len([item for item in history if item["type"] == "function_call"]) == 1
        assert len([item for item in history if item["type"] == "function_call_output"]) == 1
    asyncio.run(scenario())


def test_optional_native_traces_are_separate_and_do_not_copy_content(tmp_path):
    from livekit.agents.telemetry import tracer
    from opentalk.sessions.tracing import session_trace
    from opentalk.sessions.state import SessionState
    from opentalk.sessions.telemetry import TelemetryStore
    config = replace(session_settings.load_session_config(), tracing_enabled=True)
    async def scenario():
        async with session_trace(config, SessionState("session", "attempt")):
            with tracer.start_as_current_span("test.pipeline", attributes={
                "lk.pii.transcript": "private-transcript", "gen_ai.input.messages": "private-prompt",
                "gen_ai.usage.input_tokens": 5,
            }):
                pass
        async with open_session(text_only=True, agent_key="conversation", session_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="private-conversation")
        native = journal.telemetry.read(session.userdata.session_id)
        assert any(event.get("name") == "agent_session" for event in native)
        assert any(event.get("name") == "llm_request_run" for event in native)
        assert "private-conversation" not in json.dumps([event for event in native if event["type"] == "trace"])
    asyncio.run(scenario())
    events = TelemetryStore(config.telemetry_database_path).read("session")
    assert any(event["type"] == "trace" and event["name"] == "test.pipeline" for event in events)
    assert "private-transcript" not in json.dumps(events)
    assert "private-prompt" not in json.dumps(events)
    assert any(event.get("attributes", {}).get("gen_ai.usage.input_tokens") == 5 for event in events)


def test_persistence_failure_prevents_normal_resume(tmp_path, monkeypatch):
    async def scenario():
        with pytest.raises(OSError, match="disk failure"):
            async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
                await session.start(agent=agent)
                await session.run(user_input="Hello")
                def fail(*args, **kwargs):
                    raise OSError("disk failure")
                monkeypatch.setattr(journal.store, "checkpoint", fail)
        assert journal.store.get(session.userdata.session_id)["status"] == "failed"
    asyncio.run(scenario())


def test_api_end_request_closes_native_session_and_commits_checkpoint(tmp_path):
    async def scenario():
        config = replace(session_settings.load_session_config(), heartbeat_seconds=0.02)
        async with open_session(text_only=True, agent_key="conversation", session_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="Hello")
            closed = asyncio.Event()
            session.on("close", lambda event: closed.set())
            journal.store.request_end(session.userdata.session_id, attempt_id=session.userdata.attempt_id)
            await asyncio.wait_for(closed.wait(), timeout=2)
        saved = journal.store.get(session.userdata.session_id)
        assert saved["status"] == "completed" and saved["end_requested"]
        assert len(journal.store.history(saved["session_id"])["items"]) == 2
    asyncio.run(scenario())


def test_provider_close_failure_keeps_session_failed(tmp_path):
    class FailingCloseModel(ScriptedLLM):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError("provider close failure")
    async def scenario():
        with pytest.raises(RuntimeError, match="provider close failure"):
            async with open_session(text_only=True, agent_key="conversation", model=FailingCloseModel()) as (session, agent, journal):
                await session.start(agent=agent)
                await session.run(user_input="Hello")
        saved = journal.store.get(session.userdata.session_id)
        assert saved["status"] == "failed"
        assert len(journal.store.history(saved["session_id"])["items"]) == 2
    asyncio.run(scenario())
