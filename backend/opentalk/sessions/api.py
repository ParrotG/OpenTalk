"""Local demo control API for session allocation, connection tokens, and termination."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, web
from dotenv import load_dotenv
from livekit import api
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration

from opentalk.asr.provider import load_asr_config
from opentalk.config import PROJECT_ROOT
from opentalk.documents.config import DocumentConfig, load_document_config
from opentalk.documents.library import DocumentError, DocumentManager, DocumentProcessManager
from opentalk.llm.provider import load_llm_config
from opentalk.sessions.config import load_session_config
from opentalk.sessions.store import SessionError, SessionStore
from opentalk.sessions.telemetry import TelemetryStore
from opentalk.tts.provider import load_tts_config
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory

logger = logging.getLogger(__name__)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPRequestEntityTooLarge:
        return web.json_response(
            {"error": "request_too_large", "message": "The request is too large."},
            status=413,
        )
    except SessionError as error:
        return web.json_response({"error": error.code, "message": str(error)},
                                 status=404 if error.code == "session_not_found" else 409)
    except DocumentError as error:
        status = {
            "invalid_document_id": 400,
            "document_not_found": 404,
            "document_busy": 409,
            "document_unavailable": 503,
            "document_delete_failed": 503,
        }.get(error.code, 409)
        return web.json_response({"error": error.code, "message": str(error)}, status=status)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request", "message": "Invalid request parameters."}, status=400)


async def _json_body(request: web.Request) -> object:
    """Keep JSON control requests bounded while the upload route accepts larger bodies."""
    maximum = 16384
    if request.content_length is not None and request.content_length > maximum:
        raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=request.content_length)
    chunks = []
    size = 0
    while chunk := await request.content.read(min(8192, maximum + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=size)
    return json.loads(b"".join(chunks))


def _upload_name(value: str) -> str:
    """Preserve readable Unicode names while removing path and control characters."""
    name = Path(unquote(value).replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(". ")
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    return name or "document.pdf"


def create_app(
    *,
    configuration=None,
    store=None,
    livekit=None,
    api_key=None,
    api_secret=None,
    document_config: DocumentConfig | None = None,
    document_manager: DocumentManager | None = None,
    telemetry_store: TelemetryStore | None = None,
):
    configuration = configuration or load_session_config()
    store = store or SessionStore(configuration.database_path, lease_seconds=configuration.lease_seconds,
                                  pending_seconds=configuration.token_ttl_seconds)
    voice_config = load_voice_config()
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)
    key = api_key or os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = api_secret or os.environ.get("LIVEKIT_API_SECRET", "").strip()
    public_url = os.environ.get("LIVEKIT_PUBLIC_URL", configuration.public_livekit_url)
    internal_url = os.environ.get("LIVEKIT_URL", voice_config.livekit_url)
    document_config = document_config or load_document_config()
    document_manager = document_manager or DocumentProcessManager(document_config)
    telemetry_store = telemetry_store or TelemetryStore(
        configuration.telemetry_database_path,
        max_events=configuration.telemetry_max_events,
        retention_days=configuration.telemetry_retention_days,
    )
    app = web.Application(
        middlewares=[errors],
        client_max_size=document_config.max_file_size_bytes + 1024 * 1024,
    )

    async def health(request):
        await asyncio.to_thread(store.list, limit=1)
        return web.json_response({"status": "ok"})

    async def services(request):
        # These checks do not invoke paid providers or allocate a room.
        async def database_check():
            await asyncio.to_thread(store.list, limit=1)

        async def livekit_check():
            if not key or not secret:
                raise ValueError("Missing LiveKit credentials.")
            if livekit is not None:
                await livekit.room.list_rooms(api.ListRoomsRequest())
            else:
                async with api.LiveKitAPI(url=internal_url, api_key=key, api_secret=secret,
                                         timeout=ClientTimeout(total=3)) as client:
                    await client.room.list_rooms(api.ListRoomsRequest())

        async def worker_check():
            worker_url = os.environ.get("OPENTALK_WORKER_HEALTH_URL", "http://127.0.0.1:8081").rstrip("/")
            async with ClientSession(timeout=ClientTimeout(total=3)) as client:
                async with client.get(worker_url) as response:
                    response.raise_for_status()
                async with client.get(worker_url + "/worker") as response:
                    response.raise_for_status()
                    information = await response.json()
                    if information.get("agent_name") != voice_config.agent_name:
                        raise ValueError("The worker agent name does not match.")

        names = ("sessions", "livekit", "worker")
        results = await asyncio.gather(*(asyncio.wait_for(check(), timeout=4)
            for check in (database_check, livekit_check, worker_check)), return_exceptions=True)
        checks = {name: {"status": "unavailable" if isinstance(result, BaseException) else "ok"}
                  for name, result in zip(names, results)}
        def provider_configuration():
            credentials = (load_llm_config().api_key_env, load_asr_config().api_key_env,
                           load_tts_config().api_key_env)
            return all(os.environ.get(name, "").strip() for name in credentials)
        try:
            configured = await asyncio.to_thread(provider_configuration)
            checks["providers"] = {"status": "configured" if configured else "missing_configuration"}
        except ValueError:
            checks["providers"] = {"status": "invalid_configuration"}
        ready = all(checks[name]["status"] == "ok" for name in names) and checks["providers"]["status"] == "configured"
        return web.json_response({"status": "ok" if ready else "degraded", "services": checks},
                                 headers={"Cache-Control": "no-store"})

    async def create(request):
        if not key or not secret:
            return web.json_response({"error": "missing_credentials", "message": "Configure LiveKit API credentials."}, status=503)
        body = await _json_body(request)
        if not isinstance(body, dict):
            raise ValueError("An object is required.")
        agent_key = body.get("agent", "conversation")
        voice_enabled = body.get("voice_enabled", True)
        if type(voice_enabled) is not bool:
            raise ValueError("voice_enabled must be a boolean.")
        agent_factory(agent_key)
        reservation = await asyncio.to_thread(store.reserve, request_id=body["request_id"], mode="room",
                                               agent_key=agent_key, resume_id=body.get("resume_session_id"))
        if reservation["status"] not in {"pending", "active"} or reservation["end_requested"]:
            raise SessionError("session_closed", "Create a new request or resume a completed session.")
        metadata = json.dumps({"session_id": reservation["session_id"], "attempt_id": reservation["attempt_id"],
                               "agent_key": reservation["agent_key"], "voice_enabled": voice_enabled})
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
        if request.query.get("tail") == "true":
            limit = int(request.query.get("limit", 200))
            if not 1 <= limit <= 1000:
                raise ValueError("Invalid history limit.")
            return web.json_response(await asyncio.to_thread(store.history_tail,
                request.match_info["session_id"], max_items=limit))
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
        body = await _json_body(request)
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

    async def delete_session(request):
        session_id = request.match_info["session_id"]
        saved = await asyncio.to_thread(store.get, session_id)
        if saved["status"] in {"pending", "active"}:
            raise SessionError("session_busy", "End the active session before deleting it.")

        def delete_persisted_session():
            telemetry_store.delete_session(session_id)
            return store.delete(session_id)

        deleted = await asyncio.to_thread(delete_persisted_session)
        return web.json_response({"deleted": True, "session_id": deleted["session_id"]})

    async def documents(request):
        return web.json_response(
            {
                "items": await document_manager.documents(),
                "max_file_size_bytes": document_config.max_file_size_bytes,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def upload_document(request):
        staging_root = document_config.storage_root / ".uploads"
        try:
            await asyncio.to_thread(staging_root.mkdir, parents=True, exist_ok=True)
        except OSError:
            logger.exception("The document upload staging directory could not be created.")
            return web.json_response(
                {
                    "error": "document_storage_unavailable",
                    "message": "Document storage is unavailable. Check the backend data directory.",
                },
                status=503,
            )
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError):
            return web.json_response(
                {"error": "invalid_upload", "message": "A multipart PDF upload is required."},
                status=400,
            )
        field = await reader.next()
        if field is None or field.name != "file" or not field.filename:
            return web.json_response(
                {"error": "invalid_upload", "message": "A PDF file field is required."},
                status=400,
            )
        source_name = _upload_name(field.filename)
        upload_path = staging_root / uuid4().hex / source_name
        try:
            await asyncio.to_thread(upload_path.parent.mkdir, parents=True, exist_ok=False)
        except OSError:
            logger.exception("The document upload staging directory could not be created.")
            return web.json_response(
                {
                    "error": "document_storage_unavailable",
                    "message": "Document storage is unavailable. Check the backend data directory.",
                },
                status=503,
            )
        size = 0
        too_large = False
        signature = bytearray()
        digest = hashlib.sha256()
        try:
            stream = await asyncio.to_thread(upload_path.open, "wb")
        except OSError:
            await asyncio.to_thread(shutil.rmtree, upload_path.parent, True)
            logger.exception("The uploaded document could not be opened for writing.")
            return web.json_response(
                {
                    "error": "document_storage_unavailable",
                    "message": "Document storage is unavailable. Check the backend data directory.",
                },
                status=503,
            )
        try:
            while chunk := await field.read_chunk(1024 * 1024):
                size += len(chunk)
                if size > document_config.max_file_size_bytes:
                    too_large = True
                    break
                if len(signature) < 5:
                    signature.extend(chunk[: 5 - len(signature)])
                digest.update(chunk)
                await asyncio.to_thread(stream.write, chunk)
        finally:
            await asyncio.to_thread(stream.close)
        if too_large:
            await asyncio.to_thread(shutil.rmtree, upload_path.parent, True)
            return web.json_response(
                {
                    "error": "file_too_large",
                    "message": "The PDF exceeds the configured file size limit.",
                },
                status=413,
            )
        extra = await reader.next()
        if extra is not None:
            await asyncio.to_thread(shutil.rmtree, upload_path.parent, True)
            return web.json_response(
                {"error": "invalid_upload", "message": "Upload exactly one PDF file."},
                status=400,
            )
        if size == 0 or bytes(signature) != b"%PDF-":
            await asyncio.to_thread(shutil.rmtree, upload_path.parent, True)
            message = "The PDF input is empty." if size == 0 else "The file is not a valid PDF."
            return web.json_response(
                {"error": "invalid_pdf", "message": message},
                status=400,
            )
        document_id = digest.hexdigest()
        accepted, job_id = await document_manager.enqueue(
            document_id=document_id,
            name=source_name,
            size_bytes=size,
            staged_path=upload_path,
        )
        items = await document_manager.documents()
        document = next(item for item in items if item["document_id"] == document_id)
        return web.json_response(
            {"accepted": accepted, "job_id": job_id or None, "document": document},
            status=202 if accepted else 200,
            headers={"Cache-Control": "no-store"},
        )

    async def delete_document(request):
        document_id = request.match_info["document_id"]
        await document_manager.delete(document_id)
        return web.json_response({"deleted": True, "document_id": document_id})

    async def close_documents(app):
        await document_manager.close()

    app.add_routes([web.get("/health", health), web.get("/health/services", services), web.post("/sessions", create),
                    web.get("/sessions", listing), web.get("/sessions/{session_id}", show),
                    web.get("/sessions/{session_id}/history", history),
                    web.post("/sessions/{session_id}/end", end),
                    web.delete("/sessions/{session_id}", delete_session),
                    web.get("/documents", documents), web.post("/documents", upload_document),
                    web.delete("/documents/{document_id}", delete_document)])
    app.on_cleanup.append(close_documents)
    return app


def main():
    parser = argparse.ArgumentParser(description="Run the local demo session control API.")
    parser.add_argument("--config", type=Path, help="Session configuration path.")
    args = parser.parse_args()
    configuration = load_session_config(args.config)
    web.run_app(create_app(configuration=configuration), host=configuration.api_host, port=configuration.api_port)


if __name__ == "__main__":
    main()
