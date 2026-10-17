"""Bound streaming TTS requests without delaying the first spoken text."""

from collections.abc import AsyncIterable, AsyncIterator

_SENTENCE_ENDINGS = frozenset(".!?;。！？；\n")


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _split_prefix(text: str, byte_limit: int) -> tuple[str, str]:
    """Split at a valid character boundary, preferring nearby sentence or word breaks."""
    size = 0
    end = 0
    for index, character in enumerate(text):
        character_size = _utf8_size(character)
        if size + character_size > byte_limit:
            break
        size += character_size
        end = index + 1

    if end == len(text):
        return text, ""
    if end == 0:
        return "", text

    preferred = 0
    for index, character in enumerate(text[:end], start=1):
        if character in _SENTENCE_ENDINGS:
            preferred = index
    if preferred < end // 2:
        for index, character in enumerate(text[:end], start=1):
            if character.isspace():
                preferred = index
    if preferred >= end // 2:
        end = preferred
    return text[:end], text[end:]


class StreamingTTSRequestSplitter:
    """Expose consecutive bounded request inputs over one original text stream."""

    def __init__(self, source: AsyncIterable[str], *, max_bytes: int) -> None:
        if max_bytes < 4:
            raise ValueError("The TTS request byte limit must be at least 4.")
        self._source = source.__aiter__()
        self._max_bytes = max_bytes
        self._pending = ""
        self._source_exhausted = False

    @property
    def exhausted(self) -> bool:
        return self._source_exhausted and not self._pending

    async def _next_text(self) -> str | None:
        if self._pending:
            text, self._pending = self._pending, ""
            return text
        if self._source_exhausted:
            return None
        try:
            return await anext(self._source)
        except StopAsyncIteration:
            self._source_exhausted = True
            return None

    async def request(self) -> AsyncIterator[str]:
        """Yield input for one provider request and retain overflow for the next one."""
        used = 0
        while used < self._max_bytes:
            text = await self._next_text()
            if text is None:
                return
            if not text:
                continue

            prefix, suffix = _split_prefix(text, self._max_bytes - used)
            if not prefix:
                self._pending = text
                return
            if suffix:
                self._pending = suffix
            yield prefix
            used += _utf8_size(prefix)
            if suffix or used >= self._max_bytes:
                return

