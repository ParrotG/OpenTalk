import asyncio
from types import SimpleNamespace

from livekit.agents.llm import find_function_tools

from opentalk.agents.base import ConversationAgent


class Response:
    @staticmethod
    def to_dict():
        return {
            "ok": True,
            "queries": ["standalone query", "exact term"],
            "result_count": 1,
            "evidence": [{"context_text": "Grounded evidence.", "page_numbers": [2]}],
        }

    @staticmethod
    def image_paths():
        return []


class Retriever:
    config = SimpleNamespace(max_images=2, max_image_bytes=1024)

    def __init__(self):
        self.calls = []
        self.closed = False

    def search(self, query, **options):
        self.calls.append((query, options))
        return Response()

    def close(self):
        self.closed = True


class AccessResponse:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return {"ok": True, **self.value}

    @staticmethod
    def image_paths():
        return []


class Accessor:
    config = SimpleNamespace(max_images=2, max_image_bytes=1024)

    def __init__(self):
        self.calls = []

    def inspect(self, document_id, **options):
        self.calls.append(("inspect", document_id, options))
        return AccessResponse({"view": options["view"], "items": []})

    def find_text(self, document_id, query, **options):
        self.calls.append(("find", document_id, query, options))
        return AccessResponse({"query": query, "matches": []})


class Journal:
    def __init__(self):
        self.events = []

    def record(self, event_type, **values):
        self.events.append({"type": event_type, **values})


def context_for(agent, turn="current", response="response"):
    agent.turn_id = turn
    agent._speech_turns[response] = turn
    return SimpleNamespace(
        speech_handle=SimpleNamespace(id=response, interrupted=False),
        function_call=SimpleNamespace(call_id="call"),
    )


def test_document_retrieval_tool_returns_grounded_evidence_and_closes(monkeypatch):
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    retriever = Retriever()
    journal = Journal()
    agent = ConversationAgent(
        instructions="Test agent.",
        journal=journal,
        state=SimpleNamespace(preferred_response_language=None),
        retrieval=retriever,
    )

    async def scenario():
        result = await agent.search_documents(
            context_for(agent),
            query="standalone query",
            alternate_query="exact term",
            max_results=4,
        )
        assert result["ok"] and result["evidence"][0]["page_numbers"] == [2]
        assert retriever.calls == [
            (
                "standalone query",
                {
                    "alternate_query": "exact term",
                    "document_id": None,
                    "limit": 4,
                },
            )
        ]
        assert any(
            event["type"] == "business_result"
            and event["name"] == "search_documents"
            and event["result"]["ok"]
            for event in journal.events
        )
        await agent.on_exit()

    asyncio.run(scenario())
    assert retriever.closed


def test_conversation_agent_exposes_retrieval_and_reasoning_tools():
    assert {tool.info.name for tool in find_function_tools(ConversationAgent)} == {
        "escalate_reasoning",
        "find_document_text",
        "inspect_document",
        "search_documents",
    }


def test_structured_document_tools_forward_bounded_queries(monkeypatch):
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    accessor = Accessor()
    agent = ConversationAgent(
        instructions="Test agent.",
        journal=Journal(),
        state=SimpleNamespace(preferred_response_language=None),
        document_access=accessor,
    )

    async def scenario():
        inspected = await agent.inspect_document(
            context_for(agent),
            document_id="document-1",
            view="page",
            page_start=3,
            cursor="1:20",
        )
        found = await agent.find_document_text(
            context_for(agent),
            document_id="document-1",
            query="known phrase",
            match_mode="substring",
            section_id="section-1",
            max_results=4,
        )
        assert inspected["ok"] and inspected["images_attached"] == 0
        assert found["ok"] and found["images_attached"] == 0

    asyncio.run(scenario())
    assert accessor.calls == [
        (
            "inspect",
            "document-1",
            {
                "view": "page",
                "section_id": None,
                "parent_section_id": None,
                "section_scope": "direct",
                "page_start": 3,
                "page_end": None,
                "cursor": "1:20",
            },
        ),
        (
            "find",
            "document-1",
            "known phrase",
            {
                "match_mode": "substring",
                "section_id": "section-1",
                "page_start": None,
                "page_end": None,
                "limit": 4,
            },
        ),
    ]


def test_shared_retrieval_is_not_closed_by_agent():
    retriever = Retriever()
    agent = ConversationAgent(
        instructions="Test agent.",
        journal=Journal(),
        state=SimpleNamespace(preferred_response_language=None),
        retrieval=retriever,
        owns_retrieval=False,
    )

    asyncio.run(agent.on_exit())

    assert not retriever.closed


def test_retrieved_image_is_converted_to_livekit_multimodal_content(tmp_path):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"png-fixture")

    images = ConversationAgent._load_images(
        [image_path],
        maximum=1,
        max_bytes=1024,
    )

    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].image.startswith("data:image/png;base64,")
