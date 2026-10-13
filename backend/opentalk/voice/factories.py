"""Agent composition boundary for replaceable business capabilities."""

from opentalk.agents.base import ConversationAgent


def agent_factory(agent_key="conversation"):
    if agent_key != "conversation":
        raise ValueError("Unknown agent type. Use conversation.")

    async def conversation(**options):
        return ConversationAgent(instructions="You are a helpful conversational assistant.", **options)
    return conversation
