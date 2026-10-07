"""Agent composition adapters; session infrastructure never imports business storage."""

import asyncio

from opentalk.agents.base import ConversationAgent


def agent_factory(agent_key="booking", *, backend_config=None, service=None):
    if agent_key == "conversation":
        async def conversation(**options):
            return ConversationAgent(instructions="You are a helpful conversational assistant.", **options)
        return conversation
    if agent_key != "booking":
        raise ValueError("Unknown agent type. Use booking or conversation.")

    async def booking(**options):
        # Business dependencies are isolated to this replaceable adapter.
        from opentalk.config import load_config
        from opentalk.domain.booking_service import BookingService
        from opentalk.storage.repository import BookingRepository
        from opentalk.tools.database_tools import DatabaseTools
        from opentalk.voice.agent import BookingAgent

        config = backend_config or load_config()
        booking_service = service
        if booking_service is None:
            booking_service = await asyncio.to_thread(
                lambda: BookingService(BookingRepository(config.database_path),
                                       user_id=config.demo_user_id,
                                       session_id=options["state"].session_id, timezone=config.timezone))
        else:
            booking_service.session_id = options["state"].session_id
        return BookingAgent(DatabaseTools(booking_service, config.rooms), **options)
    return booking
