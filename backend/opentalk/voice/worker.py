"""Run a LiveKit room worker or the SDK's local microphone console."""

import asyncio
import os

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, JobProcess, cli

from opentalk.config import PROJECT_ROOT
from opentalk.voice.config import load_voice_config
from opentalk.voice.session import create_vad, open_session


load_dotenv(PROJECT_ROOT / ".env.local", override=False)
configuration = load_voice_config()
server = AgentServer(ws_url=os.environ.get("LIVEKIT_URL", configuration.livekit_url))


def prewarm(process: JobProcess):
    process.userdata["vad"] = create_vad(configuration)


server.setup_fnc = prewarm


@server.rtc_session(agent_name=configuration.agent_name)
async def entrypoint(context: JobContext):
    async with open_session(voice_config=configuration, vad=context.proc.userdata.get("vad")) as (session, agent, journal):
        await session.start(agent=agent, room=context.room)
        await context.connect()
        session.say("Hello. I can help you check, reserve, and cancel meeting rooms. What date and room do you need?")
        # Keep resource ownership until the job is explicitly shut down.
        finished = asyncio.Event()

        async def shutdown():
            finished.set()
        context.add_shutdown_callback(shutdown)
        session.on("close", lambda event: finished.set())

        async def persist():
            while not finished.is_set():
                await journal.save(agent.booking_tools.service)
                try:
                    await asyncio.wait_for(finished.wait(), timeout=configuration.log_flush_interval_seconds)
                except TimeoutError:
                    continue
        persistence = asyncio.create_task(persist())
        shutdown_waiter = asyncio.create_task(finished.wait())
        try:
            done, _ = await asyncio.wait([persistence, shutdown_waiter], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            persistence.cancel()
            shutdown_waiter.cancel()
            await asyncio.gather(persistence, shutdown_waiter, return_exceptions=True)


if __name__ == "__main__":
    cli.run_app(server)
