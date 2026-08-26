"""Model-free regression tests for realtime translation v2."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from translation.contracts import TranslationRequest
from translation.coordinator import StreamingTranslationCoordinator
from translation.policies import LocalAgreementPolicy, longest_stable_prefix
from translation.protection import SensitiveSpanProtector
from translation.worker import _LatestWinsBuffer


class FakeBackend:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def translate(self, request):
        self.requests.append(request)
        return next(self.outputs)


def make_request(text, revision, final=False, target="zh-Hant"):
    return TranslationRequest(
        segment_id="seg-1",
        revision=revision,
        source_lang="ja",
        target_lang=target,
        text=text,
        is_final=final,
    )


def test_protection_roundtrip():
    protected = SensitiveSpanProtector().protect("vLLM 0.10.0 costs 500 and mail a@b.com")
    assert protected.text.count("__HAYAMIMI_KEEP_") == 3
    assert protected.restore(protected.text) == "vLLM 0.10.0 costs 500 and mail a@b.com"


def test_cjk_local_agreement_commits_common_prefix():
    policy = LocalAgreementPolicy("zh-Hant")
    first = policy.update("今天要介紹新的")
    assert first.committed == ""

    second = policy.update("今天要介紹一個新的模型")
    assert second.committed == "今天要介紹"
    assert second.speculative == "一個新的模型"


def test_latin_local_agreement_never_commits_partial_word():
    assert longest_stable_prefix("Today we intro", "Today we introduce", "en") == "Today we"


def test_coordinator_restores_protected_numbers():
    backend = FakeBackend(["價格是 __HAYAMIMI_KEEP_0000__ 元"])
    coordinator = StreamingTranslationCoordinator(backend)
    update = coordinator.translate(make_request("価格は500円です", 1, final=True))

    assert update.accepted
    assert update.full_text == "價格是 500 元"


def test_coordinator_rejects_dropped_placeholder():
    backend = FakeBackend(["價格是五百元"])
    coordinator = StreamingTranslationCoordinator(backend)
    update = coordinator.translate(make_request("価格は500円です", 1, final=True))

    assert not update.accepted
    assert "missing protected placeholders" in update.error
    assert coordinator._policies == {}


def test_latest_wins_buffer_coalesces_partial_revisions():
    buffer = _LatestWinsBuffer()
    buffer.put(make_request("draft 1", 1))
    buffer.put(make_request("draft 2", 2))

    request = buffer.get()
    assert request is not None
    assert request.revision == 2
    assert request.text == "draft 2"


def test_final_replaces_pending_partial_and_has_priority():
    buffer = _LatestWinsBuffer()
    buffer.put(make_request("draft", 1))
    buffer.put(make_request("final", 2, final=True))

    request = buffer.get()
    assert request is not None
    assert request.is_final
    assert request.revision == 2
