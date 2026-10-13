"""Offline checks for configuration and the native streaming tool protocol."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from livekit.agents import llm
from livekit.plugins import openai as livekit_openai
from openai import AsyncOpenAI
from voice_fakes import tool_factory

from opentalk.llm import provider
from opentalk.voice.session import open_session


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


def test_fragmented_tool_stream_and_role_transcript(tmp_path):
    async def scenario():
        calls = []
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
                    chunk({"tool_calls": [{"index": 0, "id": "call-echo", "type": "function",
                                          "function": {"name": "echo", "arguments": '{"text":'}}]}),
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"hello"}'}}]}),
                    chunk({}, "tool_calls"),
                ]
            else:
                messages = body["messages"]
                assert any(message["role"] == "tool" and message["tool_call_id"] == "call-echo"
                           for message in messages)
                events = [chunk({"content": "Echo response "}), chunk({"content": "is ready."}),
                          chunk({}, "stop")]
            data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=data + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        async with AsyncOpenAI(api_key="offline-test-key", base_url="https://offline.invalid",
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))) as client:
            async with livekit_openai.LLM(model=config.model, client=client) as model:
                async with open_session(text_only=True, factory=tool_factory(calls),
                                        model=model) as (session, agent, journal):
                    await session.start(agent=agent)
                    await session.run(user_input="Echo hello.")
                    assert session.history.items[-1].text_content == "Echo response is ready."
                    await journal.save()
                    await journal.save()
                    history = journal.store.history(session.userdata.session_id)["items"]
                    assert calls == ["hello"]
                    stored_calls = [item for item in history if item["type"] == "function_call"]
                    assert len(stored_calls) == 1 and stored_calls[0]["name"] == "echo"
                    assert json.loads(stored_calls[0]["arguments"]) == {"text": "hello"}
                    roles = [item["role"] for item in history if item["type"] == "message"]
                    assert roles == ["user", "assistant"]
                    assert "offline-test-key" not in json.dumps(history)
                assert len(requests) == 2
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
