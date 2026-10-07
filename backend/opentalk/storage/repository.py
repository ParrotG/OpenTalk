"""SQLite business storage, atomic migrations, and serialized reservation writes."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from opentalk.domain.models import Booking, Operation, Resource, Slot, User


def stable_id(kind, value):
    return str(uuid5(NAMESPACE_URL, f"opentalk:{kind}:{value}"))


def apply_schema(connection):
    """Execute DDL without executescript's implicit transaction commit."""
    statement = ""
    for line in Path(__file__).with_name("schema.sql").read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""


class BookingRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(slots)")}
            legacy = "room" in columns
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise ValueError("The booking database uses a newer, unsupported schema.")
            if legacy:
                # Keep a consistent pre-migration copy, including committed WAL data.
                backup_path = self.database_path.with_name(self.database_path.name + ".pre-resources-v1.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        connection.backup(backup)
                connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Another constructor may already have completed the migration.
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(slots)")}
                if "room" in columns:
                    self._migrate(connection)
                else:
                    apply_schema(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("The booking database contains invalid foreign keys.")
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migrate(self, connection):
        slots = [dict(row) for row in connection.execute("SELECT * FROM slots")]
        bookings = [dict(row) for row in connection.execute("SELECT * FROM bookings")]
        operations = [dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY rowid")]
        for table in ("events", "bookings", "operations", "slots"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        apply_schema(connection)
        for uid in sorted({row["user_id"] for row in operations + bookings}):
            self.insert_user(connection, User(uid, uid, "Unspecified"))
        for name in sorted({row["room"] for row in slots}):
            self.insert_resource(connection, Resource(stable_id("resource", name), name,
                                 "meeting_room", "Unspecified", 1))
        for row in slots:
            self.insert_slot(connection, Slot(row["slot_id"], row["room"], row["starts_at"],
                             row["ends_at"], stable_id("resource", row["room"])))
        # Insert self-references after all operations exist, regardless of legacy row order.
        for row in operations:
            connection.execute(
                """INSERT INTO operations (operation_id, uid, kind, target_id, version,
                status, result_json, error_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(row[key] for key in ("operation_id", "user_id", "kind", "target_id", "version",
                      "status", "result_json", "error_code", "created_at", "updated_at")))
        for row in operations:
            connection.execute("UPDATE operations SET supersedes=? WHERE operation_id=?",
                               (row["supersedes"], row["operation_id"]))
        for row in bookings:
            self.insert_booking(connection, Booking(**{key: row[key] for key in
                                ("booking_id", "user_id", "slot_id", "status", "created_at")}),
                                row["operation_id"])

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def insert_user(self, connection, user):
        connection.execute("INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(uid) DO NOTHING",
                           (user.uid, user.name, user.department))

    def insert_resource(self, connection, resource):
        connection.execute("INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(rid) DO NOTHING",
                           (resource.rid, resource.name, resource.type, resource.location,
                            resource.capacity, json.dumps(resource.metadata, ensure_ascii=False, sort_keys=True)))

    def find_resource(self, connection, reference):
        rows = connection.execute("SELECT * FROM resources WHERE rid=? OR name=?",
                                  (reference, reference)).fetchall()
        if len(rows) != 1:
            return None
        row = dict(rows[0])
        row["metadata"] = json.loads(row["metadata"])
        return Resource(**row)

    def find_operation(self, connection, operation_id):
        row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        return Operation(row["operation_id"], row["uid"], row["kind"], row["target_id"],
                         row["version"], row["status"],
                         Booking(**json.loads(row["result_json"])) if row["result_json"] else None,
                         row["error_code"], row["supersedes"],
                         json.loads(row["request_json"]) if row["request_json"] else None)

    def find_slot(self, connection, slot_id):
        row = connection.execute("""SELECT s.sid AS slot_id, r.name AS room, s.starts_at,
            s.ends_at, s.rid AS resource_id FROM slots s JOIN resources r USING(rid) WHERE sid=?""",
                                 (slot_id,)).fetchone()
        return Slot(**dict(row)) if row else None

    def insert_slot(self, connection, slot):
        connection.execute("INSERT INTO slots VALUES (?, ?, ?, ?)",
                           (slot.slot_id, slot.resource_id, slot.starts_at, slot.ends_at))

    def slots(self, connection):
        rows = connection.execute("""SELECT s.sid AS slot_id, r.name AS room, s.starts_at,
            s.ends_at, s.rid AS resource_id FROM slots s JOIN resources r USING(rid)
            ORDER BY s.starts_at, r.name""").fetchall()
        return [Slot(**dict(row)) for row in rows]

    def insert_operation(self, connection, operation, now):
        connection.execute("""INSERT INTO operations
            (operation_id, uid, kind, target_id, version, supersedes, status, request_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (operation.operation_id, operation.user_id, operation.kind, operation.target_id,
             operation.version, operation.supersedes, operation.status,
             json.dumps(operation.request, sort_keys=True) if operation.request is not None else None, now, now))

    def update_operation(self, connection, operation_id, status, now, result=None, error_code=None):
        connection.execute("""UPDATE operations SET status=?, result_json=?, error_code=?, updated_at=?
            WHERE operation_id=?""", (status, json.dumps(asdict(result)) if result else None,
                                      error_code, now, operation_id))

    def insert_booking(self, connection, booking, operation_id):
        connection.execute("INSERT INTO slot_users VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (booking.booking_id, booking.slot_id, booking.user_id, operation_id,
                            booking.status, booking.created_at, booking.created_at))

    def find_booking(self, connection, booking_id):
        row = connection.execute("""SELECT booking_id, uid AS user_id, sid AS slot_id, status, created_at
            FROM slot_users WHERE booking_id=?""", (booking_id,)).fetchone()
        return Booking(**dict(row)) if row else None

    def cancel_booking(self, connection, booking_id, now):
        connection.execute("UPDATE slot_users SET status='cancelled', updated_at=? WHERE booking_id=?",
                           (now, booking_id))

    def operations(self, connection, user_id=None):
        rows = connection.execute("SELECT operation_id FROM operations WHERE (? IS NULL OR uid=?) ORDER BY rowid",
                                  (user_id, user_id)).fetchall()
        return [self.find_operation(connection, row[0]) for row in rows]
