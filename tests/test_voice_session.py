"""Exercise the native AgentSession, tool runner, persistence, and interruption boundary."""

import asyncio
import json
import os
import threading
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

from livekit import rtc
from livekit.agents import llm, vad
import pytest

from opentalk.domain.booking_service import BookingService
from opentalk.storage.repository import BookingRepository
from opentalk.voice.config import load_voice_config
from opentalk.voice.session import create_vad, open_session
from voice_fakes import FakeSTT, FakeTTS, PlaybackSink, QueueAudioInput, ScriptedLLM


def setup(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "voice.sqlite3"),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    slots = [service.seed_slot("Room A", datetime(2030, 1, 2, hour, tzinfo=UTC),
                              datetime(2030, 1, 2, hour + 1, tzinfo=UTC)) for hour in (1, 2)]
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    return service, slots, config


def test_native_booking_change_confirm_cancel_and_logs(tmp_path):
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM({
        "select nine": ("prepare_booking", {"slot_id": slots[0].slot_id}),
        "actually ten": ("prepare_booking", {"slot_id": slots[1].slot_id}),
    })

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config, model=model) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="select nine")
            await asyncio.sleep(0)
            old = agent._pending.copy()
            assert agent._ready == (old["operation_id"], old["version"])
            await session.run(user_input="actually ten")
            await asyncio.sleep(0)
            current = agent._pending.copy()
            assert current["version"] == 2
            assert service.get_operation(old["operation_id"]).status == "invalidated"
            model.actions["确认预约"] = ("confirm_booking", {
                "operation_id": current["operation_id"], "version": current["version"],
            })
            await session.run(user_input="确认预约")
            booking = service.get_operation(current["operation_id"]).result
            assert booking.slot_id == slots[1].slot_id and booking.status == "active"
            model.actions["cancel booking"] = ("cancel_booking", {"booking_id": booking.booking_id})
            await session.run(user_input="cancel booking")
            assert service.get_booking(booking.booking_id).status == "cancelled"
            await journal.save(service)
            await journal.save(service)
        report = json.loads(journal.path.read_text())
        assert len(report["operation_events"]) == 5
        assert any(item["role"] == "user" for item in report["messages"].values())
        assert any(item["role"] == "assistant" for item in report["messages"].values())
        assert {event["name"] for event in report["events"] if event["type"] == "business_result"} >= {
            "prepare_booking", "confirm_booking", "cancel_booking",
        }
        assert len({event["event_id"] for event in report["events"]}) == len(report["events"])
        assert "api_key" not in journal.path.read_text().lower()
    asyncio.run(scenario())


def test_no_implicit_confirmation_or_stale_version(tmp_path):
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM({"select nine": ("prepare_booking", {"slot_id": slots[0].slot_id})})

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config, model=model) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="select nine")
            proposal = agent._pending.copy()
            arguments = {"operation_id": proposal["operation_id"], "version": proposal["version"]}
            model.actions["yes, but change the time"] = ("confirm_booking", arguments)
            await session.run(user_input="yes, but change the time")
            assert service.get_operation(proposal["operation_id"]).status == "pending"
            model.actions["confirm booking"] = ("confirm_booking", {**arguments, "version": 99})
            await session.run(user_input="confirm booking")
            assert len(service.list_available_slots(date(2030, 1, 2))) == 2
            errors = [event["result"].get("error") for event in journal.report["events"]
                      if event["type"] == "business_result"]
            assert errors.count("confirmation_required") == 2
    asyncio.run(scenario())


def test_language_preference_survives_mixed_input(tmp_path):
    service, slots, config = setup(tmp_path)

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config, model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="请用英语回复")
            await session.run(user_input="帮我 check Room A tomorrow")
            assert agent.preferred_response_language == "en"
            await session.run(user_input="please reply in Chinese")
            assert agent.preferred_response_language == "zh"
            assert not hasattr(agent, "detected_language")
    asyncio.run(scenario())


def test_silero_model_loads_offline():
    model = create_vad()
    assert model.model == "silero"
    assert model._opts.sample_rate == 16000
    assert model._opts.min_silence_duration == 0.55

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


