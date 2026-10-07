"""Opt-in real model tests for streaming and the current query/edit native agent."""

import asyncio
import ast
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from opentalk.domain.booking_service import BookingService
from opentalk.llm.smoke import run_smoke
from opentalk.storage.repository import BookingRepository
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory
from opentalk.voice.session import open_session

pytestmark = pytest.mark.live_llm


def make_service(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "live.sqlite3"))
    day = datetime.now(service.timezone).date() + timedelta(days=2)
    for hour in (9, 10):
        start = datetime.combine(day, time(hour), service.timezone)
        service.seed_slot("Room A", start, start + timedelta(hours=1))
    return service, day, replace(load_voice_config(), log_directory=tmp_path / "logs")


def test_live_streaming():
    result = asyncio.run(run_smoke())
    assert result["content_chunks"] >= 2
    assert result["first_content_seconds"] < result["total_seconds"]


def test_live_sql_booking_update_cancel(tmp_path):
    service, day, config = make_service(tmp_path)

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input="List all known rooms and find the earliest available intervals, today or any other day.")
            assert any(event["type"] == "business_result" and event["name"] == "query" and event["result"]["ok"]
                       for event in journal.report["events"])
            assert service.list_events() == []
            await session.run(user_input=f"Book Room A on {day} from 09:00 to 10:00 local time. Ask me before editing.")
            assert service.list_events() == []
            await session.run(user_input="Yes, that works. Go ahead.")
            rows = (await agent.booking_tools.query("SELECT * FROM bookings WHERE status='active'"))["rows"]
            assert len(rows) == 1
            booking_id = rows[0]["booking_id"]
            await session.run(user_input=f"Move that reservation to {day} from 10:00 to 11:00, same room. Ask before editing.")
            assert len(service.list_events()) == 1
            await session.run(user_input="可以，请修改。")
            slot = (await agent.booking_tools.query(
                "SELECT s.* FROM slots s JOIN bookings b USING(slot_id) WHERE b.status='active'"))["rows"][0]
            assert datetime.fromisoformat(slot["starts_at"]).astimezone(service.timezone).hour == 10
            await session.run(user_input="Cancel this reservation. Please ask me before editing.")
            assert service.get_booking(booking_id).status == "active"
            await session.run(user_input="Please proceed with the cancellation.")
            assert service.get_booking(booking_id).status == "cancelled"
            assert len(service.list_events()) == 3
    asyncio.run(scenario())


def test_live_no_edit_without_confirmation(tmp_path):
    service, day, config = make_service(tmp_path)

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config) as (session, agent, _):
            await session.start(agent=agent)
            await session.run(user_input=f"I want Room A on {day} from 09:00 to 10:00. Do not edit until I confirm.")
            assert service.list_events() == []
            await session.run(user_input="Never mind, don't book anything.")
            assert service.list_events() == []
            assert (await agent.booking_tools.query("SELECT count(*) AS n FROM bookings"))["rows"] == [{"n": 0}]
    asyncio.run(scenario())


def test_live_scoped_reasoning(tmp_path):
    service, day, config = make_service(tmp_path)

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(service=service), voice_config=config) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input=(
                "Use escalate_reasoning once to compare these given options: Room A 09:00-10:00 "
                "and Room B 10:00-11:00. Both fit a one-hour meeting; I prefer the earliest. "
                "Do not edit or reserve anything. Briefly notify me before the analysis."
            ))
            assert any(event["type"] == "reasoning_finished" and event["active_profile"] == "default"
                       for event in journal.report["events"])
            results = [event for event in journal.report["events"]
                       if event["type"] == "tool_result" and event["name"] == "escalate_reasoning"]
            outputs = [item for item in session.history.items if item.type == "function_call_output"
                       and item.call_id == results[0]["call_id"]]
            assert results and ast.literal_eval(outputs[0].output)["ok"] is True
            assert service.list_events() == []
            await session.run(user_input="Thanks. Just acknowledge; do not use tools.")
            assert len([event for event in journal.report["events"] if event["type"] == "reasoning_started"]) == 1
    asyncio.run(scenario())
