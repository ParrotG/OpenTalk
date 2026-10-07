"""Run the real control API and native RoomIO with deterministic providers in isolation."""

import asyncio
import json
import os
import signal
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from aiohttp import web
from livekit import api, rtc
from livekit.agents import room_io

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]
from opentalk.sessions.api import create_app
from opentalk.sessions.config import load_session_config
from opentalk.sessions.store import SessionStore
from opentalk.voice.room_control import RoomAudioMode, close_disconnected_session
from opentalk.voice.session import open_session
from voice_fakes import FakeSTT, FakeTTS, ScriptedLLM

URL = "ws://127.0.0.1:18280"


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    os.environ["LIVEKIT_URL"] = URL
    os.environ["LIVEKIT_PUBLIC_URL"] = URL
    tasks = set()
    counts = {}
    with tempfile.TemporaryDirectory(prefix="opentalk-native-ui-") as directory:
        base = Path(directory)
        config = base / "livekit.yaml"
        config.write_text(
            "development: true\nport: 18280\nbind_addresses: [127.0.0.1]\n"
            "rtc:\n  node_ip: 127.0.0.1\n  use_external_ip: false\n"
            "  enable_loopback_candidate: true\n  tcp_port: 18281\n  udp_port: 18282\n"
            "  ips:\n    includes: [127.0.0.1/32]\n", encoding="utf-8")
        server = await asyncio.create_subprocess_exec(
            "livekit-server", "--config", str(config),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        settings = replace(load_session_config(), database_path=base / "sessions.sqlite3",
                           telemetry_database_path=base / "telemetry.sqlite3",
                           heartbeat_seconds=0.2, public_livekit_url=URL)
        store = SessionStore(settings.database_path)
        client = api.LiveKitAPI(URL, "devkey", "secret")
        runner = None
        try:
            for _ in range(100):
                try:
                    await client.room.list_rooms(api.ListRoomsRequest())
                    break
                except Exception:
                    if server.returncode is not None:
                        raise RuntimeError("The isolated LiveKit server exited.")
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("The isolated LiveKit server did not become ready.")

            async def agent_session(allocation, voice_enabled):
                saved = allocation["session"]
                peer = rtc.Room()
                token = (api.AccessToken("devkey", "secret").with_identity("native-test-agent")
                         .with_kind("agent").with_grants(api.VideoGrants(
                             agent=True, room_join=True, room=saved["room_name"], can_update_own_metadata=True))
                         .to_jwt())
                model = ScriptedLLM()
                class CountingTTS(FakeTTS):
                    def stream(self, **kwargs):
                        counts[saved["session_id"]] += 1
                        return super().stream(**kwargs)
                counts[saved["session_id"]] = 0
                disconnects = set()
                try:
                    await peer.connect(URL, token)
                    async with open_session(session_config=settings, store=store, reservation=saved,
                                            agent_key="conversation", model=model,
                                            stt_model=FakeSTT(), tts_model=CountingTTS()) as (session, agent, journal):
                        mode = agent.audio_mode = RoomAudioMode(session, enabled=voice_enabled)
                        identity = f"user-{saved['attempt_id']}"
                        mode.register(peer, identity, wait_for_ready=True)
                        closed = asyncio.Event()
                        session.on("close", lambda event: closed.set())
                        def disconnected(participant):
                            if participant.identity == identity:
                                task = asyncio.create_task(close_disconnected_session(session, journal, peer, identity))
                                disconnects.add(task)
                                task.add_done_callback(disconnects.discard)
                        peer.on("participant_disconnected", disconnected)
                        await session.start(agent=agent, room=peer, session_host=False,
                            room_options=room_io.RoomOptions(close_on_disconnect=False,
                                text_output=room_io.TextOutputOptions(sync_transcription=False)))
                        mode.configure_outputs()
                        try:
                            await closed.wait()
                        finally:
                            peer.off("participant_disconnected", disconnected)
                            for task in disconnects:
                                task.cancel()
                            await asyncio.gather(*disconnects, return_exceptions=True)
                            await mode.aclose()
                finally:
                    await peer.disconnect()

            @web.middleware
            async def fixture(request, handler):
                if request.path == "/health/services":
                    return web.json_response({"status": "ok", "services": {"sessions": {"status": "ok"}}})
                if request.path == "/probe":
                    return web.json_response(counts)
                response = await handler(request)
                if request.path == "/sessions" and request.method == "POST" and response.status == 200:
                    body = await request.json()
                    allocation = json.loads(response.text)
                    task = asyncio.create_task(agent_session(allocation, body.get("voice_enabled", True)))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                return response

            app = create_app(configuration=settings, store=store, livekit=client,
                             api_key="devkey", api_secret="secret")
            app.middlewares.insert(0, fixture)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 18283).start()
            print("Native fixture ready", flush=True)
            await stop.wait()
        finally:
            if runner:
                await runner.cleanup()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            if server.returncode is None:
                server.terminate()
            try:
                await asyncio.wait_for(server.wait(), timeout=5)
            except asyncio.TimeoutError:
                server.kill()
                await server.wait()


if __name__ == "__main__":
    asyncio.run(main())
