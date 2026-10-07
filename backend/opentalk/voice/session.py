"""Compose independently configured providers into a native AgentSession."""

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import aiohttp
from livekit.agents import AgentSession, TurnHandlingOptions, llm
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import silero

from opentalk.asr.provider import create_stt, load_asr_config
from opentalk.llm.provider import create_llm, load_llm_config, reasoning_config
from opentalk.sessions.config import load_session_config
from opentalk.sessions.state import SessionState
from opentalk.sessions.store import SessionError, SessionStore
from opentalk.sessions.telemetry import TelemetryStore
from opentalk.sessions.tracing import session_trace
from opentalk.tts.provider import create_tts, load_tts_config
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory
from opentalk.voice.journal import SessionJournal


def create_vad(config=None):
    config = config or load_voice_config()
    return silero.VAD.load(force_cpu=True, sample_rate=16000, **config.vad_options)


@asynccontextmanager
async def open_session(*, text_only=False, voice_config=None, session_config=None,
                       store=None, reservation=None, resume_id=None, factory=None, agent_key="booking",
                       model=None, reasoning_model=None, vad=None, stt_model=None, tts_model=None):
    voice_config = voice_config or load_voice_config()
    session_config = session_config or load_session_config()
    store = store or await asyncio.to_thread(SessionStore, session_config.database_path,
                                             lease_seconds=session_config.lease_seconds,
                                             pending_seconds=session_config.token_ttl_seconds)
    if reservation is None:
        reservation = await asyncio.to_thread(store.reserve, request_id=uuid4().hex,
                                               mode="text" if text_only else "room",
                                               agent_key=agent_key, resume_id=resume_id)
    elif reservation["agent_key"] != agent_key:
        raise ValueError("The reservation agent type does not match the requested agent.")
    owner = uuid4().hex
    reservation = await asyncio.to_thread(store.claim, reservation["session_id"], reservation["attempt_id"], owner)
    state = SessionState(reservation["session_id"], reservation["attempt_id"],
                         reservation["userdata"].get("preferred_response_language"))
    try:
        async with session_trace(session_config, state):
            async with _run_session(text_only=text_only, voice_config=voice_config, session_config=session_config,
                                    store=store, state=state, owner=owner, factory=factory or agent_factory(agent_key),
                                    model=model, reasoning_model=reasoning_model, vad=vad,
                                    stt_model=stt_model, tts_model=tts_model) as result:
                yield result
    except BaseException as error:
        await asyncio.shield(asyncio.to_thread(store.fail, state.session_id, state.attempt_id,
                                               type(error).__name__))
        raise


@asynccontextmanager
async def _run_session(*, text_only, voice_config, session_config, store, state, owner,
                       factory, model, reasoning_model, vad, stt_model, tts_model):
    telemetry = await asyncio.to_thread(TelemetryStore, session_config.telemetry_database_path,
                                        max_events=session_config.telemetry_max_events,
                                        retention_days=session_config.telemetry_retention_days)
    journal = SessionJournal(store, telemetry, state, owner,
                             pending_event_limit=session_config.pending_event_limit)
    history = await asyncio.to_thread(store.history_tail, state.session_id,
                                     max_items=session_config.context_max_items + 1)
    context = llm.ChatContext.from_dict(history).copy(exclude_instructions=True,
                                                     exclude_config_update=True, exclude_handoff=True)
    # A bounded archive read can start inside a tool exchange.
    while context.items and context.items[0].type in {"function_call", "function_call_output"}:
        context.items.pop(0)
    context.truncate(max_items=session_config.context_max_items)
    llm_config = load_llm_config()
    strong_config = reasoning_config(llm_config)
    injected_model = model is not None
    async with AsyncExitStack() as resources:
        model = await resources.enter_async_context(model or create_llm(llm_config))
        if reasoning_model is not None:
            reasoning_model = await resources.enter_async_context(reasoning_model)
        elif strong_config is not None and not injected_model:
            reasoning_model = await resources.enter_async_context(create_llm(strong_config))
        agent = await factory(journal=journal, state=state, chat_ctx=context,
                              context_max_items=session_config.context_max_items,
                              reasoning_model=reasoning_model,
                              reasoning_options=(strong_config or llm_config).connection_options)
        options = {"llm": model, "max_tool_steps": voice_config.max_tool_steps, "userdata": state}
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
        if reasoning_model is not None:
            reasoning_model.on("metrics_collected", lambda metrics: journal.record(
                "reasoning_metrics", data=metrics.model_dump(mode="json")))
        persistence_error = None
        started = time.monotonic()

        async def persist():
            nonlocal persistence_error
            try:
                while True:
                    await asyncio.sleep(session_config.heartbeat_seconds)
                    if time.monotonic() - started >= session_config.max_session_seconds:
                        raise SessionError("session_limit", "The session reached its configured duration limit.")
                    end_requested = await asyncio.to_thread(store.heartbeat, state.session_id, state.attempt_id, owner)
                    if end_requested:
                        await session.aclose()
                        return
                    await journal.save()
            except Exception as error:
                persistence_error = error
                journal.failed, journal.error_code = True, "persistence_failed"
                await session.aclose()

        persistence = asyncio.create_task(persist())
        try:
            yield session, agent, journal
            if persistence_error is not None:
                raise persistence_error
        except BaseException as error:
            journal.failed, journal.error_code = True, type(error).__name__
            raise
        finally:
            persistence.cancel()
            await asyncio.gather(persistence, return_exceptions=True)
            try:
                await session.aclose()
            except BaseException:
                journal.failed, journal.error_code = True, "shutdown_failed"
                raise
            finally:
                await journal.save()
    # Completion is visible only after every provider has closed successfully.
    await journal.save(status="failed" if journal.failed else "completed", error_code=journal.error_code)
