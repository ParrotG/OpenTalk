"""Opt-in real model tests for streaming and the generic conversation agent."""

import ast
import asyncio
import json
import re
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
                "请调用一次 escalate_reasoning 比较以下方案：方案 A 需要一小时，方案 B 需要两小时。"
                "两者都符合要求，我偏好较快的方案。仅使用这些事实，并在分析前用中文简短通知我。"
            ))
            assert any(event["type"] == "reasoning_finished" and event["active_profile"] == "default"
                       for event in journal.report["events"])
            calls = [item for item in session.history.items
                     if item.type == "function_call" and item.name == "escalate_reasoning"]
            assert calls
            notice = json.loads(calls[0].arguments)["message"]
            assert re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", notice)
            results = [event for event in journal.report["events"]
                       if event["type"] == "tool_result" and event["name"] == "escalate_reasoning"]
            outputs = [item for item in session.history.items if item.type == "function_call_output"
                       and item.call_id == results[0]["call_id"]]
            assert results and ast.literal_eval(outputs[0].output)["ok"] is True
            await session.run(user_input="Thanks. Just acknowledge; do not use tools.")
            assert len([event for event in journal.report["events"] if event["type"] == "reasoning_started"]) == 1
    asyncio.run(scenario())
