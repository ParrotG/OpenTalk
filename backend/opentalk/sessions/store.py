"""Transactional SQLite session history, leases, and resumable attempts."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SessionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SessionStore:
    def __init__(self, path, *, lease_seconds=60, pending_seconds=900, clock=time.time):
        self.path = Path(path)
        self.lease_seconds = lease_seconds
        self.pending_seconds = pending_seconds
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, agent_key TEXT NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, room_name TEXT,
                    userdata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    ended_at REAL, end_requested INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    status TEXT NOT NULL, owner TEXT, created_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL, ended_at REAL, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    attempt_id TEXT NOT NULL, agent_key TEXT NOT NULL, mode TEXT NOT NULL,
                    resume_id TEXT
                );
                CREATE TABLE IF NOT EXISTS history (
                    session_id TEXT NOT NULL REFERENCES sessions,
                    item_id TEXT NOT NULL, position INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, item_id), UNIQUE(session_id, position)
                );
                CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS attempts_heartbeat ON attempts(status, heartbeat_at);
            """)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _expire(self, db):
        cutoff = self.clock() - self.lease_seconds
        ids = db.execute("""SELECT attempt_id FROM attempts WHERE
                         (status='active' AND heartbeat_at < ?) OR (status='pending' AND created_at < ?)""",
                         (cutoff, self.clock() - self.pending_seconds)).fetchall()
        for row in ids:
            self._finish(db, row["attempt_id"], "failed", "lease_expired")

    def _get(self, db, session_id):
        row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionError("session_not_found", "The session was not found.")
        result = dict(row)
        result["userdata"] = json.loads(result.pop("userdata_json"))
        result["end_requested"] = bool(result["end_requested"])
        return result

    def get(self, session_id):
        with self.connection(write=True) as db:
            self._expire(db)
            return self._get(db, session_id)

    def list(self, *, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise SessionError("invalid_page", "Use a limit from 1 to 100 and a non-negative offset.")
        with self.connection(write=True) as db:
            self._expire(db)
            ids = db.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()
            return [self._get(db, row[0]) for row in ids]

    def reserve(self, *, request_id, mode="text", agent_key="booking", resume_id=None):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise SessionError("invalid_request", "Provide a non-empty request ID of at most 200 characters.")
        if mode not in {"text", "room"} or not isinstance(agent_key, str) or not agent_key:
            raise SessionError("invalid_request", "Invalid session mode or agent key.")
        with self.connection(write=True) as db:
            self._expire(db)
            prior = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if (prior["mode"], prior["agent_key"], prior["resume_id"]) != (mode, agent_key, resume_id):
                    raise SessionError("idempotency_conflict", "The request ID belongs to a different request.")
                session = self._get(db, prior["session_id"])
                if session["attempt_id"] != prior["attempt_id"]:
                    raise SessionError("stale_request", "This request refers to an older session attempt.")
                return session
            now, attempt_id = self.clock(), uuid4().hex
            if resume_id:
                session = self._get(db, resume_id)
                if session["status"] != "completed":
                    raise SessionError("not_resumable", "Only normally completed sessions can be resumed.")
                if session["agent_key"] != agent_key:
                    raise SessionError("agent_mismatch", "Resume with the original agent type.")
                session_id = resume_id
                room = f"opentalk-{session_id}-{attempt_id}" if mode == "room" else None
                db.execute("""UPDATE sessions SET status='pending', attempt_id=?, mode=?, room_name=?,
                           updated_at=?, ended_at=NULL, end_requested=0, error_code=NULL WHERE session_id=?""",
                           (attempt_id, mode, room, now, session_id))
            else:
                session_id = uuid4().hex
                room = f"opentalk-{session_id}-{attempt_id}" if mode == "room" else None
                db.execute("""INSERT INTO sessions(session_id, agent_key, mode, status, attempt_id,
                           room_name, created_at, updated_at) VALUES(?,?,?,'pending',?,?,?,?)""",
                           (session_id, agent_key, mode, attempt_id, room, now, now))
            db.execute("INSERT INTO attempts VALUES(?,?,'pending',NULL,?,?,NULL,NULL)",
                       (attempt_id, session_id, now, now))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)",
                       (request_id, session_id, attempt_id, agent_key, mode, resume_id))
            return self._get(db, session_id)

    def claim(self, session_id, attempt_id, owner):
        with self.connection(write=True) as db:
            self._expire(db)
            session = self._get(db, session_id)
            if session["attempt_id"] != attempt_id or session["status"] != "pending" or session["end_requested"]:
                raise SessionError("session_busy", "The session attempt cannot be started.")
            now = self.clock()
            db.execute("UPDATE attempts SET status='active', owner=?, heartbeat_at=? WHERE attempt_id=?",
                       (owner, now, attempt_id))
            db.execute("UPDATE sessions SET status='active', updated_at=? WHERE session_id=?", (now, session_id))
            return self._get(db, session_id)

    def _owned(self, db, session_id, attempt_id, owner):
        row = db.execute("""SELECT a.* FROM attempts a JOIN sessions s USING(session_id)
                         WHERE a.attempt_id=? AND a.session_id=? AND s.attempt_id=a.attempt_id""",
                         (attempt_id, session_id)).fetchone()
        if row is None or row["status"] != "active" or row["owner"] != owner:
            raise SessionError("stale_attempt", "The session attempt is no longer owned by this worker.")
        if row["heartbeat_at"] < self.clock() - self.lease_seconds:
            raise SessionError("lease_expired", "The worker lease expired before this checkpoint.")

    def heartbeat(self, session_id, attempt_id, owner):
        with self.connection(write=True) as db:
            self._expire(db)
            self._owned(db, session_id, attempt_id, owner)
            now = self.clock()
            db.execute("UPDATE attempts SET heartbeat_at=? WHERE attempt_id=?", (now, attempt_id))
            db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            return self._get(db, session_id)["end_requested"]

    def history(self, session_id, *, limit=None, offset=0):
        with self.connection() as db:
            self._get(db, session_id)
            sql = "SELECT payload_json FROM history WHERE session_id=? ORDER BY position"
            args = [session_id]
            if limit is not None:
                if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
                    raise SessionError("invalid_page", "Use a history limit from 1 to 1000 and a non-negative offset.")
                sql += " LIMIT ? OFFSET ?"
                args += [limit, offset]
            return {"items": [json.loads(row[0]) for row in db.execute(sql, args)]}

    def checkpoint(self, session_id, attempt_id, owner, history, userdata, *, status=None, error_code=None):
        with self.connection(write=True) as db:
            self._owned(db, session_id, attempt_id, owner)
            position = db.execute("SELECT COALESCE(MAX(position),0) FROM history WHERE session_id=?", (session_id,)).fetchone()[0]
            for item in history["items"]:
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
                existing = db.execute("SELECT payload_json FROM history WHERE session_id=? AND item_id=?",
                                      (session_id, item["id"])).fetchone()
                if existing:
                    if existing[0] != payload:
                        db.execute("UPDATE history SET payload_json=? WHERE session_id=? AND item_id=?",
                                   (payload, session_id, item["id"]))
                else:
                    position += 1
                    db.execute("INSERT INTO history VALUES(?,?,?,?)", (session_id, item["id"], position, payload))
            now = self.clock()
            db.execute("UPDATE sessions SET userdata_json=?, updated_at=? WHERE session_id=?",
                       (json.dumps(userdata, sort_keys=True), now, session_id))
            db.execute("UPDATE attempts SET heartbeat_at=? WHERE attempt_id=?", (now, attempt_id))
            if status is not None:
                if status not in {"completed", "failed"}:
                    raise ValueError("Invalid final session status.")
                self._finish(db, attempt_id, status, error_code)

    def history_tail(self, session_id, *, max_items=120):
        with self.connection() as db:
            self._get(db, session_id)
            rows = db.execute("SELECT payload_json FROM history WHERE session_id=? ORDER BY position DESC LIMIT ?",
                              (session_id, max_items)).fetchall()
            return {"items": [json.loads(row[0]) for row in reversed(rows)]}

    def history_count(self, session_id):
        with self.connection() as db:
            self._get(db, session_id)
            return db.execute("SELECT COUNT(*) FROM history WHERE session_id=?", (session_id,)).fetchone()[0]

    def _finish(self, db, attempt_id, status, error_code=None):
        now = self.clock()
        db.execute("UPDATE attempts SET status=?, ended_at=?, error_code=? WHERE attempt_id=?",
                   (status, now, error_code, attempt_id))
        db.execute("UPDATE sessions SET status=?, ended_at=?, updated_at=?, error_code=? WHERE attempt_id=?",
                   (status, now, now, error_code, attempt_id))

    def fail(self, session_id, attempt_id, error_code):
        with self.connection(write=True) as db:
            session = self._get(db, session_id)
            if session["attempt_id"] == attempt_id and session["status"] in {"pending", "active", "completed"}:
                self._finish(db, attempt_id, "failed", error_code)

    def request_end(self, session_id, *, attempt_id=None):
        with self.connection(write=True) as db:
            self._expire(db)
            session = self._get(db, session_id)
            if attempt_id is not None and attempt_id != session["attempt_id"]:
                raise SessionError("stale_attempt", "This end request refers to an older session attempt.")
            if session["status"] == "pending":
                self._finish(db, session["attempt_id"], "failed", "not_started")
            elif session["status"] == "active":
                db.execute("UPDATE sessions SET end_requested=1 WHERE session_id=?", (session_id,))
            return self._get(db, session_id)

    def attempts(self, session_id):
        with self.connection() as db:
            self._get(db, session_id)
            return [dict(row) for row in db.execute("SELECT * FROM attempts WHERE session_id=? ORDER BY created_at", (session_id,))]
