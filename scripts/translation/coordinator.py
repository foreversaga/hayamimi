from __future__ import annotations

import time

from .contracts import TranslationBackend, TranslationRequest, TranslationUpdate
from .policies import LocalAgreementPolicy
from .protection import SensitiveSpanProtector
from .validation import TranslationValidator


class StreamingTranslationCoordinator:
    """Model-agnostic streaming translation state and safety layer.

    The coordinator is deliberately synchronous. The realtime pipeline should
    execute it on its translation worker thread so STT remains on the hot path.
    """

    def __init__(
        self,
        backend: TranslationBackend,
        protector: SensitiveSpanProtector | None = None,
        validator: TranslationValidator | None = None,
    ):
        self._backend = backend
        self._protector = protector or SensitiveSpanProtector()
        self._validator = validator or TranslationValidator()
        self._policies: dict[tuple[str, str], LocalAgreementPolicy] = {}

    def translate(self, request: TranslationRequest) -> TranslationUpdate:
        key = (request.segment_id, request.target_lang)
        policy = self._policies.setdefault(key, LocalAgreementPolicy(request.target_lang))
        protected = self._protector.protect(request.text)
        protected_request = TranslationRequest(
            segment_id=request.segment_id,
            revision=request.revision,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            text=protected.text,
            is_final=request.is_final,
            context=request.context,
        )

        started = time.perf_counter()
        try:
            translated = self._backend.translate(protected_request)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            return self._rejected(request, policy, latency_ms, str(exc))
        latency_ms = (time.perf_counter() - started) * 1000

        validation = self._validator.validate(request.text, translated, protected)
        if not validation.valid:
            return self._rejected(request, policy, latency_ms, validation.reason or "invalid translation")

        restored = protected.restore(translated).strip()
        snapshot = policy.finalize(restored) if request.is_final else policy.update(restored)
        if request.is_final:
            self._policies.pop(key, None)

        return TranslationUpdate(
            segment_id=request.segment_id,
            revision=request.revision,
            target_lang=request.target_lang,
            committed=snapshot.committed,
            speculative=snapshot.speculative,
            full_text=restored,
            is_final=request.is_final,
            accepted=True,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _rejected(
        request: TranslationRequest,
        policy: LocalAgreementPolicy,
        latency_ms: float,
        reason: str,
    ) -> TranslationUpdate:
        return TranslationUpdate(
            segment_id=request.segment_id,
            revision=request.revision,
            target_lang=request.target_lang,
            committed=policy.committed,
            speculative="",
            full_text=policy.committed,
            is_final=request.is_final,
            accepted=False,
            latency_ms=latency_ms,
            error=reason,
        )
