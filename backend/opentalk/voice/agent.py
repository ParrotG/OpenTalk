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
                f"Business timezone: {self.booking_tools.service.timezone}. "
                f"Current user UID: {self.booking_tools.service.user_id}. "
                "Resource descriptions and availability must be queried from the database.")

    @function_tool()
    async def query(self, context: RunContext, sql: str) -> dict:
        """Run one read-only SQLite statement, including joins, CTEs and date-range searches."""
        return await self.execute(context, "query", lambda: self.booking_tools.query(sql))

    @function_tool()
    async def edit(self, context: RunContext, action: Literal["add", "delete", "update"],
                   resource: str, starts_at: str, ends_at: str,
                   new_resource: str | None = None, new_starts_at: str | None = None,
                   new_ends_at: str | None = None, uid: str | None = None) -> dict:
        """Edit a reservation AFTER asking the user and receiving confirmation in a later turn.

        Identify your own original reservation by exact resource name/ID and ISO times.
        Add reserves that interval; delete cancels it; update moves it to replacement
        values (omitted replacements preserve the original).
        """
        return await self.execute(context, "edit", lambda: self.booking_tools.edit(
            action, resource, starts_at, ends_at, new_resource=new_resource,
            new_starts_at=new_starts_at, new_ends_at=new_ends_at,
            request_id=f"{self._speech_turns[context.speech_handle.id]}:{context.function_call.call_id}", uid=uid,
        ))
