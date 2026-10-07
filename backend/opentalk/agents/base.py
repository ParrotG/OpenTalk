"""Generic native LiveKit agent with scoped reasoning and response bookkeeping."""

import asyncio
from datetime import UTC, datetime

from livekit.agents import Agent, RunContext, function_tool, llm


VOICE_INSTRUCTIONS = (
    " You are a voice assistant. Respond in natural conversational language suitable "
    "for speaking aloud. Use short, clear sentences and verbal transitions. Do not use "
    "tables, emoji, Markdown formatting, code blocks, or visual bullet lists in user-facing "
    "responses. Summarize options verbally and ask a brief follow-up question when needed. "
    "Keep SQL and structured arguments inside tool calls rather than reading them aloud."
)


class ConversationAgent(Agent):
    def __init__(self, *, instructions, journal, state, chat_ctx=None, context_max_items=120,
                 reasoning_model=None, reasoning_options=None, clock=None):
        self.journal = journal
        self.state = state
        self.context_max_items = context_max_items
        self.reasoning_model = reasoning_model
        self.reasoning_options = reasoning_options
        self.clock = clock or (lambda: datetime.now(UTC))
        self.turn_id = None
        self._speech_turns = {}
        self._execution_lock = asyncio.Lock()
        self._reasoning_turn = None
        super().__init__(chat_ctx=chat_ctx, instructions=instructions + VOICE_INSTRUCTIONS + (
            " Follow the user's language and explicit response language preferences. "
            "For a difficult task, use escalate_reasoning after briefly telling the user "
            "you are checking it. Give the tool a self-contained task and relevant facts. "
            "It performs one stronger analysis, then the default model continues. "
            "Do not escalate repeatedly for the same user turn."
        ))

    @property
    def preferred_response_language(self):
        return self.state.preferred_response_language

    @preferred_response_language.setter
    def preferred_response_language(self, value):
        self.state.preferred_response_language = value

    def runtime_context(self):
        return f"Current date and time: {self.clock():%Y-%m-%d %H:%M:%S}."

    async def llm_node(self, chat_ctx, tools, model_settings):
        user = next((item for item in reversed(chat_ctx.items)
                     if item.type == "message" and item.role == "user"), None)
        if user is not None and user.id != self.turn_id:
            self.turn_id = user.id
            text = (user.text_content or "").strip().casefold().rstrip(".!?。！？")
            preferences = {"请用英语回复": "en", "please reply in english": "en",
                           "请用中文回复": "zh", "please reply in chinese": "zh"}
            self.preferred_response_language = preferences.get(text, self.preferred_response_language)
            self.journal.record("turn_started", turn_id=self.turn_id,
                                preferred_response_language=self.preferred_response_language)
        handle = self.session.current_speech
        if handle is not None:
            self._speech_turns = {key: value for key, value in self._speech_turns.items()
                                  if value == self.turn_id}
            self._speech_turns[handle.id] = self.turn_id
        if self.preferred_response_language and self.session.tts is not None:
            self.session.tts.update_options(language=self.preferred_response_language)
        context = chat_ctx.copy()
        context.truncate(max_items=self.context_max_items)
        context.add_message(role="system", content=(self.runtime_context() +
                            f" Reply language preference: {self.preferred_response_language or 'follow the user'}."))
        async for chunk in Agent.default.llm_node(self, context, tools, model_settings):
            yield chunk

    def _valid(self, context: RunContext):
        return (not context.speech_handle.interrupted
                and self._speech_turns.get(context.speech_handle.id) == self.turn_id)

    async def execute(self, context: RunContext, name, action):
        async def work():
            async with self._execution_lock:
                if not self._valid(context):
                    return {"ok": False, "error": "stale_response", "message": "The response is no longer current."}
                turn = self._speech_turns[context.speech_handle.id]
                result = await action()
                self.journal.record("business_result", turn_id=turn,
                                    response_id=context.speech_handle.id, call_id=context.function_call.call_id,
                                    name=name, result={key: result[key] for key in
                                                     ("ok", "error", "operation_id", "replayed") if key in result})
                return result
        task = asyncio.create_task(work())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Retain the result of any database transaction that has already started.
            await asyncio.shield(task)
            raise

    @function_tool()
    async def escalate_reasoning(self, context: RunContext, task: str,
                                 message: str = "Please wait while I check this more carefully.") -> dict:
        """Briefly notify the user and perform one stronger, tool-free analysis.

        Supply a self-contained task with relevant query results and constraints.
        The default model resumes after this call. Do not request database edits here.
        """
        if not self._valid(context):
            return {"ok": False, "error": "stale_response"}
        if self.reasoning_model is None:
            return {"ok": False, "error": "reasoning_unavailable"}
        if self._reasoning_turn == self.turn_id:
            return {"ok": False, "error": "already_escalated", "message": "Use the existing analysis for this turn."}
        self._reasoning_turn = self.turn_id
        turn = self.turn_id
        self.journal.record("reasoning_started", turn_id=turn)
        try:
            await self.session.say(message, allow_interruptions=True)
            if not self._valid(context):
                return {"ok": False, "error": "stale_response"}
            analysis = llm.ChatContext()
            analysis.add_message(role="system", content=(
                "Analyze the supplied task and facts. Return a concise actionable conclusion, "
                "not a chain of thought. You cannot access tools or change data. "
                + VOICE_INSTRUCTIONS + " " + self.runtime_context()
            ))
            analysis.add_message(role="user", content=task)
            parts = []
            async with self.reasoning_model.chat(chat_ctx=analysis, tools=[],
                                                 conn_options=self.reasoning_options) as stream:
                async for chunk in stream:
                    if not self._valid(context):
                        return {"ok": False, "error": "stale_response"}
                    if chunk.usage:
                        self.journal.record("reasoning_usage", turn_id=turn,
                                            prompt_tokens=chunk.usage.prompt_tokens,
                                            completion_tokens=chunk.usage.completion_tokens,
                                            total_tokens=chunk.usage.total_tokens)
                    if chunk.delta and chunk.delta.content:
                        parts.append(chunk.delta.content)
            if not parts:
                return {"ok": False, "error": "empty_analysis"}
            return {"ok": True, "analysis": "".join(parts)}
        finally:
            self.journal.record("reasoning_finished", turn_id=turn, active_profile="default")
