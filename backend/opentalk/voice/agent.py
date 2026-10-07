"""Booking-specific tools layered on the reusable conversation agent."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from opentalk.agents.base import ConversationAgent
from opentalk.config import PROJECT_ROOT
from opentalk.tools.database_tools import DatabaseTools


class BookingAgent(ConversationAgent):
    def __init__(self, tools: DatabaseTools, **options):
        self.booking_tools = tools
        super().__init__(
            instructions=(PROJECT_ROOT / "config/booking_prompt.md").read_text(encoding="utf-8"),
            **options,
            clock=lambda: tools.service.clock().astimezone(tools.service.timezone),
        )

    def runtime_context(self):
        return (super().runtime_context() + " "
                f"Configured rooms: {list(self.booking_tools.rooms)}.")

    @function_tool()
    async def query(self, context: RunContext, sql: str) -> dict:
        """Run one read-only SQLite statement, including joins, CTEs and date-range searches."""
        return await self.execute(context, "query", lambda: self.booking_tools.query(sql))

    @function_tool()
    async def edit(self, context: RunContext, action: Literal["add", "delete", "update"],
                   room: str, starts_at: str, ends_at: str,
                   new_room: str | None = None, new_starts_at: str | None = None,
                   new_ends_at: str | None = None) -> dict:
        """Edit a reservation AFTER asking the user and receiving confirmation in a later turn.

        Identify the original reservation by room and ISO start/end timestamps.
        Add reserves that interval; delete cancels it; update moves it to replacement
        values (omitted replacements preserve the original).
        """
        return await self.execute(context, "edit", lambda: self.booking_tools.edit(
            action, room, starts_at, ends_at, new_room=new_room,
            new_starts_at=new_starts_at, new_ends_at=new_ends_at,
            request_id=self._speech_turns[context.speech_handle.id],
        ))
