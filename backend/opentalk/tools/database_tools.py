"""Read-only SQL and user-scoped resource reservation edits for the local demo."""

import asyncio
import sqlite3
import time
import tomllib
from contextlib import closing

from opentalk.config import PROJECT_ROOT
from opentalk.domain.models import BookingError


class DatabaseTools:
    def __init__(self, service):
        self.service = service
        with (PROJECT_ROOT / "config/booking_agent.toml").open("rb") as file:
            options = tomllib.load(file)["query"]
        self.max_rows = options["max_rows"]
        self.max_vm_steps = options["max_vm_steps"]
        self.query_timeout = options["timeout_seconds"]
        if (type(self.max_rows) is not int or self.max_rows < 1
                or type(self.max_vm_steps) is not int or self.max_vm_steps < 1
                or type(self.query_timeout) not in (float, int) or not 0 < self.query_timeout <= 60):
            raise ValueError("Invalid query resource limits.")

    async def query(self, sql):
        try:
            return await asyncio.to_thread(self._query, sql)
        except (sqlite3.Error, ValueError) as error:
            return {"ok": False, "error": "query_rejected", "message": str(error)}

    def _query(self, sql):
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Provide one read-only SQL statement.")
        path = self.service.repository.database_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(path, uri=True, timeout=self.query_timeout)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

            def authorize(action, first, second, database, source):
                if action not in allowed:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {
                    "load_extension", "readfile", "writefile",
                }:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            deadline = time.monotonic() + self.query_timeout
            steps = 0

            def budget():
                nonlocal steps
                steps += 1000
                return int(steps >= self.max_vm_steps or time.monotonic() >= deadline)
            connection.set_progress_handler(budget, 1000)
            cursor = connection.execute(sql)
            if cursor.description is None:
                raise ValueError("Only result-producing read queries are permitted.")
            rows = cursor.fetchmany(self.max_rows + 1)
            return {"ok": True, "columns": [item[0] for item in cursor.description],
                    "rows": [dict(row) for row in rows[:self.max_rows]],
                    "truncated": len(rows) > self.max_rows, "max_rows": self.max_rows}

    async def edit(self, action, resource, starts_at, ends_at, *, new_resource=None,
                   new_starts_at=None, new_ends_at=None, request_id, uid=None):
        try:
            return await asyncio.to_thread(self.service.edit, action, resource, starts_at, ends_at,
                                           new_resource=new_resource, new_starts_at=new_starts_at,
                                           new_ends_at=new_ends_at, request_id=request_id, uid=uid)
        except BookingError as error:
            return {"ok": False, "error": error.code, "message": str(error)}
        except sqlite3.Error:
            return {"ok": False, "error": "database_error", "message": "The database edit failed."}
