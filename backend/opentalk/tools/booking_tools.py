"""Meeting-room tools with host-controlled authorization for committed writes."""

import asyncio
from dataclasses import asdict
from datetime import date
from uuid import uuid4

from livekit.agents import function_tool
from livekit.agents.llm import find_function_tools

from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError


class BookingTools:
    def __init__(self, service: BookingService):
        self.service = service
        self.pending_operation_id: str | None = None
        self._confirmed: set[tuple[str, int]] = set()
        self._cancellations: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get_tools(self):
        return find_function_tools(self)

    def authorize_confirmation(self, operation_id: str, version: int) -> None:
        """Called by the host after user confirmation, never exposed as an LLM tool."""
        operation = self.service.get_operation(operation_id)
        if operation.status != "pending" or operation.version != version:
            raise BookingError("confirmation_mismatch", "The proposal is no longer confirmable.")
        self._confirmed.add((operation_id, version))

    def authorize_cancellation(self, booking_id: str) -> None:
        """Bind an explicit cancellation request to a stable host-generated key."""
        self.service.get_booking(booking_id)
        self._cancellations.setdefault(booking_id, str(uuid4()))

    async def _call(self, function, *args, **kwargs) -> dict:
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
            return {"ok": True, "data": asdict(result)}
        except BookingError as error:
            return {"ok": False, "error": error.code, "message": str(error)}

    @function_tool()
    async def list_available_slots(self, day: str, room: str | None = None) -> dict:
        """Find free slots for a YYYY-MM-DD date and optional exact room name.

        Times in the returned slots are UTC; present them in the business timezone.
        """
        try:
            slots = await asyncio.to_thread(
                self.service.list_available_slots, date.fromisoformat(day), room,
            )
            return {"ok": True, "data": [asdict(slot) for slot in slots]}
        except (ValueError, BookingError) as error:
            return {"ok": False, "error": getattr(error, "code", "invalid_date"),
                    "message": "Provide a valid YYYY-MM-DD date and room."}

    @function_tool()
    async def prepare_booking(self, slot_id: str) -> dict:
        """Prepare a selected slot without reserving it. Replaces any pending proposal.

        Ask the user to confirm the returned operation ID and version before booking.
        """
        async with self._lock:
            supersedes = None
            if self.pending_operation_id:
                old = await asyncio.to_thread(self.service.get_operation, self.pending_operation_id)
                if old.status == "pending":
                    if old.target_id == slot_id:
                        return {"ok": True, "data": asdict(old)}
                    supersedes = old.operation_id
            result = await self._call(
                self.service.prepare_booking, slot_id, str(uuid4()), supersedes=supersedes,
            )
            if result["ok"]:
                if supersedes:
                    self._confirmed = {pair for pair in self._confirmed if pair[0] != supersedes}
                self.pending_operation_id = result["data"]["operation_id"]
            return result

    @function_tool()
    async def confirm_booking(self, operation_id: str, version: int) -> dict:
        """Commit the exact proposal only after the host has accepted user confirmation."""
        async with self._lock:
            if (operation_id, version) not in self._confirmed:
                return {"ok": False, "error": "confirmation_required",
                        "message": "Explicit user confirmation for this proposal is required."}
            return await self._call(self.service.confirm_booking, operation_id, version)

    @function_tool()
    async def get_booking(self, booking_id: str) -> dict:
        """Retrieve the current state of a booking belonging to the current user."""
        return await self._call(self.service.get_booking, booking_id)

    @function_tool()
    async def get_operation(self, operation_id: str) -> dict:
        """Recover the persisted outcome of an operation after a lost response."""
        return await self._call(self.service.get_operation, operation_id)

    @function_tool()
    async def invalidate_operation(self, operation_id: str) -> dict:
        """Discard an unconfirmed proposal when the user abandons the booking."""
        async with self._lock:
            result = await self._call(self.service.invalidate_operation, operation_id)
            if result["ok"]:
                self._confirmed = {pair for pair in self._confirmed if pair[0] != operation_id}
            return result

    @function_tool()
    async def cancel_booking(self, booking_id: str) -> dict:
        """Cancel a booking only after the host has authorized that explicit user request."""
        async with self._lock:
            operation_id = self._cancellations.get(booking_id)
            if operation_id is None:
                return {"ok": False, "error": "cancellation_required",
                        "message": "An explicit cancellation request is required."}
            return await self._call(self.service.cancel_booking, booking_id, operation_id)
