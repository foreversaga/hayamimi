from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


_PLACEHOLDER_PREFIX = "__HAYAMIMI_KEEP_"
_PLACEHOLDER_RE = re.compile(r"__HAYAMIMI_KEEP_\d{4}__")

# Order matters: URLs and emails must be captured before technical identifiers
# and their numeric parts. Mixed identifiers cover model/product tokens such as
# Qwen3.8, RTX5070Ti, H3 and CUDA12.9 without freezing ordinary prose.
_PROTECTED_SPAN_RE = re.compile(
    r"https?://[^\s<>]+"
    r"|www\.[^\s<>]+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.+-]*\d[A-Za-z0-9_.+-]*(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])\d+[A-Za-z][A-Za-z0-9_.+-]*(?![A-Za-z0-9_])"
    r"|\b[vV]?\d+(?:\.\d+){1,4}(?:[-+][A-Za-z0-9._-]+)?\b"
    r"|(?<![A-Za-z0-9_])[+-]?\d+(?:[.,]\d+)*(?:%|％)?(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class ProtectedText:
    text: str
    replacements: tuple[tuple[str, str], ...]

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(placeholder for placeholder, _ in self.replacements)

    def restore(self, translated: str) -> str:
        restored = translated
        for placeholder, original in self.replacements:
            restored = restored.replace(placeholder, original)
        return restored

    def placeholder_mismatch(self, translated: str) -> str | None:
        """Return an error when placeholders are missing, duplicated or invented."""
        expected = Counter(self.placeholders)
        found = Counter(_PLACEHOLDER_RE.findall(translated))
        if expected == found:
            return None

        missing = list((expected - found).elements())
        extra = list((found - expected).elements())
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra/duplicated: " + ", ".join(extra))
        return "; ".join(details) or "placeholder count mismatch"

    def missing_placeholders(self, translated: str) -> tuple[str, ...]:
        found = Counter(_PLACEHOLDER_RE.findall(translated))
        expected = Counter(self.placeholders)
        return tuple((expected - found).elements())


class SensitiveSpanProtector:
    """Protect immutable spans that translation models commonly corrupt."""

    def protect(self, text: str) -> ProtectedText:
        replacements: list[tuple[str, str]] = []

        def replace(match: re.Match[str]) -> str:
            placeholder = f"{_PLACEHOLDER_PREFIX}{len(replacements):04d}__"
            replacements.append((placeholder, match.group(0)))
            return placeholder

        protected = _PROTECTED_SPAN_RE.sub(replace, text)
        return ProtectedText(protected, tuple(replacements))

    @staticmethod
    def contains_placeholder(text: str) -> bool:
        return _PLACEHOLDER_RE.search(text) is not None
