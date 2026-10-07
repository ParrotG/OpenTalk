"""Compose independently configured providers into a native AgentSession."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import aiohttp
from livekit.agents import AgentSession, TurnHandlingOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import silero

from opentalk.asr.provider import create_stt, load_asr_config
from opentalk.config import load_config
from opentalk.domain.booking_service import BookingService
from opentalk.llm.provider import create_llm, load_llm_config, reasoning_config
from opentalk.storage.repository import BookingRepository
from opentalk.tools.database_tools import DatabaseTools
from opentalk.tts.provider import create_tts, load_tts_config
from opentalk.voice.agent import BookingAgent
from opentalk.voice.config import load_voice_config
from opentalk.voice.journal import SessionJournal


def create_vad(config=None):
    config = config or load_voice_config()
    return silero.VAD.load(force_cpu=True, sample_rate=16000, **config.vad_options)


@asynccontextmanager
async def open_session(*, text_only=False, backend_config=None, voice_config=None,
                       service=None, model=None, reasoning_model=None, vad=None, stt_model=None, tts_model=None):
    voice_config = voice_config or load_voice_config()
    backend_config = backend_config or load_config()
    if service is None:
        def build_service():
            return BookingService(BookingRepository(backend_config.database_path),
                                  user_id=backend_config.demo_user_id, session_id=uuid4().hex,
                                  timezone=backend_config.timezone)
        service = await asyncio.to_thread(build_service)
    journal = SessionJournal(service.session_id, voice_config.log_directory / f"{service.session_id}.json")
    llm_config = load_llm_config()
    strong_config = reasoning_config(llm_config)
    injected_model = model is not None
    async with AsyncExitStack() as resources:
        model = await resources.enter_async_context(model or create_llm(llm_config))
        if reasoning_model is not None:
            reasoning_model = await resources.enter_async_context(reasoning_model)
        elif strong_config is not None and not injected_model:
            reasoning_model = await resources.enter_async_context(create_llm(strong_config))
        agent = BookingAgent(DatabaseTools(service, backend_config.rooms), journal,
                             reasoning_model=reasoning_model,
                             reasoning_options=(strong_config or llm_config).connection_options)
        options = {"llm": model, "max_tool_steps": voice_config.max_tool_steps}
        if text_only:
            options["turn_handling"] = TurnHandlingOptions(turn_detection="manual", preemptive_generation={"enabled": False})
            options["conn_options"] = SessionConnectOptions(llm_conn_options=llm_config.connection_options)
        else:
            asr_config, tts_config = load_asr_config(), load_tts_config()
            http_session = await resources.enter_async_context(aiohttp.ClientSession())
            recognizer = await resources.enter_async_context(stt_model or create_stt(asr_config, http_session=http_session))
            synthesizer = await resources.enter_async_context(tts_model or create_tts(tts_config, http_session=http_session))
            options.update(
                stt=recognizer, tts=synthesizer,
                vad=vad or await asyncio.to_thread(create_vad, voice_config),
                turn_handling=TurnHandlingOptions(
                    turn_detection="stt",
                    endpointing={"min_delay": voice_config.endpoint_min_delay, "max_delay": voice_config.endpoint_max_delay},
                    interruption={"enabled": True, "mode": "vad", "min_duration": voice_config.min_interruption_duration,
                                  "min_words": 0, "resume_false_interruption": False},
                    preemptive_generation={"enabled": False},
                ),
                conn_options=SessionConnectOptions(
                    llm_conn_options=llm_config.connection_options,
                    stt_conn_options=asr_config.connection_options,
                    tts_conn_options=tts_config.connection_options,
                ),
            )
        session = AgentSession(**options)
        if text_only:
            session.output.set_audio_enabled(False)
        journal.attach(session)
        try:
            yield session, agent, journal
        finally:
            try:
                await session.aclose()
            finally:
                await journal.save(service)
