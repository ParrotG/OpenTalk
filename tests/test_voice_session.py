"""Exercise native sessions with the generic agent and SQL/edit booking subclass."""

import asyncio
import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from livekit import rtc
from livekit.agents import llm, vad
from livekit.agents.llm import find_function_tools
import pytest

from opentalk.agents.base import ConversationAgent
from opentalk.domain.booking_service import BookingService
from opentalk.storage.repository import BookingRepository
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory
from opentalk.voice.session import create_vad, open_session
from voice_fakes import FakeSTT, FakeTTS, PlaybackSink, QueueAudioInput, ScriptedLLM


def setup(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "voice.sqlite3"),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    service.seed_slot("Room A", datetime(2030, 1, 2, 1, tzinfo=UTC), datetime(2030, 1, 2, 2, tzinfo=UTC))
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    return service, config


def edit_arguments(action="add", **kwargs):
    return {"action": action, "resource": "Room A", "starts_at": "2030-01-02T09:00:00",
            "ends_at": "2030-01-02T10:00:00", **kwargs}


def test_native_queries_natural_confirmation_edits_and_logs(tmp_path):
    service, config = setup(tmp_path)
    model = ScriptedLLM({
        "any available rooms?": [("query", {"sql": "SELECT name AS room FROM resources"}),
                                 ("query", {"sql": "SELECT * FROM slots WHERE starts_at > '2030-01-01'"})],
        "Yes, that works": ("edit", edit_arguments()),
        "Go ahead and move it": ("edit", edit_arguments("update", new_starts_at="2030-01-02T10:00",
                                                         new_ends_at="2030-01-02T11:00")),
        "Please do": ("edit", edit_arguments("delete", starts_at="2030-01-02T10:00", ends_at="2030-01-02T11:00")),
    }, replies={"Book nine": "Reserve Room A, January 2, 09:00 to 10:00. Shall I proceed? "})

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config, model=model) as (session, agent, journal):
            assert isinstance(agent, ConversationAgent)
            assert {tool.info.name for tool in find_function_tools(type(agent))} == {"query", "edit", "escalate_reasoning"}
            await session.start(agent=agent)
            await session.run(user_input="any available rooms?")
            await session.run(user_input="Book nine")
            assert service.list_operations() == []
            await session.run(user_input="Yes, that works")
            await session.run(user_input="Go ahead and move it")
            await session.run(user_input="Please do")
            assert len(service.list_operations()) == 3
            assert (await agent.booking_tools.query("SELECT status FROM slot_users"))["rows"] == [{"status": "cancelled"}]
            await journal.save()
            await journal.save()
        report = journal.report
        history = journal.store.history(session.userdata.session_id)["items"]
        assert len(service.list_operations()) == 3
        assert {item["role"] for item in history if item["type"] == "message"} == {"user", "assistant"}
        calls = [event for event in report["events"] if event["type"] == "business_result"]
        assert [call["name"] for call in calls[:2]] == ["query", "query"]
        assert len({event["event_id"] for event in report["events"]}) == len(report["events"])
    asyncio.run(scenario())


def test_language_preference_survives_mixed_input(tmp_path):
    service, config = setup(tmp_path)

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config, model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="请用英语回复")
            await session.run(user_input="帮我 check Room A tomorrow")
            assert agent.preferred_response_language == "en"
            await session.run(user_input="please reply in Chinese")
            assert agent.preferred_response_language == "zh"
            assert not hasattr(agent, "detected_language")
    asyncio.run(scenario())


def test_silero_model_loads_and_performs_offline_inference():
    model = create_vad()
    assert model.model == "silero" and model._opts.sample_rate == 16000

    async def inference():
        stream = model.stream()
        try:
            for _ in range(10):
                stream.push_frame(rtc.AudioFrame(data=bytes(1024), sample_rate=16000,
                                                 num_channels=1, samples_per_channel=512))
            stream.end_input()
            events = [event async for event in stream]
            assert any(event.type == vad.VADEventType.INFERENCE_DONE for event in events)
            assert not any(event.type == vad.VADEventType.START_OF_SPEECH for event in events)
        finally:
            await stream.aclose()
    asyncio.run(inference())


