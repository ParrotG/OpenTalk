"""Run an actual streamed request through LiveKit without a room or AgentSession."""

import asyncio
import json
import time

from livekit.agents import llm

from opentalk.llm.provider import create_llm, load_llm_config


async def run_smoke() -> dict:
    config = load_llm_config()
    context = llm.ChatContext()
    context.add_message(role="system", content="Answer concisely in English.")
    context.add_message(
        role="user",
        content="Explain in two short sentences why meeting rooms need reservations.",
    )
    chunks = []
    usage = None
    first_content_seconds = None
    started = time.perf_counter()
    async with create_llm(config) as model:
        async with model.chat(
            chat_ctx=context, conn_options=config.connection_options, tool_choice="none",
        ) as stream:
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                if chunk.delta and chunk.delta.content:
                    if first_content_seconds is None:
                        first_content_seconds = time.perf_counter() - started
                    chunks.append(chunk.delta.content)
    if len(chunks) < 2 or not "".join(chunks).strip():
        raise RuntimeError("The smoke request did not produce multiple non-empty text chunks.")
    return {
        "status": "passed", "model": config.model, "content_chunks": len(chunks),
        "first_content_seconds": first_content_seconds,
        "total_seconds": time.perf_counter() - started,
        "response": "".join(chunks), "usage": usage,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, indent=2))
