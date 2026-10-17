"""Agent composition boundary for replaceable business capabilities."""

from opentalk.agents.base import ConversationAgent
from opentalk.documents.access import DocumentAccessService
from opentalk.documents.retrieval import LazyRetrievalService

_DEFAULT_RETRIEVAL = object()
_DEFAULT_DOCUMENT_ACCESS = object()


def agent_factory(
    agent_key="conversation",
    *,
    retrieval=_DEFAULT_RETRIEVAL,
    document_access=_DEFAULT_DOCUMENT_ACCESS,
    owns_retrieval: bool | None = None,
):
    if agent_key != "conversation":
        raise ValueError("Unknown agent type. Use conversation.")
    if retrieval is _DEFAULT_RETRIEVAL:
        retrieval = LazyRetrievalService()
        owns_retrieval = True if owns_retrieval is None else owns_retrieval
    elif owns_retrieval is None:
        owns_retrieval = False
    if document_access is _DEFAULT_DOCUMENT_ACCESS:
        document_access = DocumentAccessService()

    async def conversation(**options):
        return ConversationAgent(
            instructions="You are a helpful conversational assistant.",
            retrieval=retrieval,
            document_access=document_access,
            owns_retrieval=owns_retrieval,
            **options,
        )

    return conversation
