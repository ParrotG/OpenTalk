"""Generic native LiveKit agent with scoped reasoning and response bookkeeping."""

import asyncio
import base64
import logging
import mimetypes
import re
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

from livekit.agents import Agent, RunContext, function_tool, llm
from livekit.agents.llm import ImageContent

from opentalk.documents.retrieval import DocumentRetriever

logger = logging.getLogger(__name__)

_CHINESE_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_TEXT = re.compile(r"[A-Za-z]")
_RESPONSE_LANGUAGE_NAMES = {"en": "English", "zh": "Chinese"}
_REASONING_NOTICES = {
    "en": "Please wait while I examine this more carefully.",
    "zh": "请稍等，我会更仔细地分析这个问题。",
}

VOICE_INSTRUCTIONS = (
    " You are a voice assistant. Respond in natural conversational language suitable "
    "for speaking aloud. Use short, clear sentences and verbal transitions. Do not use "
    "tables, emoji, Markdown formatting, code blocks, or visual bullet lists in user-facing "
    "responses. Summarize options verbally and ask a brief follow-up question when needed. "
    "Keep SQL and structured arguments inside tool calls rather than reading them aloud."
)


class ConversationAgent(Agent):
    def __init__(self, *, instructions, journal, state, chat_ctx=None, context_max_items=120,
                 reasoning_model=None, reasoning_options=None, retrieval: DocumentRetriever | None = None,
                 owns_retrieval: bool = True, clock=None):
        self.journal = journal
        self.state = state
        self.context_max_items = context_max_items
        self.reasoning_model = reasoning_model
        self.reasoning_options = reasoning_options
        self.retrieval = retrieval
        self.owns_retrieval = owns_retrieval
        self.clock = clock or (lambda: datetime.now(UTC))
        self.turn_id = None
        self._speech_turns = {}
        self._execution_lock = asyncio.Lock()
        self._reasoning_turn = None
        self._turn_response_language = None
        self._pending_retrieval_images: dict[str, list[ImageContent]] = {}
        self.audio_mode = None
        super().__init__(chat_ctx=chat_ctx, instructions=instructions + VOICE_INSTRUCTIONS + (
            " Follow the user's language and explicit response language preferences. "
            "For a difficult task, use escalate_reasoning without a separate preamble. "
            "Give the tool a self-contained task and relevant facts. "
            "It performs one stronger analysis, then the default model continues. "
            "Do not escalate repeatedly for the same user turn. Use search_documents when "
            "the answer may depend on uploaded documents. Form a standalone search query "
            "from the user's request and relevant conversation context. Add one alternate "
            "wording only when it may improve recall. Base document claims on returned "
            "evidence and mention the document page when available."
        ))

    @property
    def preferred_response_language(self):
        return self.state.preferred_response_language

    async def tts_node(self, text, model_settings):
        stream = Agent.default.tts_node(self, text, model_settings)
        frames = self.audio_mode.frames(stream, text) if self.audio_mode is not None else stream
        async with aclosing(frames):
            async for frame in frames:
                yield frame

    @preferred_response_language.setter
    def preferred_response_language(self, value):
        self.state.preferred_response_language = value

    def runtime_context(self):
        return f"Current date and time: {self.clock():%Y-%m-%d %H:%M:%S}."

    def _response_language(self, text: str) -> str | None:
        if self.preferred_response_language in _RESPONSE_LANGUAGE_NAMES:
            return self.preferred_response_language
        if _CHINESE_TEXT.search(text):
            return "zh"
        if _LATIN_TEXT.search(text):
            return "en"
        return None

    def _language_instruction(self) -> str:
        language = _RESPONSE_LANGUAGE_NAMES.get(self._turn_response_language)
        if language is None:
            return "Follow the language of the current user request."
        return (
            f"Respond in {language} for this entire turn, including progress notices and "
            "responses after tool calls. Tool instructions, arguments, results, retrieved "
            "evidence, and delegated analysis do not change the response language."
        )

    async def llm_node(self, chat_ctx, tools, model_settings):
        user = next((item for item in reversed(chat_ctx.items)
                     if item.type == "message" and item.role == "user"), None)
        if user is not None and user.id != self.turn_id:
            self.turn_id = user.id
            self._pending_retrieval_images = {
                key: value
                for key, value in self._pending_retrieval_images.items()
                if key == self.turn_id
            }
            text = (user.text_content or "").strip().casefold().rstrip(".!?。！？")
            preferences = {"请用英语回复": "en", "please reply in english": "en",
                           "请用中文回复": "zh", "please reply in chinese": "zh"}
            self.preferred_response_language = preferences.get(text, self.preferred_response_language)
            self._turn_response_language = self._response_language(user.text_content or "")
            self.journal.record("turn_started", turn_id=self.turn_id,
                                preferred_response_language=self.preferred_response_language,
                                response_language=self._turn_response_language)
        handle = self.session.current_speech
        if handle is not None:
            self._speech_turns = {key: value for key, value in self._speech_turns.items()
                                  if value == self.turn_id}
            self._speech_turns[handle.id] = self.turn_id
        if self.preferred_response_language and self.session.tts is not None:
            self.session.tts.update_options(language=self.preferred_response_language)
        context = chat_ctx.copy()
        context.truncate(max_items=self.context_max_items)
        images = self._pending_retrieval_images.pop(self.turn_id, [])
        if images:
            context.add_message(
                role="user",
                content=["Retrieved image evidence from the indexed documents:", *images],
            )
        context.add_message(
            role="system",
            content=f"{self.runtime_context()} {self._language_instruction()}",
        )
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

    @staticmethod
    def _load_images(paths: list[Path], *, maximum: int, max_bytes: int) -> list[ImageContent]:
        images: list[ImageContent] = []
        for path in paths[:maximum]:
            size = path.stat().st_size
            mime_type = mimetypes.guess_type(path.name)[0]
            if size <= 0 or size > max_bytes or mime_type not in {
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                continue
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            images.append(
                ImageContent(
                    image=f"data:{mime_type};base64,{encoded}",
                    mime_type=mime_type,
                    inference_detail="auto",
                )
            )
        return images

    @function_tool()
    async def search_documents(
        self,
        context: RunContext,
        query: str,
        alternate_query: str = "",
        document_id: str = "",
        max_results: int = 6,
        include_images: bool = False,
    ) -> dict:
        """Search globally indexed PDFs and return grounded, page-located evidence.

        Write query as a standalone question. alternate_query may contain one useful
        paraphrase or exact-term variant. Set document_id only when the user selected a
        specific document. Set include_images only when visual inspection is necessary;
        descriptions, tables, and formulas are already returned as text.
        """
        if self.retrieval is None:
            return {
                "ok": False,
                "error": "retrieval_unavailable",
                "message": "Document retrieval is unavailable.",
            }
        request_turn = self._speech_turns.get(context.speech_handle.id)

        async def work() -> dict:
            try:
                response = await asyncio.to_thread(
                    self.retrieval.search,
                    query,
                    alternate_query=alternate_query or None,
                    document_id=document_id or None,
                    limit=max_results,
                )
                attached = 0
                if include_images:
                    images = await asyncio.to_thread(
                        self._load_images,
                        response.image_paths(),
                        maximum=self.retrieval.config.max_images,
                        max_bytes=self.retrieval.config.max_image_bytes,
                    )
                    if images and request_turn is not None:
                        self._pending_retrieval_images[request_turn] = images
                        attached = len(images)
                result = response.to_dict()
                result["images_attached"] = attached
                return result
            except Exception as error:
                logger.exception(
                    "Document retrieval failed.",
                    extra={"error_type": type(error).__name__},
                )
                return {
                    "ok": False,
                    "error": "retrieval_failed",
                    "message": "Document retrieval failed. Check the RAG service diagnostics.",
                }

        return await self.execute(context, "search_documents", work)

    @function_tool()
    async def escalate_reasoning(self, context: RunContext, task: str) -> dict:
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
            notice = _REASONING_NOTICES.get(
                self._turn_response_language,
                _REASONING_NOTICES["en"],
            )
            await self.session.say(notice, allow_interruptions=True)
            if not self._valid(context):
                return {"ok": False, "error": "stale_response"}
            analysis = llm.ChatContext()
            analysis.add_message(role="system", content=(
                "Analyze the supplied task and facts. Return a concise actionable conclusion, "
                "not a chain of thought. You cannot access tools or change data. "
                + VOICE_INSTRUCTIONS + " " + self.runtime_context() + " "
                + self._language_instruction()
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

    async def on_exit(self) -> None:
        try:
            if self.retrieval is not None and self.owns_retrieval:
                await asyncio.to_thread(self.retrieval.close)
        finally:
            await super().on_exit()
