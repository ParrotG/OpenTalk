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
