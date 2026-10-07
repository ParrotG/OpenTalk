"""Exercise the production worker and dispatch lifecycle with offline model doubles."""

import os
import sys
from pathlib import Path

from livekit.agents import AgentServer, JobContext, cli

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_fakes import FakeSTT, FakeTTS, ScriptedLLM
from opentalk.voice import worker
from opentalk.voice.session import open_session


def offline_session(**options):
    model = ScriptedLLM({"List resources": ("query", {"sql": "SELECT name, capacity FROM resources ORDER BY name"})})
    return open_session(**options, model=model, stt_model=FakeSTT(), tts_model=FakeTTS())


server = AgentServer(ws_url=os.environ["LIVEKIT_URL"], host="0.0.0.0", port=8081,
                     num_idle_processes=1)
server.setup_fnc = worker.prewarm


@server.rtc_session(agent_name=worker.configuration.agent_name)
async def entrypoint(context: JobContext):
    # Apply the doubles inside the job process; production code has no test-provider switch.
    worker.open_session = offline_session
    await worker.entrypoint(context)


if __name__ == "__main__":
    cli.run_app(server)