def test_audio_input_transcription_agent_tool_and_speech_output(tmp_path):
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM({"select nine": ("prepare_booking", {"slot_id": slots[0].slot_id})})

    async def scenario():
        async with open_session(service=service, voice_config=config, model=model,
                                stt_model=FakeSTT("select nine"), tts_model=FakeTTS()) as (session, agent, journal):
            microphone, speaker = QueueAudioInput(), PlaybackSink()
            session.input.audio = microphone
            session.output.audio = speaker
            finished = asyncio.Event()
            session.on("conversation_item_added", lambda event: finished.set()
                       if event.item.type == "message" and event.item.role == "assistant" else None)
            await session.start(agent=agent)
            await microphone.queue.put(rtc.AudioFrame(data=bytes(1024), sample_rate=16000,
                                                       num_channels=1, samples_per_channel=512))
            await asyncio.wait_for(finished.wait(), timeout=5)
            await asyncio.sleep(0)
            assert agent._pending is not None and agent._ready is not None
            assert speaker.frames
            assert service.get_operation(agent._pending["operation_id"]).status == "pending"
            assert session.options.turn_handling["turn_detection"] == "stt"
            assert session.options.interruption["mode"] == "vad"
            report = journal.report
            assert any(event["type"] == "asr_transcript" and not event["is_final"] for event in report["events"])
            users = [message for message in report["messages"].values() if message["role"] == "user"]
            assert len(users) == 1 and users[0]["text"] == "select nine"
    asyncio.run(scenario())


def test_interruption_cancels_synthesis_and_prevents_obsolete_audio(tmp_path):
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM()
    model.slow = True
    synthesizer = FakeTTS()

    async def scenario():
        async with open_session(service=service, voice_config=config, model=model,
                                stt_model=FakeSTT(), tts_model=synthesizer) as (session, agent, journal):
            speaker = PlaybackSink()
            session.output.audio = speaker
            await session.start(agent=agent)
            result = session.run(user_input="Explain the booking process", input_modality="audio")
            await asyncio.wait_for(speaker.first_audio.wait(), timeout=2)
            speech = session.current_speech
            await session.interrupt(force=True)
            await result
            assert speech.interrupted
            assert speaker.cleared > 0 and synthesizer.cancelled > 0
            count = len(speaker.frames)
            await asyncio.sleep(0.25)
            assert len(speaker.frames) == count
            model.slow = False
            await session.run(user_input="please reply in English", input_modality="audio")
            assert len(speaker.frames) > count
            assert agent.preferred_response_language == "en" and synthesizer.languages[-1] == "en"
            assert any(event["type"] == "response_finished" and event["interrupted"] for event in journal.report["events"])
    asyncio.run(scenario())


def test_interrupted_proposal_cannot_be_confirmed(tmp_path):
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM({"select nine": ("prepare_booking", {"slot_id": slots[0].slot_id})})

    async def scenario():
        async with open_session(service=service, voice_config=config, model=model,
                                stt_model=FakeSTT(), tts_model=FakeTTS()) as (session, agent, journal):
            speaker = PlaybackSink()
            session.output.audio = speaker
            await session.start(agent=agent)
            result = session.run(user_input="select nine", input_modality="audio")
            await asyncio.wait_for(speaker.first_audio.wait(), timeout=2)
            proposal = agent._pending.copy()
            await session.interrupt(force=True)
            await result
            await asyncio.sleep(0)
            assert agent._ready is None
            model.actions["confirm booking"] = ("confirm_booking", {
                "operation_id": proposal["operation_id"], "version": proposal["version"],
            })
            await session.run(user_input="confirm booking", input_modality="audio")
            assert service.get_operation(proposal["operation_id"]).status == "pending"
            assert len(service.list_available_slots(date(2030, 1, 2))) == 2
    asyncio.run(scenario())


def test_obsolete_tool_call_is_rejected_before_execution(tmp_path):
    service, slots, config = setup(tmp_path)

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            agent._begin_turn(llm.ChatMessage(id="old", role="user", content=["select nine"]))
            agent._speech_turns["old-response"] = "old"
            context = SimpleNamespace(speech_handle=SimpleNamespace(id="old-response", interrupted=False),
                                      function_call=SimpleNamespace(call_id="old-call"))
            agent._begin_turn(llm.ChatMessage(id="new", role="user", content=["actually ten"]))
            result = await agent.prepare_booking(context, slots[0].slot_id)
            assert result["error"] == "stale_response"
            assert service.list_events() == []
    asyncio.run(scenario())


