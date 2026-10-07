"""Test native text/audio switching and graceful browser closure without provider calls."""

import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents.voice import io

from opentalk.voice.room_control import RoomAudioMode, close_disconnected_session
from opentalk.voice.session import open_session
from voice_fakes import FakeSTT, FakeTTS, PlaybackSink, ScriptedLLM


class CountingTTS(FakeTTS):
    def __init__(self):
        super().__init__()
        self.streams = 0

    def stream(self, **kwargs):
        self.streams += 1
        return super().stream(**kwargs)


class TextSink(io.TextOutput):
    def __init__(self):
        super().__init__(label="Test text output", next_in_chain=None)
        self.parts = []
        self.first_text = asyncio.Event()
        self.flushes = 0

    async def capture_text(self, text):
        self.parts.append(str(text))
        self.first_text.set()

    def flush(self):
        self.flushes += 1


def test_native_text_mode_skips_tts_and_voice_mode_can_be_reenabled():
    async def scenario():
        tts = CountingTTS()
        async with open_session(agent_key="conversation", model=ScriptedLLM(),
                                stt_model=FakeSTT(), tts_model=tts) as (session, agent, journal):
            speaker, text = PlaybackSink(), TextSink()
            session.output.audio = speaker
            session.output.transcription = text
            try:
                mode = agent.audio_mode = RoomAudioMode(session, enabled=False)
                await session.start(agent=agent)
                mode.configure_outputs()
                await asyncio.wait_for(session.run(user_input="Text request"), timeout=3)
                assert tts.streams == 0 and not speaker.frames
                assert "Please review the result." in "".join(text.parts)
                await mode.switch(True)
                await asyncio.wait_for(session.run(user_input="Voice request"), timeout=3)
                assert tts.streams > 0 and speaker.frames
                await mode.switch(False)
                streams = tts.streams
                await asyncio.wait_for(session.run(user_input="Text again"), timeout=3)
                assert tts.streams == streams
            finally:
                await mode.aclose()
    asyncio.run(scenario())


def test_cancel_voice_stops_current_tts_but_preserves_the_original_text_generation():
    async def scenario():
        model, tts = ScriptedLLM(), CountingTTS()
        model.slow = True
        async with open_session(agent_key="conversation", model=model,
                                stt_model=FakeSTT(), tts_model=tts) as (session, agent, journal):
            speaker, text = PlaybackSink(), TextSink()
            session.output.audio = speaker
            session.output.transcription = text
            try:
                mode = agent.audio_mode = RoomAudioMode(session)
                await session.start(agent=agent)
                mode.configure_outputs()
                response = session.generate_reply(user_input="Continue this response")
                await asyncio.wait_for(speaker.first_audio.wait(), timeout=3)
                await mode.switch(False)
                await asyncio.wait_for(response, timeout=3)
                assert tts.cancelled == 1
                assert len(model.requests) == 1
                assistant = [item for item in session.history.items
                             if item.type == "message" and item.role == "assistant"]
                assert len(assistant) == 1 and not assistant[0].interrupted
                assert assistant[0].text_content == "I checked the request. Please review the result."
                assert "".join(text.parts) == assistant[0].text_content
                assert text.flushes == 1
            finally:
                await mode.aclose()
    asyncio.run(scenario())


def test_enabling_voice_mid_text_reply_keeps_that_reply_unsynchronized():
    async def scenario():
        model, tts = ScriptedLLM(), CountingTTS()
        model.slow = True
        async with open_session(agent_key="conversation", model=model,
                                stt_model=FakeSTT(), tts_model=tts) as (session, agent, journal):
            speaker, text = PlaybackSink(), TextSink()
            session.output.audio = speaker
            session.output.transcription = text
            mode = agent.audio_mode = RoomAudioMode(session, enabled=False)
            try:
                await session.start(agent=agent)
                mode.configure_outputs()
                response = session.generate_reply(user_input="Text first")
                await asyncio.wait_for(text.first_text.wait(), timeout=3)
                await mode.switch(True)
                await asyncio.wait_for(response, timeout=3)
                assert tts.streams == 0
                assert "".join(text.parts) == "I checked the request. Please review the result."
                await asyncio.wait_for(session.run(user_input="Voice next"), timeout=3)
                assert tts.streams > 0
            finally:
                await mode.aclose()
    asyncio.run(scenario())


def test_page_close_intent_may_arrive_after_the_media_disconnect():
    async def scenario():
        async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            close = asyncio.create_task(close_disconnected_session(
                session, journal, SimpleNamespace(remote_participants={}), "User", grace_seconds=1))
            await asyncio.sleep(0.05)
            journal.store.request_end(session.userdata.session_id, attempt_id=session.userdata.attempt_id)
            await asyncio.wait_for(close, timeout=2)
        assert journal.store.get(session.userdata.session_id)["status"] == "completed"
    asyncio.run(scenario())


def test_committed_messages_checkpoint_without_waiting_for_periodic_heartbeat():
    async def scenario():
        async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="Save automatically")
            await asyncio.wait_for(journal.flush(), timeout=2)
            items = journal.store.history(session.userdata.session_id)["items"]
            assert [item["role"] for item in items] == ["user", "assistant"]
            assert journal.store.get(session.userdata.session_id)["status"] == "active"
    asyncio.run(scenario())


@pytest.mark.parametrize("normal", [True, False])
def test_browser_disconnect_requires_a_normal_close_intent_to_resume(normal):
    async def scenario():
        async with open_session(text_only=True, agent_key="conversation", model=ScriptedLLM()) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="Keep my history")
            if normal:
                journal.store.request_end(session.userdata.session_id, attempt_id=session.userdata.attempt_id)
            await close_disconnected_session(session, journal, SimpleNamespace(remote_participants={}),
                                             "User", grace_seconds=0)
        saved = journal.store.get(session.userdata.session_id)
        assert saved["status"] == ("completed" if normal else "failed")
        assert len(journal.store.history(saved["session_id"])["items"]) == 2
    asyncio.run(scenario())


def test_audio_mode_rpc_validates_the_linked_caller_and_payload():
    async def scenario():
        methods = {}
        session = SimpleNamespace(
            output=io.AgentOutput(lambda: None, lambda: None, lambda: None),
            input=io.AgentInput(lambda: None, lambda: None),
        )
        mode = RoomAudioMode(session, enabled=False)
        mode.register(SimpleNamespace(local_participant=SimpleNamespace(
            register_rpc_method=lambda name, handler: methods.update({name: handler}))), "User")
        update = methods["opentalk.set_voice_mode"]
        with pytest.raises(rtc.RpcError):
            await update(SimpleNamespace(caller_identity="Other", payload='{"enabled":false}'))
        with pytest.raises(rtc.RpcError):
            await update(SimpleNamespace(caller_identity="User", payload='{"enabled":"false"}'))
        assert await update(SimpleNamespace(caller_identity="User", payload='{"enabled":false}')) == '{"enabled": false}'
    asyncio.run(scenario())
