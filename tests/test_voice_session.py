"""Exercise native sessions with the generic agent and test-only tools."""

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import vad
from livekit.agents.llm import find_function_tools
from voice_fakes import FakeSTT, FakeTTS, PlaybackSink, QueueAudioInput, ScriptedLLM, tool_factory

from opentalk.agents.base import ConversationAgent
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory
from opentalk.voice.session import create_vad, open_session


def test_language_preference_survives_mixed_input(tmp_path):
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(), voice_config=config, model=ScriptedLLM()) as (session, agent, journal):
            assert isinstance(agent, ConversationAgent)
            assert {tool.info.name for tool in find_function_tools(type(agent))} == {
                "escalate_reasoning",
                "search_documents",
            }
            await session.start(agent=agent)
            await session.run(user_input="请用英语回复")
            await session.run(user_input="帮我 explain streaming tomorrow")
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


def test_audio_transcription_and_speech_output(tmp_path):
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    model = ScriptedLLM()

    async def scenario():
        async with open_session(factory=agent_factory(), voice_config=config, model=model,
                                stt_model=FakeSTT("hello"), tts_model=FakeTTS()) as (session, agent, journal):
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
            assert len(users) == 1 and users[0].text_content == "hello"
    asyncio.run(scenario())


def test_interruption_stops_obsolete_audio(tmp_path):
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    model = ScriptedLLM()
    model.slow = True
    synthesizer = FakeTTS()

    async def scenario():
        async with open_session(factory=agent_factory(), voice_config=config, model=model,
                                stt_model=FakeSTT(), tts_model=synthesizer) as (session, agent, journal):
            speaker = PlaybackSink()
            session.output.audio = speaker
            await session.start(agent=agent)
            result = session.run(user_input="Explain voice assistants", input_modality="audio")
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


def test_text_entrypoint_reads_pipe_and_saves(tmp_path, monkeypatch, capsys):
    from opentalk.voice import text
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    model = ScriptedLLM()
    monkeypatch.setattr(text, "open_session", lambda **kwargs: open_session(
        text_only=True, factory=agent_factory(), voice_config=config, model=model))
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer:
        writer.write("hello\n/quit\n")
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(text.sys, "stdin", reader)
        asyncio.run(text.converse())
    assert "Assistant>" in capsys.readouterr().out
    from opentalk.sessions.config import load_session_config
    from opentalk.sessions.store import SessionStore
    assert len(SessionStore(load_session_config().database_path).list()) == 1


def test_scoped_reasoning_then_default_model_resumes(tmp_path):
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")
    complex_request = "请比较这两个复杂方案"
    main = ScriptedLLM({complex_request: ("escalate_reasoning", {
        "task": "Compare the supplied options.",
        "message": "请稍等，我会更仔细地分析这个问题。",
    })}, replies={complex_request: "这是比较结果。"})
    strong = ScriptedLLM()

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(), voice_config=config,
                                model=main, reasoning_model=strong) as (session, agent, journal):
            await session.start(agent=agent)
            await asyncio.wait_for(session.run(user_input=complex_request), timeout=5)
            assistant_messages = [item.text_content for item in session.history.items
                                  if item.type == "message" and item.role == "assistant"]
            assert "请稍等，我会更仔细地分析这个问题。" in assistant_messages
            await session.run(user_input="simple")
            assert len(strong.requests) == 1 and strong.requests[0]["tools"] == 0
            assert main.requests[-1]["text"] == "simple"
            assert any(event["type"] == "reasoning_finished" and event["active_profile"] == "default"
                       for event in journal.report["events"])
            assert len([event for event in journal.report["events"] if event["type"] == "business_result"]) == 0
    asyncio.run(scenario())


def test_obsolete_tool_is_rejected(tmp_path):
    calls = []
    async def scenario():
        async with open_session(text_only=True, factory=tool_factory(calls), model=ScriptedLLM()) as (_, agent, _):
            context = context_for(agent, turn="old")
            agent.turn_id = "new"
            assert (await agent.echo(context, "obsolete"))["error"] == "stale_response"
            assert calls == []
    asyncio.run(scenario())


def test_started_tool_survives_speech_cancellation_and_is_logged(tmp_path):
    calls = []
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def action(text):
            started.set()
            await release.wait()
            return {"ok": True, "text": text}
        async with open_session(text_only=True, factory=tool_factory(calls, action), model=ScriptedLLM()) as (_, agent, journal):
            context = context_for(agent)
            task = asyncio.create_task(agent.echo(context, "committed"))
            await asyncio.wait_for(started.wait(), timeout=1)
            context.speech_handle.interrupted = True
            task.cancel()
            agent.turn_id = "new-turn"
            agent._speech_turns.clear()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == ["committed"]
            assert any(event["type"] == "business_result" and event["name"] == "echo" and event["result"]["ok"]
                       for event in journal.report["events"])
    asyncio.run(scenario())
