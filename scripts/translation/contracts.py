from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TranslationContext:
    history: tuple[tuple[str, str], ...] = ()
    glossary: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TranslationRequest:
    segment_id: str
    revision: int
    source_lang: str
    target_lang: str
    text: str
    is_final: bool
    context: TranslationContext = field(default_factory=TranslationContext)


@dataclass(frozen=True)
class TranslationUpdate:
    segment_id: str
    revision: int
    target_lang: str
    committed: str
    speculative: str
    full_text: str
    is_final: bool
    accepted: bool
    latency_ms: float
    error: str | None = None


class TranslationBackend(Protocol):
    def translate(self, request: TranslationRequest) -> str:
        """Return only the translated text for a single request."""
