from __future__ import annotations

import re
from dataclasses import dataclass

from .protection import ProtectedText


_REPEATED_TOKEN_RE = re.compile(r"(?:^|\s)(\S+)(?:\s+\1){5,}(?:\s|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None


class TranslationValidator:
    def __init__(self, max_length_ratio: float = 6.0):
        self._max_length_ratio = max_length_ratio

    def validate(self, source: str, translated: str, protected: ProtectedText) -> ValidationResult:
        candidate = translated.strip()
        if not candidate:
            return ValidationResult(False, "empty translation")

        missing = protected.missing_placeholders(candidate)
        if missing:
            return ValidationResult(False, f"missing protected placeholders: {', '.join(missing)}")

        source_len = max(len(source.strip()), 1)
        if len(candidate) > source_len * self._max_length_ratio + 64:
            return ValidationResult(False, "translation length is implausibly large")

        if _REPEATED_TOKEN_RE.search(candidate):
            return ValidationResult(False, "translation contains a repetition loop")

        return ValidationResult(True)
