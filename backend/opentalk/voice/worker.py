"""Run a LiveKit room worker or the SDK's local microphone console."""

import asyncio
import json
import os

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, JobProcess, cli

from opentalk.config import PROJECT_ROOT
from opentalk.sessions.config import load_session_config
from opentalk.sessions.store import SessionStore
from opentalk.voice.factories import agent_factory
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
    session_config = load_session_config()
    store = await asyncio.to_thread(SessionStore, session_config.database_path,
                                    lease_seconds=session_config.lease_seconds,
                                    pending_seconds=session_config.token_ttl_seconds)
    reservation = None
    agent_key = "booking"
    if context.job.metadata:
        metadata = json.loads(context.job.metadata)
        reservation = await asyncio.to_thread(store.get, metadata["session_id"])
        if (reservation["attempt_id"] != metadata["attempt_id"]
                or reservation["room_name"] != context.room.name
                or reservation["mode"] != "room"):
            raise ValueError("The job does not match the allocated session room and attempt.")
        agent_key = reservation["agent_key"]
    async with open_session(voice_config=configuration, session_config=session_config,
                            store=store, reservation=reservation, agent_key=agent_key,
                            factory=agent_factory(agent_key), vad=context.proc.userdata.get("vad")) as (session, agent, journal):
        await session.start(agent=agent, room=context.room)
        await context.connect()
        if not reservation or not reservation["userdata"]:
            session.say("Hello. How can I help you?")
        # Keep resource ownership until the job is explicitly shut down.
        finished = asyncio.Event()

        async def shutdown():
            journal.failed, journal.error_code = True, "job_shutdown"
            finished.set()
        context.add_shutdown_callback(shutdown)
        session.on("close", lambda event: finished.set())

        await finished.wait()
    saved = await asyncio.to_thread(store.get, session.userdata.session_id)
    if saved["end_requested"] and saved["status"] == "completed":
        from livekit import api
        await context.api.room.delete_room(api.DeleteRoomRequest(room=saved["room_name"]))


if __name__ == "__main__":
    cli.run_app(server)
