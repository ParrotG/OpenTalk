"""Bounded diagnostics and metrics, separate from resumable conversation data."""

import json
import sqlite3
import time
from pathlib import Path


class TelemetryStore:
    def __init__(self, path, *, max_events=10000, retention_days=7):
        self.path = Path(path)
        self.max_events = max_events
        self.retention_seconds = retention_days * 86400
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS telemetry (
                       event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                       kind TEXT NOT NULL, timestamp REAL NOT NULL, payload_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS telemetry_time ON telemetry(timestamp)")

    def write(self, events):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.executemany("""INSERT INTO telemetry VALUES(?,?,?,?,?,?) ON CONFLICT(event_id)
                           DO UPDATE SET payload_json=excluded.payload_json, timestamp=excluded.timestamp
                           WHERE telemetry.payload_json != excluded.payload_json""", [
                (ev["event_id"], ev["session_id"], ev["attempt_id"], ev["type"], ev["timestamp"],
                 json.dumps(ev, ensure_ascii=False, sort_keys=True)) for ev in events
            ])
            db.execute("DELETE FROM telemetry WHERE timestamp < ?", (time.time() - self.retention_seconds,))
            db.execute("""DELETE FROM telemetry WHERE event_id IN (
                       SELECT event_id FROM telemetry ORDER BY timestamp DESC, event_id DESC LIMIT -1 OFFSET ?)""",
                       (self.max_events,))

    def read(self, session_id, *, limit=100):
        with sqlite3.connect(self.path, timeout=5) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM telemetry WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            )]
