"""Typed booking records and stable business errors."""

from dataclasses import dataclass, field


class BookingError(Exception):
    """Expose a stable error code and an English diagnostic message."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Slot:
    slot_id: str
    room: str
    starts_at: str
    ends_at: str
    resource_id: str = ""


@dataclass(frozen=True)
class User:
    uid: str
    name: str
    department: str


@dataclass(frozen=True)
class Resource:
    rid: str
    name: str
    type: str
    location: str
    capacity: int
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Booking:
    booking_id: str
    user_id: str
    slot_id: str
    status: str
    created_at: str


@dataclass(frozen=True)
class Operation:
    operation_id: str
    user_id: str
    kind: str
    target_id: str
    version: int
    status: str
    result: Booking | None
    error_code: str | None
    supersedes: str | None = None
    request: dict | None = None
