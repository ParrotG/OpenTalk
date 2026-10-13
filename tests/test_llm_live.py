"""Opt-in real model tests for streaming and the generic conversation agent."""

import asyncio
import ast
from dataclasses import replace

import pytest

from opentalk.llm.smoke import run_smoke
from opentalk.voice.config import load_voice_config
from opentalk.voice.factories import agent_factory
from opentalk.voice.session import open_session

pytestmark = pytest.mark.live_llm


def test_live_streaming():
    result = asyncio.run(run_smoke())
    assert result["content_chunks"] >= 2
    assert result["first_content_seconds"] < result["total_seconds"]


def test_live_scoped_reasoning(tmp_path):
    config = replace(load_voice_config(), log_directory=tmp_path / "logs")

    async def scenario():
        async with open_session(text_only=True, factory=agent_factory(), voice_config=config) as (session, agent, journal):
            await session.start(agent=agent)
            await session.run(user_input=(
                "Use escalate_reasoning once to compare these given options: Option A takes one hour "
                "and Option B takes two hours. Both meet the requirements; I prefer the faster option. "
                "Use only the supplied facts. Briefly notify me before the analysis."
            ))
            assert any(event["type"] == "reasoning_finished" and event["active_profile"] == "default"
                       for event in journal.report["events"])
            results = [event for event in journal.report["events"]
                       if event["type"] == "tool_result" and event["name"] == "escalate_reasoning"]
            outputs = [item for item in session.history.items if item.type == "function_call_output"
                       and item.call_id == results[0]["call_id"]]
            assert results and ast.literal_eval(outputs[0].output)["ok"] is True
            await session.run(user_input="Thanks. Just acknowledge; do not use tools.")
            assert len([event for event in journal.report["events"] if event["type"] == "reasoning_started"]) == 1
    asyncio.run(scenario())
