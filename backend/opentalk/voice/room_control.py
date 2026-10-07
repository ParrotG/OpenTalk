"""Browser transport controls, independent of the agent's business tools."""

import asyncio
import json
import time

from livekit import rtc
from livekit.agents.voice import io
from livekit.agents.voice.transcription import TranscriptSynchronizer


VOICE_MODE_METHOD = "opentalk.set_voice_mode"


class _ForwardedText(io.TextOutput):
    def __init__(self, router, output):
        super().__init__(label="Forwarded browser text", next_in_chain=output)
        self.router = router

    async def capture_text(self, text):
        self.router.forwarded += str(text)
        await self.next_in_chain.capture_text(text)

    def flush(self):
        if not self.router.switching:
            self.next_in_chain.flush()


class _ModeTextOutput(io.TextOutput):
    def __init__(self, mode, output):
        super().__init__(label="Browser text mode", next_in_chain=output)
        self.mode = mode
        self.lock = asyncio.Lock()
        self.generated = self.forwarded = ""
        self.capturing = False
        self.text_only = False
        self.switching = False

    async def capture_text(self, text):
        async with self.lock:
            if not self.capturing:
                self.capturing = True
                self.generated = self.forwarded = ""
                handle = self.mode.session.current_speech
                self.text_only = not self.mode.session.output.audio_enabled or (
                    handle is not None and not self.mode.speech_modes.get(handle.id, True))
            self.generated += str(text)
            if self.text_only:
                self.forwarded += str(text)
                await self.next_in_chain.capture_text(text)
            else:
                await self.mode.synchronizer.text_output.capture_text(text)

    def flush(self):
        if self.text_only:
            self.next_in_chain.flush()
        else:
            self.mode.synchronizer.text_output.flush()
        self.capturing = False

    def on_attached(self):
        self.mode.synchronizer.text_output.on_attached()

    def on_detached(self):
        self.mode.synchronizer.text_output.on_detached()


class RoomAudioMode:
    def __init__(self, session, *, enabled=True):
        self.session = session
        self.disabled = asyncio.Event()
        self.synchronizer = None
        self.text_output = None
        self.speech_modes = {}
        self.ready = asyncio.Event()
        self.set_enabled(enabled)

    def configure_outputs(self):
        self.text_output = _ModeTextOutput(self, self.session.output.transcription)
        self.synchronizer = TranscriptSynchronizer(
            next_in_chain_audio=self.session.output.audio,
            next_in_chain_text=_ForwardedText(self.text_output, self.session.output.transcription))
        self.session.output.audio = self.synchronizer.audio_output
        self.session.output.transcription = self.text_output

        def created(event):
            self.speech_modes[event.speech_handle.id] = self.session.output.audio_enabled
            while len(self.speech_modes) > 16:
                del self.speech_modes[next(iter(self.speech_modes))]
        self.session.on("speech_created", created)
        self.ready.set()

    async def switch(self, enabled):
        if enabled or self.text_output is None:
            self.set_enabled(enabled)
            return
        async with self.text_output.lock:
            self.text_output.switching = True
            try:
                self.set_enabled(False)
                await self.synchronizer.barrier()
                # Native detachment drops buffered, unspoken text. Forward that prefix once
                # before allowing the original LLM stream to continue without synchronization.
                pending = self.text_output.generated[len(self.text_output.forwarded):]
                self.text_output.text_only = True
                if pending:
                    await self.text_output.next_in_chain.capture_text(pending)
                    self.text_output.forwarded += pending
                if not self.text_output.capturing:
                    self.text_output.next_in_chain.flush()
            finally:
                self.text_output.switching = False

    async def aclose(self):
        if self.synchronizer is not None:
            await self.synchronizer.aclose()

    def set_enabled(self, enabled):
        if enabled and self.disabled.is_set():
            self.disabled = asyncio.Event()
        elif not enabled:
            self.disabled.set()
        # Native output detachment allows subsequent text to bypass playback synchronization.
        if self.session.output.audio_enabled != enabled:
            self.session.output.set_audio_enabled(enabled)
        if self.session.input.audio_enabled != enabled:
            self.session.input.set_audio_enabled(enabled)
        if not enabled and self.session.output.audio is not None:
            self.session.output.audio.clear_buffer()

    def register(self, room, identity, *, wait_for_ready=False):
        async def update(data):
            if data.caller_identity != identity:
                raise rtc.RpcError(1403, "Only the linked user can change audio mode.")
            try:
                body = json.loads(data.payload)
                enabled = body["enabled"]
                if type(enabled) is not bool:
                    raise ValueError("A boolean is required.")
            except (ValueError, KeyError, TypeError):
                raise rtc.RpcError(1400, "Invalid audio mode request.")
            if wait_for_ready:
                # Data channels can arrive before the signaling participant event.
                # A successful acknowledgement must also mean RoomIO can accept chat.
                try:
                    async with asyncio.timeout(8):
                        await self.ready.wait()
                        while identity not in room.remote_participants:
                            await asyncio.sleep(0.01)
                except TimeoutError:
                    raise rtc.RpcError(1503, "The conversation is not ready yet.")
            await self.switch(enabled)
            return json.dumps({"enabled": enabled})
        room.local_participant.register_rpc_method(VOICE_MODE_METHOD, update)

    async def frames(self, stream, text):
        """Stop active synthesis without restarting the LLM or its tool executions."""
        if self.disabled.is_set():
            await stream.aclose()
            async for _ in text:
                pass
            return
        stop = asyncio.create_task(self.disabled.wait())
        next_frame = None
        switched = False
        try:
            while True:
                next_frame = asyncio.create_task(anext(stream))
                done, _ = await asyncio.wait((next_frame, stop), return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    switched = True
                    break
                try:
                    yield next_frame.result()
                except StopAsyncIteration:
                    break
        finally:
            for task in (next_frame, stop):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (next_frame, stop) if task is not None),
                                 return_exceptions=True)
            await stream.aclose()
        if switched:
            # Keep consuming this branch so the original LLM can finish its text response.
            async for _ in text:
                pass


async def close_disconnected_session(session, journal, room, identity, *, grace_seconds=2):
    """Allow a page-close request to arrive before classifying a lost connection."""
    deadline = time.monotonic() + grace_seconds
    while True:
        if identity in room.remote_participants:
            return
        try:
            saved = await asyncio.to_thread(journal.store.get, journal.state.session_id)
        except Exception as error:
            journal.failed, journal.error_code = True, "persistence_failed"
            journal.record("disconnect_checkpoint_error", error_type=type(error).__name__)
            break
        if saved["end_requested"]:
            break
        if time.monotonic() >= deadline:
            journal.failed, journal.error_code = True, "participant_disconnected"
            break
        await asyncio.sleep(0.1)
    await session.aclose()
