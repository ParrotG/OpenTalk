"""Native model doubles for deterministic session tests without provider requests."""

import asyncio
import json
from uuid import uuid4

from livekit import rtc
from livekit.agents import llm, stt, tts
from livekit.agents.voice import io


class ScriptedLLM(llm.LLM):
    def __init__(self, actions=None, replies=None):
        super().__init__()
        self.actions = actions or {}
        self.replies = replies or {}
        self.requests = []
        self.started = asyncio.Event()
        self.slow = False

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        return ScriptedStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class ScriptedStream(llm.LLMStream):
    async def _run(self):
        user_index = next(index for index in range(len(self._chat_ctx.items) - 1, -1, -1)
                          if self._chat_ctx.items[index].type == "message"
                          and self._chat_ctx.items[index].role == "user")
        text = self._chat_ctx.items[user_index].text_content
        self._llm.started.set()
        self._llm.requests.append({"text": text, "tools": len(self._tools)})
        outputs = [item for item in self._chat_ctx.items[user_index + 1:] if item.type == "function_call_output"]
        action = self._llm.actions.get(text)
        if isinstance(action, list):
            action = action[len(outputs)] if len(outputs) < len(action) else None
        elif outputs:
            action = None
        if action:
            name, arguments = action(self._chat_ctx) if callable(action) else action
            self._event_ch.send_nowait(llm.ChatChunk(id=uuid4().hex, delta=llm.ChoiceDelta(
                role="assistant", tool_calls=[llm.FunctionToolCall(
                    call_id=uuid4().hex, name=name, arguments=json.dumps(arguments),
                )],
            )))
            return
        for part in (self._llm.replies.get(text, "I checked the request. "), "Please review the result."):
            self._event_ch.send_nowait(llm.ChatChunk(id=uuid4().hex,
                                                   delta=llm.ChoiceDelta(role="assistant", content=part)))
            if self._llm.slow:
                await asyncio.sleep(0.2)


class FakeTTS(tts.TTS):
    def __init__(self):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=True), sample_rate=24000, num_channels=1)
        self.languages = []
        self.cancelled = 0

    def update_options(self, *, language):
        self.languages.append(language)

    def synthesize(self, text, *, conn_options):
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options):
        return FakeSynthesis(tts=self, conn_options=conn_options)


class FakeSynthesis(tts.SynthesizeStream):
    async def _run(self, emitter):
        emitter.initialize(request_id=uuid4().hex, sample_rate=24000, num_channels=1,
                           mime_type="audio/pcm", stream=True)
        emitter.start_segment(segment_id=uuid4().hex)
        try:
            async for text in self._input_ch:
                if isinstance(text, str):
                    emitter.push(b"\x01\x00" * 2400)
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self._tts.cancelled += 1
            raise
        finally:
            emitter.end_segment()


class FakeSTT(stt.STT):
    def __init__(self, text="hello"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))
        self.text = text

    async def _recognize_impl(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, *, conn_options, **kwargs):
        return FakeRecognition(stt=self, conn_options=conn_options)


class FakeRecognition(stt.SpeechStream):
    async def _run(self):
        emitted = False
        async for frame in self._input_ch:
            if not isinstance(frame, rtc.AudioFrame) or emitted:
                continue
            emitted = True
            for kind in (stt.SpeechEventType.START_OF_SPEECH, stt.SpeechEventType.INTERIM_TRANSCRIPT,
                         stt.SpeechEventType.FINAL_TRANSCRIPT, stt.SpeechEventType.END_OF_SPEECH):
                self._event_ch.send_nowait(stt.SpeechEvent(type=kind, alternatives=(
                    [stt.SpeechData(text=self._stt.text, language="")] if "transcript" in kind.value else []
                )))


class QueueAudioInput(io.AudioInput):
    def __init__(self):
        super().__init__(label="Simulated microphone")
        self.queue = asyncio.Queue()

    async def __anext__(self):
        return await self.queue.get()


class PlaybackSink(io.AudioOutput):
    def __init__(self):
        super().__init__(label="Simulated speaker", capabilities=io.AudioOutputCapabilities(pause=False), sample_rate=24000)
        self.frames = []
        self.first_audio = asyncio.Event()
        self.cleared = 0
        self.active = False
        self.position = 0.0

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.active = True
        await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)
        self.position += frame.samples_per_channel / frame.sample_rate
        self.frames.append(frame)
        self.first_audio.set()

    def flush(self):
        super().flush()
        if self.active:
            self.active = False
            self.on_playback_finished(playback_position=self.position, interrupted=False)
            self.position = 0.0

    def clear_buffer(self):
        self.cleared += 1
        super().flush()
        if self.active:
            self.active = False
            self.on_playback_finished(playback_position=self.position, interrupted=True)
            self.position = 0.0