def test_started_commit_survives_speech_cancellation_and_is_logged(tmp_path, monkeypatch):
    service, slots, config = setup(tmp_path)
    proposal = service.prepare_booking(slots[0].slot_id, "confirmed-operation")
    started, release = threading.Event(), threading.Event()
    original = service.confirm_booking

    def delayed_commit(*args):
        started.set()
        assert release.wait(timeout=3)
        return original(*args)
    monkeypatch.setattr(service, "confirm_booking", delayed_commit)

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            agent._pending = {"operation_id": proposal.operation_id, "version": proposal.version}
            agent._ready = (proposal.operation_id, proposal.version)
            agent._begin_turn(llm.ChatMessage(id="confirm", role="user", content=["confirm booking"]))
            agent._speech_turns["confirm-response"] = "confirm"
            context = SimpleNamespace(speech_handle=SimpleNamespace(id="confirm-response", interrupted=False),
                                      function_call=SimpleNamespace(call_id="confirm-call"))
            task = asyncio.create_task(agent.confirm_booking(context, proposal.operation_id, proposal.version))
            assert await asyncio.to_thread(started.wait, 1)
            context.speech_handle.interrupted = True
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            operation = service.get_operation(proposal.operation_id)
            assert operation.status == "succeeded" and operation.result.status == "active"
            assert any(event["type"] == "business_result" and event["name"] == "confirm_booking"
                       and event["result"]["ok"] for event in journal.report["events"])
    asyncio.run(scenario())


def test_text_entrypoint_reads_pipe_and_persists_session(tmp_path, monkeypatch, capsys):
    from opentalk.voice import text
    service, slots, config = setup(tmp_path)
    model = ScriptedLLM({"select nine": ("prepare_booking", {"slot_id": slots[0].slot_id})})
    monkeypatch.setattr(text, "open_session", lambda **kwargs: open_session(
        text_only=True, service=service, voice_config=config, model=model))
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer:
        writer.write("select nine\n/quit\n")
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(text.sys, "stdin", reader)
        asyncio.run(text.converse(None))
    output = capsys.readouterr().out
    assert "Assistant> Please confirm Room A" in output
    assert "report_file" in output
    reports = list(config.log_directory.glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["operation_events"]
    assert len(service.list_available_slots(date(2030, 1, 2))) == 2


def test_cancellation_authorization_rechecks_current_turn(tmp_path, monkeypatch):
    service, slots, config = setup(tmp_path)
    proposal = service.prepare_booking(slots[0].slot_id, "original")
    booking = service.confirm_booking(proposal.operation_id, proposal.version)
    started, release = threading.Event(), threading.Event()

    async def scenario():
        async with open_session(text_only=True, service=service, voice_config=config,
                                model=ScriptedLLM()) as (session, agent, journal):
            original = agent.booking_tools.authorize_cancellation

            def delayed_authorization(booking_id):
                started.set()
                assert release.wait(timeout=3)
                original(booking_id)
            monkeypatch.setattr(agent.booking_tools, "authorize_cancellation", delayed_authorization)
            agent._latest_booking = booking.booking_id
            agent._begin_turn(llm.ChatMessage(id="cancel", role="user", content=["cancel booking"]))
            agent._speech_turns["cancel-response"] = "cancel"
            context = SimpleNamespace(speech_handle=SimpleNamespace(id="cancel-response", interrupted=False),
                                      function_call=SimpleNamespace(call_id="cancel-call"))
            task = asyncio.create_task(agent.cancel_booking(context, booking.booking_id))
            assert await asyncio.to_thread(started.wait, 1)
            agent._begin_turn(llm.ChatMessage(id="keep", role="user", content=["keep my booking"]))
            release.set()
            assert (await task)["error"] == "stale_response"
            assert service.get_booking(booking.booking_id).status == "active"
    asyncio.run(scenario())
