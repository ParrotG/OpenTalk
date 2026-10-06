"""Real streamed text tests that assert database outcomes instead of judging prose."""

import asyncio
from datetime import datetime, time, timedelta

import pytest

from opentalk.config import PROJECT_ROOT
from opentalk.domain.booking_service import BookingService
from opentalk.llm.provider import create_llm, load_llm_config
from opentalk.llm.smoke import run_smoke
from opentalk.storage.repository import BookingRepository
from opentalk.tools.booking_tools import BookingTools
from text_harness import TextHarness

pytestmark = pytest.mark.live_llm


def make_service(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "live.sqlite3"))
    day = datetime.now(service.timezone).date() + timedelta(days=2)
    slots = []
    for hour in (9, 10):
        start = datetime.combine(day, time(hour), service.timezone)
        slots.append(service.seed_slot("Room A", start, start + timedelta(hours=1)))
    return service, day, slots


def test_live_streaming():
    result = asyncio.run(run_smoke())
    assert result["content_chunks"] >= 2
    assert result["first_content_seconds"] < result["total_seconds"]


def test_live_booking_change_confirm_cancel(tmp_path):
    async def scenario():
        service, day, slots = make_service(tmp_path)
        tools = BookingTools(service)
        config = load_llm_config()
        async with create_llm(config) as model:
            driver = TextHarness(model, config, tools)
            try:
                await driver.turn(f"请查一下 {day} Room A 的空闲时段。")
                assert any(call["name"] == "list_available_slots" for call in driver.calls)
                await driver.turn(f"帮我 book {day} 上午九点的 Room A，先给我确认信息。")
                old = service.get_operation(tools.pending_operation_id)
                assert old.status == "pending"
                assert old.target_id == slots[0].slot_id
                assert len(service.list_available_slots(day)) == 2

                await driver.turn("Change it to 10 AM instead. Please answer in English from now on.")
                proposal = service.get_operation(tools.pending_operation_id)
                assert proposal.target_id == slots[1].slot_id
                assert proposal.version == old.version + 1
                assert service.get_operation(old.operation_id).status == "invalidated"
                assert len(service.list_available_slots(day)) == 2

                # This host-side grant represents explicit confirmation of this exact proposal.
                tools.authorize_confirmation(proposal.operation_id, proposal.version)
                await driver.turn("Yes, confirm the current 10 AM Room A proposal.")
                completed = service.get_operation(proposal.operation_id)
                assert completed.status == "succeeded"
                booking = completed.result
                assert booking.slot_id == slots[1].slot_id
                assert service.get_booking(booking.booking_id).status == "active"
                assert len(service.list_available_slots(day)) == 1

                await driver.turn(f"Check the current status of booking {booking.booking_id} using the backend.")
                assert driver.calls[-1]["name"] == "get_booking"

                tools.authorize_cancellation(booking.booking_id)
                await driver.turn(f"Cancel my booking {booking.booking_id} now.")
                assert service.get_booking(booking.booking_id).status == "cancelled"
                assert len(service.list_available_slots(day)) == 2
                assert {call["name"] for call in driver.calls} >= {
                    "prepare_booking", "confirm_booking", "cancel_booking", "get_booking",
                }
            finally:
                driver.save(PROJECT_ROOT / "logs/llm-booking-lifecycle.json")
    asyncio.run(scenario())


def test_live_no_implicit_confirmation(tmp_path):
    async def scenario():
        service, day, slots = make_service(tmp_path)
        tools = BookingTools(service)
        config = load_llm_config()
        async with create_llm(config) as model:
            driver = TextHarness(model, config, tools)
            try:
                await driver.turn(
                    f"Prepare Room A at 9 AM on {day}. Do not finalize the reservation; "
                    "I have not confirmed it."
                )
                assert tools.pending_operation_id is not None
                assert service.get_operation(tools.pending_operation_id).status == "pending"
                assert len(service.list_available_slots(day)) == 2
                await driver.turn("Never mind, discard that proposal. Do not book anything.")
                assert service.get_operation(tools.pending_operation_id).status == "invalidated"
                assert len(service.list_available_slots(day)) == 2
            finally:
                driver.save(PROJECT_ROOT / "logs/llm-no-implicit-confirmation.json")
    asyncio.run(scenario())
