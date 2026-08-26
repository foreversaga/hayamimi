from __future__ import annotations

import re
from dataclasses import dataclass


_CJK_TARGETS = {"ja", "zh", "zh-Hant", "yue", "ko"}
_NONSPACE_TOKEN_RE = re.compile(r"\S+")


def _char_lcp(left: str, right: str) -> str:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def _token_lcp(left: str, right: str) -> str:
    left_tokens = list(_NONSPACE_TOKEN_RE.finditer(left))
    right_tokens = list(_NONSPACE_TOKEN_RE.finditer(right))
    matched = 0
    limit = min(len(left_tokens), len(right_tokens))
    while matched < limit:
        if left_tokens[matched].group(0) != right_tokens[matched].group(0):
            break
        matched += 1
    if matched == 0:
        return ""
    return left[: left_tokens[matched - 1].end()]


def longest_stable_prefix(previous: str, current: str, target_lang: str) -> str:
    if target_lang in _CJK_TARGETS:
        return _char_lcp(previous, current)
    return _token_lcp(previous, current)


@dataclass(frozen=True)
class AgreementSnapshot:
    committed: str
    speculative: str


class LocalAgreementPolicy:
    """Append-only emission using agreement between consecutive hypotheses."""

    def __init__(self, target_lang: str):
        self._target_lang = target_lang
        self._previous = ""
        self._committed = ""

    @property
    def committed(self) -> str:
        return self._committed

    def update(self, hypothesis: str) -> AgreementSnapshot:
        hypothesis = hypothesis.strip()
        if not hypothesis:
            return AgreementSnapshot(self._committed, "")

        if self._previous:
            stable = longest_stable_prefix(self._previous, hypothesis, self._target_lang)
            if stable.startswith(self._committed) and len(stable) > len(self._committed):
                self._committed = stable.rstrip()

        self._previous = hypothesis
        speculative = hypothesis[len(self._committed):].lstrip()
        return AgreementSnapshot(self._committed, speculative)

    def finalize(self, hypothesis: str) -> AgreementSnapshot:
        hypothesis = hypothesis.strip()
        if hypothesis:
            self._committed = hypothesis
            self._previous = hypothesis
        return AgreementSnapshot(self._committed, "")
