from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from .contracts import TranslationRequest, TranslationUpdate
from .coordinator import StreamingTranslationCoordinator


class _LatestWinsBuffer:
    def __init__(self):
        self._condition = threading.Condition()
        self._finals: deque[TranslationRequest] = deque()
        self._partials: dict[tuple[str, str], TranslationRequest] = {}
        self._closed = False

    def put(self, request: TranslationRequest) -> None:
        key = (request.segment_id, request.target_lang)
        with self._condition:
            if self._closed:
                return
            if request.is_final:
                self._partials.pop(key, None)
                self._finals.append(request)
            else:
                self._partials[key] = request
            self._condition.notify()

    def get(self) -> TranslationRequest | None:
        with self._condition:
            while not self._closed and not self._finals and not self._partials:
                self._condition.wait()
            if self._finals:
                return self._finals.popleft()
            if self._partials:
                key = next(iter(self._partials))
                return self._partials.pop(key)
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class StreamingTranslationWorker:
    """Background translator with final-first, latest-wins partial scheduling."""

    def __init__(
        self,
        coordinator: StreamingTranslationCoordinator,
        on_update: Callable[[TranslationUpdate], None],
    ):
        self._coordinator = coordinator
        self._on_update = on_update
        self._buffer = _LatestWinsBuffer()
        self._lock = threading.Lock()
        self._latest_revision: dict[tuple[str, str], int] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, request: TranslationRequest) -> None:
        key = (request.segment_id, request.target_lang)
        with self._lock:
            self._latest_revision[key] = max(
                request.revision,
                self._latest_revision.get(key, request.revision),
            )
        self._buffer.put(request)

    def close(self) -> None:
        self._buffer.close()

    def _run(self) -> None:
        while True:
            request = self._buffer.get()
            if request is None:
                return
            update = self._coordinator.translate(request)
            if self._is_stale(request):
                continue
            self._on_update(update)
            if request.is_final:
                key = (request.segment_id, request.target_lang)
                with self._lock:
                    self._latest_revision.pop(key, None)

    def _is_stale(self, request: TranslationRequest) -> bool:
        if request.is_final:
            return False
        key = (request.segment_id, request.target_lang)
        with self._lock:
            return self._latest_revision.get(key, request.revision) > request.revision
