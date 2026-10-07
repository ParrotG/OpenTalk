"""An isolated LiveKit peer for browser protocol tests, without model calls or databases."""

import asyncio
import signal
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web
from livekit import api, rtc

URL = "ws://127.0.0.1:18180"


def token(room: str, identity: str, kind: str = "standard") -> str:
    return (
        api.AccessToken("devkey", "secret")
        .with_identity(identity)
        .with_kind(kind)
        .with_attributes({"lk.agent.state": "listening"} if kind == "agent" else {})
        .with_grants(api.VideoGrants(
            room_join=True, room=room, agent=kind == "agent", can_update_own_metadata=True
        ))
        .to_jwt()
    )


async def main() -> None:
    sessions: dict[str, dict] = {}
    peers: dict[str, rtc.Room] = {}
    histories: dict[str, list] = {}
    received: dict[str, list[str]] = {}
    tasks: set[asyncio.Task] = set()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    with tempfile.TemporaryDirectory(prefix="opentalk-ui-rtc-") as directory:
        config = Path(directory) / "livekit.yaml"
        config.write_text(
            "development: true\nport: 18180\nbind_addresses: [127.0.0.1]\n"
            "rtc:\n  node_ip: 127.0.0.1\n  use_external_ip: false\n"
            "  enable_loopback_candidate: true\n  tcp_port: 18181\n  udp_port: 18182\n"
            "  ips:\n    includes: [127.0.0.1/32]\n",
            encoding="utf-8",
        )
        server = await asyncio.create_subprocess_exec(
            "livekit-server", "--config", str(config),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        client = api.LiveKitAPI(URL, "devkey", "secret")
        runner = None
        try:
            for _ in range(100):
                if server.returncode is not None:
                    raise RuntimeError("The isolated LiveKit server exited before becoming ready.")
                try:
                    await client.room.list_rooms(api.ListRoomsRequest())
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("The isolated LiveKit server did not become ready.")

            async def handle(request: web.Request) -> web.Response:
                path = request.path
                if path == "/health/services":
                    return web.json_response({"status": "ok", "services": {
                        name: {"status": "ok"} for name in ("sessions", "livekit", "worker")
                    }})
                if path == "/sessions" and request.method == "POST":
                    data = await request.json()
                    ident = data.get("resume_session_id") or f"rtc-{uuid.uuid4()}"
                    room_name = f"ui-test-{uuid.uuid4()}"
                    saved = {"session_id": ident, "attempt_id": str(uuid.uuid4()),
                             "status": "active", "agent_key": "conversation",
                             "created_at": time.time(), "updated_at": time.time(),
                             "ended_at": None, "end_requested": False}
                    sessions[ident] = saved
                    histories.setdefault(ident, [])
                    received.setdefault(ident, [])
                    peer = rtc.Room()
                    if ident in peers:
                        await peers[ident].disconnect()
                    peers[ident] = peer

                    async def read(reader: rtc.TextStreamReader) -> None:
                        text = await reader.read_all()
                        received[ident].append(text)
                        histories[ident].append({"id": reader.info.stream_id, "type": "message",
                                                 "role": "user", "content": [text]})

                    def on_text(reader: rtc.TextStreamReader, identity: str) -> None:
                        task = asyncio.create_task(read(reader))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)

                    peer.register_text_stream_handler("lk.chat", on_text)
                    await peer.connect(URL, token(room_name, "test-agent", "agent"))
                    source = rtc.AudioSource(48000, 1)
                    track = rtc.LocalAudioTrack.create_audio_track("test-silence", source)
                    await peer.local_participant.publish_track(
                        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
                    )

                    async def silence() -> None:
                        try:
                            while peer.isconnected():
                                await source.capture_frame(rtc.AudioFrame.create(48000, 1, 960))
                        finally:
                            await source.aclose()

                    task = asyncio.create_task(silence())
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    await peer.local_participant.set_attributes({"lk.agent.state": "listening"})
                    return web.json_response({"session": saved, "serverUrl": URL,
                                              "roomName": room_name, "participantName": "User",
                                              "participantToken": token(room_name, "User")})
                if path == "/sessions":
                    return web.json_response(list(reversed(list(sessions.values()))))
                parts = path.strip("/").split("/")
                ident = parts[1] if len(parts) > 1 else ""
                if ident not in sessions:
                    return web.json_response({"error": "Unknown test session."}, status=404)
                peer = peers[ident]
                if path.endswith("/history"):
                    return web.json_response({"items": histories[ident]})
                if path.endswith("/probe"):
                    tracks = [publication.muted for participant in peer.remote_participants.values()
                              for publication in participant.track_publications.values()]
                    return web.json_response({"received": received[ident], "microphones": tracks})
                if path.endswith("/reply"):
                    text = (await request.json())["text"]
                    stream = await peer.local_participant.send_text(text, topic="lk.chat")
                    histories[ident].append({"id": stream.stream_id, "type": "message",
                                             "role": "assistant", "content": [text]})
                    return web.json_response({"ok": True})
                if path.endswith("/end"):
                    sessions[ident].update(status="completed", ended_at=time.time(), end_requested=True)
                    return web.json_response({"session": sessions[ident]})
                return web.json_response({"session": sessions[ident]})

            app = web.Application()
            app.router.add_route("*", "/{path:.*}", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 18183).start()
            print("RTC fixture ready", flush=True)
            await stop.wait()
        finally:
            if runner:
                await runner.cleanup()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for peer in peers.values():
                await peer.disconnect()
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
