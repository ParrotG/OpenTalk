"""Agent composition boundary for replaceable business capabilities."""

from opentalk.agents.base import ConversationAgent
from opentalk.documents.retrieval import LazyRetrievalService

_DEFAULT_RETRIEVAL = object()


def agent_factory(agent_key="conversation", *, retrieval=_DEFAULT_RETRIEVAL):
    if agent_key != "conversation":
        raise ValueError("Unknown agent type. Use conversation.")
    if retrieval is _DEFAULT_RETRIEVAL:
        retrieval = LazyRetrievalService()

    async def conversation(**options):
        return ConversationAgent(
            instructions="You are a helpful conversational assistant.",
            retrieval=retrieval,
            **options,
        )
    return conversation