@pytest.mark.parametrize("before,after", [
    ('livekit_url = "ws://localhost:7880"', 'livekit_url = "http://localhost:7880"'),
    ('max_tool_steps = 8', 'max_tool_steps = true'),
    ('activation_threshold = 0.5', 'activation_threshold = 2.0'),
    ('endpoint_max_delay = 3.0', 'endpoint_max_delay = -1.0'),
    ('log_flush_interval_seconds = 1.0', 'log_flush_interval_seconds = 0.0'),
])
def test_invalid_voice_configuration(tmp_path, before, after):
    from opentalk.config import PROJECT_ROOT
    path = tmp_path / "voice.toml"
    path.write_text((PROJECT_ROOT / "config/voice.toml").read_text().replace(before, after))
    with pytest.raises(ValueError, match="Invalid voice configuration"):
        load_voice_config(path)


def test_audio_transcription_query_and_speech_output(tmp_path):
    service, config = setup(tmp_path)
    model = ScriptedLLM({"list rooms": ("query", {"sql": "SELECT name AS room FROM resources"})})

    async def scenario():
        async with open_session(factory=agent_factory(service=service), voice_config=config, model=model,
                                stt_model=FakeSTT("list rooms"), tts_model=FakeTTS()) as (session, agent, journal):
            microphone, speaker = QueueAudioInput(), PlaybackSink()
            session.input.audio, session.output.audio = microphone, speaker
            finished = asyncio.Event()
            session.on("conversation_item_added", lambda event: finished.set()
                       if event.item.type == "message" and event.item.role == "assistant" else None)
            await session.start(agent=agent)
            await microphone.queue.put(rtc.AudioFrame(data=bytes(1024), sample_rate=16000,
                                                       num_channels=1, samples_per_channel=512))
            await asyncio.wait_for(finished.wait(), timeout=5)
            assert speaker.frames
            assert session.options.turn_handling["turn_detection"] == "stt"
            assert session.options.interruption["mode"] == "vad"
            users = [item for item in session.history.items if item.type == "message" and item.role == "user"]
            assert len(users) == 1 and users[0].text_content == "list rooms"
            assert any(event["type"] == "business_result" and event["name"] == "query"
                       for event in journal.report["events"])
    asyncio.run(scenario())


def test_interruption_stops_obsolete_audio(tmp_path):
    service, config = setup(tmp_path)
    model = ScriptedLLM()
    model.slow = True
    synthesizer = FakeTTS()

    async def scenario():
        async with open_session(factory=agent_factory(service=service), voice_config=config, model=model,
                                stt_model=FakeSTT(), tts_model=synthesizer) as (session, agent, journal):
            speaker = PlaybackSink()
            session.output.audio = speaker
            await session.start(agent=agent)
            result = session.run(user_input="Explain booking", input_modality="audio")
            await asyncio.wait_for(speaker.first_audio.wait(), timeout=2)
            speech = session.current_speech
            await session.interrupt(force=True)
            await result
            assert speech.interrupted and speaker.cleared > 0 and synthesizer.cancelled > 0
            count = len(speaker.frames)
            await asyncio.sleep(0.25)
            assert len(speaker.frames) == count
            model.slow = False
            await session.run(user_input="please reply in English", input_modality="audio")
            assert len(speaker.frames) > count and synthesizer.languages[-1] == "en"
    asyncio.run(scenario())


def context_for(agent, turn="current", response="response"):
    agent.turn_id = turn
    agent._speech_turns[response] = turn
    return SimpleNamespace(speech_handle=SimpleNamespace(id=response, interrupted=False),
                           function_call=SimpleNamespace(call_id="call"))


