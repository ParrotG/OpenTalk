"""Verify durable sessions, concurrent ownership, replay safety, and bounded telemetry."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from livekit.agents import llm

from opentalk.sessions.store import SessionError, SessionStore
from opentalk.sessions.telemetry import TelemetryStore


def active(store):
    saved = store.reserve(request_id="create")
    store.claim(saved["session_id"], saved["attempt_id"], "worker")
    return saved


def test_checkpoint_idempotency_finalization_and_resume(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    saved = active(store)
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="记住 project OpenTalk")
    ctx.add_message(role="assistant", content="I will remember OpenTalk.")
    history = ctx.to_dict(exclude_timestamp=False, exclude_metrics=True)
    for _ in range(3):
        store.checkpoint(saved["session_id"], saved["attempt_id"], "worker", history,
                         {"preferred_response_language": "en"})
    assert len(store.history(saved["session_id"])["items"]) == 2
    with pytest.raises(SessionError, match="normally completed"):
        store.reserve(request_id="too-early", resume_id=saved["session_id"])
    store.checkpoint(saved["session_id"], saved["attempt_id"], "worker", history,
                     {"preferred_response_language": "en"}, status="completed")
    restarted = SessionStore(store.path)
    resumed = restarted.reserve(request_id="resume", resume_id=saved["session_id"])
    assert resumed["session_id"] == saved["session_id"]
    assert resumed["attempt_id"] != saved["attempt_id"]
    assert resumed["userdata"]["preferred_response_language"] == "en"
    assert restarted.reserve(request_id="resume", resume_id=saved["session_id"])["attempt_id"] == resumed["attempt_id"]
    with pytest.raises(SessionError, match="no longer owned"):
        restarted.checkpoint(saved["session_id"], saved["attempt_id"], "worker", history, {})
    assert len(restarted.attempts(saved["session_id"])) == 2
    with pytest.raises(SessionError, match="older session attempt"):
        restarted.request_end(saved["session_id"], attempt_id=saved["attempt_id"])


def test_concurrent_reserve_and_claim_have_one_owner(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: store.reserve(request_id="same"), range(2)))
    assert rows[0]["session_id"] == rows[1]["session_id"]
    saved = rows[0]

    def claim(owner):
        try:
            store.claim(saved["session_id"], saved["attempt_id"], owner)
            return True
        except SessionError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["a", "b"])) == [False, True]
    with pytest.raises(SessionError, match="different request"):
        store.reserve(request_id="same", agent_key="conversation")


def test_failed_cancelled_and_expired_sessions_are_not_resumable(tmp_path):
    now = [100.0]
    store = SessionStore(tmp_path / "sessions.sqlite3", lease_seconds=10, clock=lambda: now[0])
    saved = active(store)
    now[0] = 111.0
    assert store.get(saved["session_id"])["status"] == "failed"
    assert store.get(saved["session_id"])["error_code"] == "lease_expired"
    with pytest.raises(SessionError, match="normally completed"):
        store.reserve(request_id="resume", resume_id=saved["session_id"])
    other = store.reserve(request_id="never-connected")
    assert store.request_end(other["session_id"])["status"] == "failed"
    assert store.request_end(other["session_id"])["status"] == "failed"


def test_snapshot_and_completed_status_commit_together(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    saved = active(store)
    with pytest.raises(KeyError):
        store.checkpoint(saved["session_id"], saved["attempt_id"], "worker",
                         {"items": [{"id": "valid", "type": "message"}, {"type": "message"}]}, {}, status="completed")
    assert store.get(saved["session_id"])["status"] == "active"
    assert store.history(saved["session_id"])["items"] == []


def test_telemetry_upsert_retention_and_row_limit(tmp_path):
    store = TelemetryStore(tmp_path / "metrics.sqlite3", max_events=3, retention_days=1)
    def event(index, timestamp=None):
        return {"event_id": str(index), "session_id": "session", "attempt_id": "attempt",
                "type": "metric", "timestamp": timestamp or time.time(), "data": index}
    events = [event(i) for i in range(10)]
    store.write(events)
    store.write(events)
    assert len(store.read("session")) == 3
    store.write([event("old", time.time() - 86401)])
    assert len(store.read("session")) == 3
    assert not any(ev["event_id"] == "old" for ev in store.read("session"))
