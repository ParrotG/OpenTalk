"""Local demo control API for session allocation, connection tokens, and termination."""

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

from aiohttp import ClientTimeout, web
from dotenv import load_dotenv
from livekit import api
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration

from opentalk.config import PROJECT_ROOT
from opentalk.sessions.config import load_session_config
from opentalk.sessions.store import SessionError, SessionStore
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except SessionError as error:
        return web.json_response({"error": error.code, "message": str(error)},
                                 status=404 if error.code == "session_not_found" else 409)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request", "message": "Invalid request parameters."}, status=400)


def create_app(*, configuration=None, store=None, livekit=None, api_key=None, api_secret=None):
    configuration = configuration or load_session_config()
    store = store or SessionStore(configuration.database_path, lease_seconds=configuration.lease_seconds,
                                  pending_seconds=configuration.token_ttl_seconds)
    voice_config = load_voice_config()
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)
    key = api_key or os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = api_secret or os.environ.get("LIVEKIT_API_SECRET", "").strip()
    public_url = os.environ.get("LIVEKIT_PUBLIC_URL", configuration.public_livekit_url)
    internal_url = os.environ.get("LIVEKIT_URL", voice_config.livekit_url)
    app = web.Application(middlewares=[errors], client_max_size=16384)

    async def health(request):
        await asyncio.to_thread(store.list, limit=1)
        return web.json_response({"status": "ok"})

    async def create(request):
        if not key or not secret:
            return web.json_response({"error": "missing_credentials", "message": "Configure LiveKit API credentials."}, status=503)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("An object is required.")
        agent_key = body.get("agent", "booking")
        agent_factory(agent_key)
        reservation = await asyncio.to_thread(store.reserve, request_id=body["request_id"], mode="room",
                                               agent_key=agent_key, resume_id=body.get("resume_session_id"))
        if reservation["status"] not in {"pending", "active"} or reservation["end_requested"]:
            raise SessionError("session_closed", "Create a new request or resume a completed session.")
        metadata = json.dumps({"session_id": reservation["session_id"], "attempt_id": reservation["attempt_id"],
                               "agent_key": reservation["agent_key"]})
        room_config = RoomConfiguration(name=reservation["room_name"], max_participants=2,
                                        metadata=metadata, agents=[RoomAgentDispatch(
                                            agent_name=voice_config.agent_name, metadata=metadata)])
        token = (api.AccessToken(key, secret)
                 .with_identity(f"user-{reservation['attempt_id']}").with_name("User")
                 .with_ttl(timedelta(seconds=configuration.token_ttl_seconds))
                 .with_grants(api.VideoGrants(room_join=True, room=reservation["room_name"],
                                             can_publish=True, can_subscribe=True, can_publish_data=True))
                 .with_room_config(room_config).to_jwt())
        return web.json_response({"session": reservation, "serverUrl": public_url,
                                  "roomName": reservation["room_name"], "participantName": "User",
                                  "participantToken": token}, headers={"Cache-Control": "no-store"})

    async def listing(request):
        return web.json_response(await asyncio.to_thread(store.list,
            limit=int(request.query.get("limit", 20)), offset=int(request.query.get("offset", 0))))

    async def show(request):
        session_id = request.match_info["session_id"]
        return web.json_response({"session": await asyncio.to_thread(store.get, session_id),
                                 "attempts": await asyncio.to_thread(store.attempts, session_id)})

    async def history(request):
        return web.json_response(await asyncio.to_thread(store.history, request.match_info["session_id"],
            limit=int(request.query.get("limit", 100)), offset=int(request.query.get("offset", 0))))

    async def delete_room(room_name):
        if not room_name:
            return
        if livekit is not None:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room_name))
        else:
            async with api.LiveKitAPI(url=internal_url, api_key=key, api_secret=secret,
                                     timeout=ClientTimeout(total=5)) as client:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))

    async def end(request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("attempt_id"), str):
            raise ValueError("An attempt ID is required.")
        session_id = request.match_info["session_id"]
        result = await asyncio.to_thread(store.request_end, session_id, attempt_id=body["attempt_id"])
        # The owner checkpoints and closes before the room is removed. Never claim success early.
        if result["mode"] == "room" and result["status"] in {"completed", "failed"}:
            try:
                await delete_room(result["room_name"])
            except api.TwirpError as error:
                if error.code != "not_found":
                    return web.json_response({"session": result, "error": "room_cleanup_failed"}, status=502)
            except Exception:
                return web.json_response({"session": result, "error": "room_cleanup_failed"}, status=502)
        return web.json_response(result, status=202 if result["status"] == "active" else 200)

    app.add_routes([web.get("/health", health), web.post("/sessions", create),
                    web.get("/sessions", listing), web.get("/sessions/{session_id}", show),
                    web.get("/sessions/{session_id}/history", history),
                    web.post("/sessions/{session_id}/end", end)])
    return app


def main():
    parser = argparse.ArgumentParser(description="Run the local demo session control API.")
    parser.add_argument("--config", type=Path, help="Session configuration path.")
    args = parser.parse_args()
    configuration = load_session_config(args.config)
    web.run_app(create_app(configuration=configuration), host=configuration.api_host, port=configuration.api_port)


if __name__ == "__main__":
    main()
