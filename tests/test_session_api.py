"""Test allocation, signed connection grants, lifecycle, and restore without a server."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from livekit import api

from opentalk.sessions.api import create_app
from opentalk.sessions import config as session_settings
from opentalk.sessions.store import SessionStore


def test_room_token_idempotency_end_and_resume(monkeypatch):
    monkeypatch.setenv("LIVEKIT_PUBLIC_URL", "ws://browser.example:7880")
    config = session_settings.load_session_config()
    store = SessionStore(config.database_path)
    remote = SimpleNamespace(room=SimpleNamespace(delete_room=AsyncMock()))
    async def scenario():
        app = create_app(configuration=config, store=store, livekit=remote,
                         api_key="test-key", api_secret="test-secret-012345678901234567890123456789")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/health")).status == 200
            payload = {"request_id": "create", "agent": "conversation"}
            response = await client.post("/sessions", json=payload)
            assert response.status == 200
            first = await response.json()
            saved = first["session"]
            assert first["serverUrl"] == "ws://browser.example:7880"
            claims = api.TokenVerifier("test-key", "test-secret-012345678901234567890123456789").verify(first["participantToken"])
            assert claims.video.room == saved["room_name"] and claims.video.room_join
            assert not claims.video.room_admin
            dispatch = claims.room_config.agents[0]
            assert dispatch.agent_name == "opentalk-booking"
            assert json.loads(dispatch.metadata)["attempt_id"] == saved["attempt_id"]
            second = await (await client.post("/sessions", json=payload)).json()
            assert second["session"]["session_id"] == saved["session_id"]
            assert len(store.list()) == 1
            store.claim(saved["session_id"], saved["attempt_id"], "worker")
            endpoint = f"/sessions/{saved['session_id']}/end"
            response = await client.post(endpoint, json={"attempt_id": saved["attempt_id"]})
            assert response.status == 202
            remote.room.delete_room.assert_not_awaited()
            # Completion belongs to the worker and commits with its final checkpoint.
            store.checkpoint(saved["session_id"], saved["attempt_id"], "worker", {"items": []}, {}, status="completed")
            assert (await client.post(endpoint, json={"attempt_id": saved["attempt_id"]})).status == 200
            remote.room.delete_room.assert_awaited_once()
            resumed = await (await client.post("/sessions", json={"request_id": "resume", "agent": "conversation",
                "resume_session_id": saved["session_id"]})).json()
            assert resumed["session"]["session_id"] == saved["session_id"]
            assert resumed["roomName"] != first["roomName"]
            assert (await client.post(endpoint, json={"attempt_id": saved["attempt_id"]})).status == 409
            assert (await client.get(f"/sessions/{saved['session_id']}/history")).status == 200
            assert (await client.get(f"/sessions/{saved['session_id']}")).status == 200
    asyncio.run(scenario())


def test_api_invalid_requests_failed_restore_and_pagination():
    config = session_settings.load_session_config()
    store = SessionStore(config.database_path)
    async def scenario():
        async with TestClient(TestServer(create_app(configuration=config, store=store,
                         api_key="test-key", api_secret="test-secret-012345678901234567890123456789"))) as client:
            for payload in ({}, [], {"request_id": "x", "agent": "invalid"}):
                assert (await client.post("/sessions", json=payload)).status == 400
            saved = store.reserve(request_id="failed")
            store.fail(saved["session_id"], saved["attempt_id"], "test_failure")
            assert (await client.post("/sessions", json={"request_id": "resume", "resume_session_id": saved["session_id"]})).status == 409
            assert (await client.get("/sessions?limit=10000")).status == 409
            assert (await client.get("/sessions/unknown")).status == 404
            assert (await client.post(f"/sessions/{saved['session_id']}/end", json={})).status == 400
    asyncio.run(scenario())


def test_service_checks_report_unavailable_dependencies_without_provider_calls(monkeypatch):
    from aiohttp import web
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-test-key")
    monkeypatch.setenv("SONIOX_API_KEY", "configured-test-key")
    config = session_settings.load_session_config()
    remote = SimpleNamespace(room=SimpleNamespace(list_rooms=AsyncMock()))

    async def scenario():
        worker = web.Application()
        async def health(request):
            return web.Response(text="OK")
        async def information(request):
            return web.json_response({"agent_name": "opentalk-booking"})
        worker.add_routes([web.get("/", health), web.get("/worker", information)])
        async with TestServer(worker) as worker_server:
            monkeypatch.setenv("OPENTALK_WORKER_HEALTH_URL", str(worker_server.make_url("/")))
            async with TestClient(TestServer(create_app(configuration=config, livekit=remote,
                    api_key="test-key", api_secret="test-secret"))) as client:
                response = await client.get("/health/services")
                assert response.status == 200
                checks = await response.json()
                assert checks["status"] == "ok"
                assert checks["services"]["providers"]["status"] == "configured"
                remote.room.list_rooms.side_effect = RuntimeError("Sensitive internal exception")
                checks = await (await client.get("/health/services")).json()
                assert checks["status"] == "degraded"
                assert checks["services"]["livekit"]["status"] == "unavailable"
                assert "Sensitive" not in json.dumps(checks)
    asyncio.run(scenario())


def test_history_tail_returns_recent_native_items_with_bounds():
    config = session_settings.load_session_config()
    store = SessionStore(config.database_path)
    saved = store.reserve(request_id="history")
    store.claim(saved["session_id"], saved["attempt_id"], "worker")
    items = [{"id": f"message-{index}", "type": "message", "role": "user", "content": [str(index)]} for index in range(5)]
    store.checkpoint(saved["session_id"], saved["attempt_id"], "worker", {"items": items}, {})
    async def scenario():
        async with TestClient(TestServer(create_app(configuration=config, store=store))) as client:
            endpoint = f"/sessions/{saved['session_id']}/history?tail=true&limit="
            result = await (await client.get(endpoint + "2")).json()
            assert [item["id"] for item in result["items"]] == ["message-3", "message-4"]
            assert (await client.get(endpoint + "1001")).status == 400
    asyncio.run(scenario())
