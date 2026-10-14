"""Deterministic multilingual lexical vectors for hybrid document retrieval."""

import hashlib
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _runs(text: str) -> list[tuple[bool, str]]:
    runs: list[tuple[bool, str]] = []
    current: list[str] = []
    current_is_han: bool | None = None
    for character in text:
        if _is_han(character):
            is_han = True
        elif character.isalnum():
            is_han = False
        else:
            if current:
                runs.append((bool(current_is_han), "".join(current)))
                current = []
                current_is_han = None
            continue
        if current and is_han != current_is_han:
            runs.append((bool(current_is_han), "".join(current)))
            current = []
        current_is_han = is_han
        current.append(character)
    if current:
        runs.append((bool(current_is_han), "".join(current)))
    return runs


def lexical_tokens(text: str) -> list[str]:
    """Tokenize Unicode text without assuming one document language."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for is_han, value in _runs(normalized):
        if is_han:
            tokens.extend(value)
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens


def _token_index(token: str) -> int:
    return int.from_bytes(
        hashlib.blake2s(token.encode("utf-8"), digest_size=4).digest(),
        "little",
    )


def encode_lexical(text: str) -> SparseVector:
    """Encode token frequencies into stable Qdrant sparse-vector dimensions."""
    counts: Counter[int] = Counter(_token_index(token) for token in lexical_tokens(text))
    indices = sorted(counts)
    return SparseVector(
        indices=indices,
        values=[1.0 + math.log(counts[index]) for index in indices],
    )
