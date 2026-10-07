"""Offline checks for configuration, the streaming protocol, and write authorization."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from livekit.agents import llm
from livekit.agents.llm import ToolContext
from livekit.agents.llm.utils import execute_function_call
from livekit.plugins import openai as livekit_openai
from openai import AsyncOpenAI

from opentalk.domain.booking_service import BookingService
from opentalk.llm import provider
from opentalk.storage.repository import BookingRepository
from opentalk.tools.booking_tools import BookingTools
from text_harness import TextHarness


def service_and_slot(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "tools.sqlite3"),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    start = datetime(2030, 1, 2, 1, tzinfo=UTC)
    return service, service.seed_slot("Room A", start, start + timedelta(hours=1))


def test_provider_configuration_and_environment_precedence(monkeypatch, tmp_path):
    config = provider.load_llm_config()
    assert config.model == "deepseek-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.extra_body == {"thinking": {"type": "disabled"}}
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env.local").write_text("TEST_LLM_CREDENTIAL=file-value\n", encoding="utf-8")
    monkeypatch.setattr(provider, "PROJECT_ROOT", root)
    config = replace(config, api_key_env="TEST_LLM_CREDENTIAL")
    captured = {}

    def constructor(**kwargs):
        captured.update(kwargs)
        return "fake-model"

    monkeypatch.setattr(provider.openai, "LLM", constructor)
    monkeypatch.setenv("TEST_LLM_CREDENTIAL", "environment-value")
    assert provider.create_llm(config) == "fake-model"
    assert captured["api_key"] == "environment-value"
    assert captured["model"] == config.model
    monkeypatch.delenv("TEST_LLM_CREDENTIAL")
    provider.create_llm(config)
    assert captured["api_key"] == "file-value"
    monkeypatch.delenv("TEST_LLM_CREDENTIAL")
    (root / ".env.local").unlink()
    with pytest.raises(ValueError, match="Missing required credential: TEST_LLM_CREDENTIAL"):
        provider.create_llm(config)


def test_llm_config_rejects_protocol_override(tmp_path):
    path = tmp_path / "llm.toml"
    text = (provider.PROJECT_ROOT / "config/llm.toml").read_text(encoding="utf-8")
    path.write_text(text.replace("[llm.extra_body.thinking]", "[llm.extra_body]\nstream = false\n\n[llm.extra_body.thinking]"),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="override protocol"):
        provider.load_llm_config(path)


def test_tool_grants_validation_replacement_and_retry(tmp_path):
    async def scenario():
        service, slot = service_and_slot(tmp_path)
        tools = BookingTools(service)
        context = ToolContext(tools.get_tools())
        assert len(context.function_tools) == 7
        invalid = await execute_function_call(
            llm.FunctionToolCall(call_id="invalid", name="list_available_slots", arguments="{}"),
            context,
        )
        assert invalid.fnc_call_out.is_error
        unknown = await execute_function_call(
            llm.FunctionToolCall(call_id="unknown", name="execute_sql", arguments="{}"), context,
        )
        assert unknown.fnc_call_out.is_error
        proposal = (await tools.prepare_booking(slot.slot_id))["data"]
        assert (await tools.prepare_booking(slot.slot_id))["data"] == proposal
        denied = await tools.confirm_booking(proposal["operation_id"], proposal["version"])
        assert denied["error"] == "confirmation_required"
        assert len(service.list_available_slots(date(2030, 1, 2))) == 1

        tools.authorize_confirmation(proposal["operation_id"], proposal["version"])
        second = service.seed_slot("Room A", datetime(2030, 1, 2, 2, tzinfo=UTC),
                                   datetime(2030, 1, 2, 3, tzinfo=UTC))
        replacement = (await tools.prepare_booking(second.slot_id))["data"]
        assert replacement["version"] == 2
        assert (await tools.confirm_booking(proposal["operation_id"], 1))["ok"] is False
        tools.authorize_confirmation(replacement["operation_id"], 2)
        booking = await tools.confirm_booking(replacement["operation_id"], 2)
        assert await tools.confirm_booking(replacement["operation_id"], 2) == booking
        booking_id = booking["data"]["booking_id"]
        assert (await tools.cancel_booking(booking_id))["error"] == "cancellation_required"
        tools.authorize_cancellation(booking_id)
        cancelled = await tools.cancel_booking(booking_id)
        assert await tools.cancel_booking(booking_id) == cancelled
        assert service.get_booking(booking_id).status == "cancelled"
        assert len(service.list_events()) == 5
    asyncio.run(scenario())


def test_fragmented_tool_stream_and_role_transcript(tmp_path):
    async def scenario():
        service, slot = service_and_slot(tmp_path)
        tools = BookingTools(service)
        config = provider.load_llm_config()
        requests = []

        def chunk(delta, finish_reason=None):
            return {"id": "test-response", "object": "chat.completion.chunk",
                    "created": 1, "model": config.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        def transport(request):
            body = json.loads(request.content)
            assert body["stream"] is True
            assert body["model"] == config.model
            requests.append(body)
            if len(requests) == 1:
                assert body["tools"]
                events = [
                    chunk({"tool_calls": [{"index": 0, "id": "call-slots", "type": "function",
                                          "function": {"name": "list_available_slots", "arguments": '{"day":'}}]}),
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"2030-01-02"}'}}]}),
                    chunk({}, "tool_calls"),
                ]
            else:
                messages = body["messages"]
                assert any(message["role"] == "tool" and message["tool_call_id"] == "call-slots"
                           for message in messages)
                events = [chunk({"content": "One slot "}), chunk({"content": "is available."}),
                          chunk({}, "stop")]
            data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=data + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        async with AsyncOpenAI(api_key="offline-test-key", base_url="https://offline.invalid",
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))) as client:
            async with livekit_openai.LLM(model=config.model, client=client) as model:
                driver = TextHarness(model, config, tools)
                assert await driver.turn("Find slots on 2030-01-02.") == "One slot is available."
                assert len(driver.calls) == 1
                assert driver.calls[0]["name"] == "list_available_slots"
                assert json.loads(driver.calls[0]["arguments"])["day"] == "2030-01-02"
                report = tmp_path / "report.json"
                driver.save(report)
                driver.save(report)
                data = json.loads(report.read_text(encoding="utf-8"))
                assert len(data["tool_calls"]) == 1
                roles = [item["role"] for item in data["transcript"]["items"] if item["type"] == "message"]
                assert roles == ["system", "user", "assistant"]
                assert "offline-test-key" not in report.read_text(encoding="utf-8")
    asyncio.run(scenario())


def test_reasoning_profile_isolated_and_streaming_request(monkeypatch):
    config = provider.load_llm_config()
    strong = provider.reasoning_config(config)
    assert config.extra_body == {"thinking": {"type": "disabled"}}
    assert strong.extra_body == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert strong.max_completion_tokens == 8192
    captured = {}
    native_llm = livekit_openai.LLM
    monkeypatch.setenv(config.api_key_env, "offline-test-key")
    monkeypatch.setattr(provider.openai, "LLM", lambda **kwargs: captured.update(kwargs) or "fake")
    provider.create_llm(strong)
    assert captured["extra_body"]["thinking"]["type"] == "enabled"
    provider.create_llm(config)
    assert captured["extra_body"]["thinking"]["type"] == "disabled"

    async def scenario():
        requests = []

        def transport(request):
            body = json.loads(request.content)
            requests.append(body)
            assert body["stream"] and body["thinking"] == {"type": "enabled"}
            assert body["reasoning_effort"] == "high"
            assert not body.get("tools")
            chunk = {"id": "analysis", "object": "chat.completion.chunk", "created": 1,
                     "model": strong.model, "choices": [{"index": 0, "delta": {
                         "content": "Choose the earlier available interval.", "reasoning_content": "private"},
                         "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        async with AsyncOpenAI(api_key="offline", base_url="https://offline.invalid",
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))) as client:
            async with native_llm(model=strong.model, client=client, extra_body=strong.extra_body) as model:
                context = llm.ChatContext()
                context.add_message(role="user", content="Compare the options.")
                parts = []
                async with model.chat(chat_ctx=context, tools=[]) as stream:
                    async for chunk in stream:
                        if chunk.delta and chunk.delta.content:
                            parts.append(chunk.delta.content)
                assert "".join(parts) == "Choose the earlier available interval."
                assert len(requests) == 1
    asyncio.run(scenario())
