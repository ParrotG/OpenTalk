"""Explicit serializable application state stored as LiveKit session userdata."""

from dataclasses import asdict, dataclass


@dataclass
class SessionState:
    session_id: str
    attempt_id: str
    preferred_response_language: str | None = None

    def to_dict(self):
        return asdict(self)
