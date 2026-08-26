from __future__ import annotations

import threading
from collections import defaultdict, deque
from pathlib import Path

from .contracts import TranslationContext, TranslationRequest, TranslationUpdate
from .worker import StreamingTranslationWorker


def parse_targets(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    targets: list[str] = []
    for raw in value.split(","):
        target = raw.strip()
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return tuple(targets)


def load_glossary(path: str) -> tuple[tuple[str, str], ...]:
    """Load `source=target`, tab, or arrow-separated terminology pairs."""
    if not path:
        return ()

    pairs: list[tuple[str, str]] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for separator in ("\t", "=", "→"):
            if separator in line:
                source, target = line.split(separator, 1)
                source = source.strip()
                target = target.strip()
                if not source or not target:
                    raise ValueError(f"{path}:{line_no}: glossary terms must be non-empty")
                pairs.append((source, target))
                break
        else:
            raise ValueError(
                f"{path}:{line_no}: expected source=target, source<TAB>target, or source→target"
            )
    return tuple(pairs)


class RealtimeTranslationRuntime:
    """Bridges STT revisions to the model-independent streaming translator."""

    def __init__(
        self,
        coordinator,
        targets: tuple[str, ...],
        server=None,
        translate_partials: bool = True,
        context_lines: int = 4,
        glossary: tuple[tuple[str, str], ...] = (),
    ):
        if not targets:
            raise ValueError("at least one translation target is required")
        self.targets = targets
        self.translate_partials = translate_partials
        self._server = server
        self._context_lines = max(context_lines, 0)
        self._glossary = glossary
        self._lock = threading.Lock()
        self._revisions: dict[str, int] = defaultdict(int)
        self._last_partial: dict[str, str] = {}
        self._final_meta: dict[tuple[str, str, int], tuple[str, str]] = {}
        self._pending_final_targets: dict[tuple[str, int], int] = {}
        self._history: dict[str, deque[tuple[str, str]]] = {
            target: deque(maxlen=self._context_lines or 1) for target in targets
        }
        self._worker = StreamingTranslationWorker(coordinator, self._on_update)

    def submit_partial(self, segment_id: str, source_lang: str, text: str) -> None:
        text = text.strip()
        if not self.translate_partials or not text:
            return
        with self._lock:
            if self._last_partial.get(segment_id) == text:
                return
            self._last_partial[segment_id] = text
        self._submit(segment_id, source_lang, text, is_final=False, mode="partial")

    def submit_final(self, segment_id: str, source_lang: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._last_partial.pop(segment_id, None)
        self._submit(segment_id, source_lang, text, is_final=True, mode="final")

    def submit_refine(self, segment_id: str, source_lang: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._submit(segment_id, source_lang, text, is_final=True, mode="refine")

    def close(self, wait: bool = True) -> None:
        self._worker.close(wait=wait)

    def _submit(self, segment_id: str, source_lang: str, text: str, is_final: bool, mode: str) -> None:
        with self._lock:
            self._revisions[segment_id] += 1
            revision = self._revisions[segment_id]

        requests: list[TranslationRequest] = []
        for target in self.targets:
            if source_lang == target:
                continue
            requests.append(TranslationRequest(
                segment_id=segment_id,
                revision=revision,
                source_lang=source_lang or "auto",
                target_lang=target,
                text=text,
                is_final=is_final,
                context=self._build_context(target),
            ))

        if not requests:
            if is_final:
                with self._lock:
                    self._revisions.pop(segment_id, None)
            return

        if is_final:
            with self._lock:
                self._pending_final_targets[(segment_id, revision)] = len(requests)
                for request in requests:
                    self._final_meta[(segment_id, request.target_lang, revision)] = (text, mode)

        accepted_count = 0
        for request in requests:
            if self._worker.submit(request):
                accepted_count += 1
            elif is_final:
                with self._lock:
                    self._final_meta.pop((segment_id, request.target_lang, revision), None)
                    self._decrement_pending_final_locked(segment_id, revision)

        if is_final and accepted_count == 0:
            with self._lock:
                self._pending_final_targets.pop((segment_id, revision), None)
                self._revisions.pop(segment_id, None)

    def _build_context(self, target: str) -> TranslationContext:
        with self._lock:
            history = tuple(self._history[target]) if self._context_lines else ()
        return TranslationContext(history=history, glossary=self._glossary)

    def _on_update(self, update: TranslationUpdate) -> None:
        key = (update.segment_id, update.target_lang, update.revision)
        if update.is_final:
            with self._lock:
                source_text, mode = self._final_meta.pop(key, ("", "final"))
                self._decrement_pending_final_locked(update.segment_id, update.revision)
        else:
            source_text, mode = "", "partial"

        if not update.accepted:
            print(
                f"[translation/{update.target_lang}/{mode}] rejected: {update.error}",
                flush=True,
            )
            if self._server is not None:
                self._server.publish({
                    "type": "translation_error",
                    "segment_id": update.segment_id,
                    "lang": update.target_lang,
                    "revision": update.revision,
                    "mode": mode,
                    "error": update.error,
                })
            return

        if mode in ("final", "refine") and source_text and self._context_lines:
            with self._lock:
                self._history[update.target_lang].append((source_text, update.full_text))

        if mode == "partial":
            print(
                f"[→{update.target_lang}/partial] {update.committed}⟦{update.speculative}⟧ "
                f"({update.latency_ms:.0f}ms)",
                flush=True,
            )
            event_type = "translation_partial"
        elif mode == "refine":
            print(f"[refine→{update.target_lang}] {update.full_text}", flush=True)
            event_type = "translation_refine"
        else:
            print(
                f"[→{update.target_lang}/final] {update.full_text} ({update.latency_ms:.0f}ms)",
                flush=True,
            )
            event_type = "translation_final"

        if self._server is not None:
            self._server.publish({
                "type": event_type,
                "segment_id": update.segment_id,
                "lang": update.target_lang,
                "revision": update.revision,
                "committed": update.committed,
                "speculative": update.speculative,
                "text": update.full_text,
                "latency_ms": update.latency_ms,
            })

    def _decrement_pending_final_locked(self, segment_id: str, revision: int) -> None:
        key = (segment_id, revision)
        remaining = self._pending_final_targets.get(key)
        if remaining is None:
            return
        remaining -= 1
        if remaining <= 0:
            self._pending_final_targets.pop(key, None)
            self._revisions.pop(segment_id, None)
        else:
            self._pending_final_targets[key] = remaining
