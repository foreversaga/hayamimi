from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..contracts import TranslationRequest
from ..protection import SensitiveSpanProtector


_TARGET_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "bn": "Bengali",
    "bo": "Tibetan",
    "cs": "Czech",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fr": "French",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "my": "Burmese",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tl": "Filipino",
    "tr": "Turkish",
    "ug": "Uyghur",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
    "zh-Hant": "Traditional Chinese",
}


class TranslationBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str = "http://127.0.0.1:8000"
    model: str = "tencent/Hy-MT2-1.8B"
    timeout_s: float = 5.0
    temperature: float = 0.0
    repetition_penalty: float = 1.05
    max_tokens: int = 512


class HyMT2OpenAIBackend:
    """Hy-MT2 backend served by a vLLM/SGLang OpenAI-compatible API."""

    def __init__(self, config: OpenAICompatibleConfig | None = None):
        self._config = config or OpenAICompatibleConfig()

    def translate(self, request: TranslationRequest) -> str:
        prompt = self._build_prompt(request)
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            # Hy-MT2 publishes a sampling recipe. Streaming Local Agreement is
            # more stable with deterministic decoding, so temperature=0 is the
            # realtime default while keeping the official repetition penalty.
            "temperature": self._config.temperature,
            "repetition_penalty": self._config.repetition_penalty,
            "max_tokens": self._config.max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            self._endpoint(),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._config.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TranslationBackendError(f"translation request failed: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationBackendError("translation server returned an invalid response") from exc
        if not isinstance(content, str):
            raise TranslationBackendError("translation server returned non-text content")
        return content.strip()

    def _endpoint(self) -> str:
        return self._config.base_url.rstrip("/") + "/v1/chat/completions"

    @staticmethod
    def _build_prompt(request: TranslationRequest) -> str:
        target_name = _TARGET_LANGUAGE_NAMES.get(request.target_lang, request.target_lang)
        sections: list[str] = []

        if request.context.glossary:
            glossary_lines = [
                f"{source} translates to {target}"
                for source, target in request.context.glossary
            ]
            sections.append("Reference the following translations:\n" + "\n".join(glossary_lines))

        if request.context.history:
            history_lines = [
                f"Source: {source}\nTranslation: {target}"
                for source, target in request.context.history[-4:]
            ]
            sections.append(
                "Use the following recent subtitle context only to preserve terminology, names, "
                "pronouns and meaning. Do not translate or repeat the context itself:\n"
                + "\n\n".join(history_lines)
            )

        if SensitiveSpanProtector.contains_placeholder(request.text):
            sections.append(
                "The source contains immutable placeholders such as __HAYAMIMI_KEEP_0000__. "
                "Preserve each placeholder exactly once. Do not alter, omit or translate it."
            )

        sections.append(
            f"Translate the following text into {target_name}. Note that you should ONLY output "
            "the translated result without any additional explanation:\n\n"
            f"{request.text}"
        )
        return "\n\n".join(sections)