def test_obsolete_edit_is_rejected(tmp_path):
    service, config = setup(tmp_path)

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config, model=ScriptedLLM()) as (_, agent, _):
            context = context_for(agent, turn="old")
            agent.turn_id = "new"
            assert (await agent.edit(context, **edit_arguments()))["error"] == "stale_response"
            assert service.list_operations() == []
    asyncio.run(scenario())


def test_started_edit_survives_speech_cancellation_and_is_logged(tmp_path, monkeypatch):
    service, config = setup(tmp_path)
    started, release = threading.Event(), threading.Event()

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config, model=ScriptedLLM()) as (_, agent, journal):
            original = service.edit

            def delayed(*args, **kwargs):
                started.set()
                assert release.wait(timeout=3)
                return original(*args, **kwargs)
            monkeypatch.setattr(service, "edit", delayed)
            context = context_for(agent)
            task = asyncio.create_task(agent.edit(context, **edit_arguments()))
            assert await asyncio.to_thread(started.wait, 1)
            context.speech_handle.interrupted = True
            task.cancel()
            agent.turn_id = "new-turn"
            agent._speech_turns.clear()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(service.list_operations()) == 1
            assert any(event["type"] == "business_result" and event["name"] == "edit" and event["result"]["ok"]
                       for event in journal.report["events"])
    asyncio.run(scenario())


def test_text_entrypoint_reads_pipe_and_saves(tmp_path, monkeypatch, capsys):
    from opentalk.voice import text
    service, config = setup(tmp_path)
    model = ScriptedLLM({"list rooms": ("query", {"sql": "SELECT name AS room FROM resources"})})
    monkeypatch.setattr(text, "open_session", lambda **kwargs: open_session(
        text_only=True, factory=agent_factory(service=service), voice_config=config, model=model))
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer:
        writer.write("list rooms\n/quit\n")
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(text.sys, "stdin", reader)
        asyncio.run(text.converse(None))
    assert "Assistant>" in capsys.readouterr().out
    from opentalk.sessions.config import load_session_config
    from opentalk.sessions.store import SessionStore
    assert len(SessionStore(load_session_config().database_path).list()) == 1


def test_scoped_reasoning_then_default_model_resumes(tmp_path):
    service, config = setup(tmp_path)
    main = ScriptedLLM({"complex": [("escalate_reasoning", {"task": "Compare the supplied options."}),
                                    ("query", {"sql": "SELECT name AS room FROM resources"})]})
    strong = ScriptedLLM()

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config,
                                model=main, reasoning_model=strong) as (session, agent, journal):
            await session.start(agent=agent)
            await asyncio.wait_for(session.run(user_input="complex"), timeout=5)
            await session.run(user_input="simple")
            assert len(strong.requests) == 1 and strong.requests[0]["tools"] == 0
            assert main.requests[-1]["text"] == "simple"
            assert any(event["type"] == "reasoning_finished" and event["active_profile"] == "default"
                       for event in journal.report["events"])
            assert len([event for event in journal.report["events"] if event["type"] == "business_result"]) == 1
    asyncio.run(scenario())


def test_one_confirmed_turn_can_edit_two_resources_and_cannot_impersonate_another_user(tmp_path):
    service, config = setup(tmp_path)
    service.seed_resource("Projector", resource_type="equipment")
    model = ScriptedLLM({
        "Confirm both": [("edit", edit_arguments()),
                         ("edit", edit_arguments(resource="Projector"))],
        "Impersonate Bob": ("edit", edit_arguments(resource="Projector", uid="bob",
                                                    starts_at="2030-01-02T10:00", ends_at="2030-01-02T11:00")),
    })
    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config,
                                model=model) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="Confirm both")
            assert len(service.list_operations()) == 2
            assert len({operation.operation_id for operation in service.list_operations()}) == 2
            await session.run(user_input="Impersonate Bob")
            assert len(service.list_operations()) == 2
            results = [item for item in session.history.items if item.type == 'function_call_output']
            assert any('forbidden' in str(item.output) for item in results)
    asyncio.run(scenario())
