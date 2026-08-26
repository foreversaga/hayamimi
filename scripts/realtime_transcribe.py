"""Real-time (or simulated real-time) multilingual transcription pipeline.

Audio -> Silero VAD -> RoutedASR -> optional streaming translation -> UI/console.
"""
import argparse
import os
import queue
import re as _re
import sys
import threading
import time
import wave

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import sherpa_onnx

from asr_engine import RoutedASR, script_corrected_lang

SAMPLE_RATE = 16000
WINDOW_SIZE = 512  # samples per VAD chunk, ~32ms @ 16kHz
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
VAD_MODEL = os.path.join(MODELS_DIR, "silero_vad.onnx")


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    duration = len(samples) / src_rate
    dst_n = int(round(duration * dst_rate))
    src_x = np.arange(len(samples)) / src_rate
    dst_x = np.arange(dst_n) / dst_rate
    return np.interp(dst_x, src_x, samples).astype(np.float32)


def read_wave(path: str, target_rate: int = SAMPLE_RATE):
    with wave.open(path, "rb") as f:
        assert f.getsampwidth() == 2, f"{path}: expected 16-bit PCM"
        num_channels = f.getnchannels()
        sample_rate = f.getframerate()
        data = f.readframes(f.getnframes())
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels).mean(axis=1)
    if sample_rate != target_rate:
        samples = resample_linear(samples, sample_rate, target_rate)
        sample_rate = target_rate
    return samples, sample_rate


def build_vad(min_silence: float = 0.35,
              max_speech: float = 12.0) -> sherpa_onnx.VoiceActivityDetector:
    cfg = sherpa_onnx.VadModelConfig(
        silero_vad=sherpa_onnx.SileroVadModelConfig(
            model=VAD_MODEL,
            min_silence_duration=min_silence,
            min_speech_duration=0.25,
            window_size=WINDOW_SIZE,
            max_speech_duration=max_speech,
        ),
        sample_rate=SAMPLE_RATE,
        num_threads=1,
    )
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)


def wav_chunks(samples: np.ndarray, sample_rate: int, realtime: bool):
    pos = 0
    n = len(samples)
    start = time.perf_counter()
    while pos < n:
        chunk = samples[pos:pos + WINDOW_SIZE]
        if len(chunk) < WINDOW_SIZE:
            chunk = np.pad(chunk, (0, WINDOW_SIZE - len(chunk)))
        if realtime:
            delay = start + pos / sample_rate - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        yield chunk
        pos += WINDOW_SIZE


def mic_chunks():
    import sounddevice as sd

    q: "queue.Queue[np.ndarray]" = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"audio input status: {status}", file=sys.stderr)
        q.put(indata[:, 0].copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=WINDOW_SIZE,
        callback=callback,
    ):
        while True:
            yield q.get()


PARTIAL_EVERY_S = 0.5
PARTIAL_WINDOW_S = 8.0


class PartialPrinter:
    """Shows in-progress drafts; overwrites in place on a tty, one line otherwise."""

    def __init__(self, enabled: bool, server=None):
        self.enabled = enabled
        self.server = server
        self._tty = sys.stdout.isatty()
        self._last_len = 0

    def show(self, text: str, segment_id: str = "", lang: str = ""):
        if not self.enabled or not text:
            return
        if self.server is not None:
            self.server.partial(text, segment_id=segment_id, lang=lang)
        if self._tty:
            pad = max(self._last_len - len(text), 0)
            print("\r~ " + text + " " * pad, end="", flush=True)
            self._last_len = len(text)
        else:
            print(f"~ {text}")

    def clear(self):
        if self.enabled and self._tty and self._last_len:
            print("\r" + " " * (self._last_len + 2) + "\r", end="", flush=True)
            self._last_len = 0


class SessionStats:
    def __init__(self):
        self.total_audio_s = 0.0
        self.segments = 0
        self.latencies_ms: list[float] = []

    def summary(self) -> str:
        if not self.latencies_ms:
            return f"total_audio={self.total_audio_s:.1f}s segments=0"
        mean = sum(self.latencies_ms) / len(self.latencies_ms)
        return (
            f"total_audio={self.total_audio_s:.1f}s segments={self.segments} "
            f"mean_latency={mean:.0f}ms max_latency={max(self.latencies_ms):.0f}ms"
        )


PREROLL_S = 1.0


class AudioHistory:
    """Rolling buffer of recent audio so finals can include pre-onset context."""

    def __init__(self, sample_rate: int, keep_s: float = 30.0):
        self.sr = sample_rate
        self.keep = int(keep_s * sample_rate)
        self.buf = np.zeros(0, dtype=np.float32)
        self.offset = 0
        self.last_seg_end = 0

    def push(self, chunk: np.ndarray):
        self.buf = np.concatenate([self.buf, chunk])
        if len(self.buf) > self.keep:
            drop = len(self.buf) - self.keep
            self.buf = self.buf[drop:]
            self.offset += drop

    def with_preroll(self, seg_start: int, seg_samples: np.ndarray) -> np.ndarray:
        want = max(seg_start - int(PREROLL_S * self.sr), self.last_seg_end, self.offset)
        pre = self.buf[want - self.offset:seg_start - self.offset]
        self.last_seg_end = seg_start + len(seg_samples)
        if len(pre) == 0:
            return seg_samples
        return np.concatenate([pre, seg_samples])


def drain_segments(
    vad,
    sample_rate: int,
    asr: RoutedASR,
    stats: SessionStats,
    printer: PartialPrinter,
    history: AudioHistory | None = None,
    known_lang: str | None = None,
    spans_out: list | None = None,
    translator_worker: "LegacyTranslationWorker | None" = None,
    realtime_translator=None,
    speaker_labeler=None,
) -> int:
    drained = 0
    while not vad.empty():
        segment = vad.front
        seg_end_time = time.perf_counter()
        samples = np.asarray(segment.samples, dtype=np.float32)
        seg_start, seg_end = segment.start, segment.start + len(samples)
        segment_id = f"seg-{int(seg_start)}"
        if history is not None:
            samples = history.with_preroll(seg_start, samples)
        vad.pop()
        drained += 1

        seg_s = len(samples) / sample_rate
        raw_speech_s = (seg_end - seg_start) / sample_rate
        result = asr.transcribe(
            samples,
            sample_rate,
            known_lang=known_lang if drained == 1 else None,
            speech_s=raw_speech_s,
        )
        latency_ms = (time.perf_counter() - seg_end_time) * 1000

        if not result["text"].strip():
            continue

        speaker = ""
        if speaker_labeler is not None:
            speaker = speaker_labeler.label(samples, sample_rate) + "|"

        stats.segments += 1
        stats.latencies_ms.append(latency_ms)
        printer.clear()
        if printer.server is not None:
            printer.server.final(
                result["text"],
                result["lang"],
                speaker.rstrip("|"),
                latency_ms,
                result.get("tier", ""),
                segment_id=segment_id,
            )
        print(
            f"[{speaker}{result['lang']}/{result.get('tier', '?')}] {result['text']}  "
            f"(seg={seg_s:.1f}s, lid={result['lid_ms']:.0f}ms, "
            f"decode={result['decode_ms']:.0f}ms, latency={latency_ms:.0f}ms)"
        )

        if translator_worker is not None and result["lang"] == "ja":
            translator_worker.submit(result["text"])
        if realtime_translator is not None:
            realtime_translator.submit_final(segment_id, result["lang"], result["text"])
        if spans_out is not None:
            spans_out.append(
                (seg_start, seg_end, result["lang"], result["text"], speaker.rstrip("|"))
            )
    return drained


def digits_consistent(src: str, out: str) -> bool:
    """Every ASCII digit run in the source must survive legacy translation."""
    src_runs = _re.findall(r"\d+", src)
    if not src_runs:
        return True
    out_runs = set(_re.findall(r"\d+", out))
    return all(run in out_runs for run in src_runs)


def safe_translate(translator, text: str) -> str:
    out = translator.translate(text)
    if out != text and not digits_consistent(text, out):
        return text
    return out


def translate_by_sentence(translator, text: str) -> str:
    sentences = [s for s in _re.split(r"(?<=[。！？!?])\s*", text) if s.strip()]
    out = []
    for sentence in sentences:
        translated = safe_translate(translator, sentence)
        if translated != sentence:
            out.append(translated)
    return " ".join(out)


def build_translators(langs: str) -> dict:
    """Build the legacy ja->en/zh/ko CTranslate2 translators."""
    out = {}
    for lang in [x.strip() for x in langs.split(",") if x.strip()]:
        if lang == "en":
            from translate_ja_en import TranslatorJaEn

            out["en"] = TranslatorJaEn()
        elif lang in ("zh", "ko"):
            from translate_m2m import TranslatorM2M

            out[lang] = TranslatorM2M(lang)
        else:
            print(f"legacy backend does not support target: {lang}", file=sys.stderr)
    return out


class LegacyTranslationWorker:
    """Compatibility worker for the original finalized Japanese translations."""

    def __init__(self, translators: dict, server=None):
        self._translators = translators
        self._server = server
        self._q: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, text: str):
        self._q.put(text)

    def _run(self):
        while True:
            text = self._q.get()
            for lang, translator in self._translators.items():
                out = safe_translate(translator, text)
                if out != text:
                    print(f"[→{lang}] {out}")
                    if self._server is not None:
                        self._server.publish({"type": "translation", "lang": lang, "text": out})


GROUP_GAP_S = 2.0
GROUP_MAX_S = 25.0


class Refiner:
    """Second pass: re-decode a whole utterance group after the speaker pauses."""

    def __init__(
        self,
        asr: RoutedASR,
        history: AudioHistory,
        sample_rate: int,
        printer: PartialPrinter,
        transcript_path: str | None = None,
        translators: dict | None = None,
        realtime_translator=None,
    ):
        self.asr = asr
        self.history = history
        self.sr = sample_rate
        self.printer = printer
        self.translators = translators or {}
        self.realtime_translator = realtime_translator
        self.spans: list[tuple[int, int, str, str, str]] = []
        self._transcript = (
            open(transcript_path, "a", encoding="utf-8") if transcript_path else None
        )
        self._worker_lock = threading.Lock()

    def maybe_refine(self, now_sample: int, force: bool = False):
        if not self.spans:
            return
        first_start = self.spans[0][0]
        last_end = self.spans[-1][1]
        due = (
            force
            or now_sample - last_end >= int(GROUP_GAP_S * self.sr)
            or last_end - first_start >= int(GROUP_MAX_S * self.sr)
        )
        if not due:
            return

        group_id = f"refine-{int(first_start)}-{int(last_end)}"
        lo = max(first_start - int(PREROLL_S * self.sr), self.history.offset)
        buf = self.history.buf[lo - self.history.offset:last_end - self.history.offset].copy()
        langs = [
            script_corrected_lang(lang, text)
            for _, _, lang, text, _ in self.spans
        ]
        lang = max(set(langs), key=langs.count)
        mixed = (
            len(set(langs)) > 1
            and min(langs.count(item) for item in set(langs)) / len(langs) >= 0.25
        )
        speakers = [speaker for _, _, _, _, speaker in self.spans if speaker]
        speaker = max(set(speakers), key=speakers.count) if speakers else ""
        fast_joined = " ".join(text for _, _, _, text, _ in self.spans if text.strip())
        self.spans = []
        if len(buf) < self.sr // 2:
            return

        def work():
            with self._worker_lock:
                if mixed:
                    text = fast_joined
                else:
                    text = self.asr.transcribe(buf, self.sr, known_lang=lang, live=False)["text"]
                if len(text.strip()) < 0.7 * len(fast_joined):
                    text = fast_joined
                if not text.strip():
                    return

                tag = f"{speaker}|{lang}" if speaker else lang
                print(f"[refine/{tag}] {text}")
                if self.printer.server is not None:
                    self.printer.server.publish({
                        "type": "refine",
                        "segment_id": group_id,
                        "text": text,
                        "lang": lang,
                        "speaker": speaker,
                    })

                if self.realtime_translator is not None:
                    self.realtime_translator.submit_refine(group_id, lang, text)

                outs = []
                if self.translators and lang == "ja":
                    for target_lang, translator in self.translators.items():
                        out = translate_by_sentence(translator, text)
                        if out and out != text:
                            print(f"[refine→{target_lang}] {out}")
                            outs.append((target_lang, out))

                if self._transcript is not None:
                    prefix = f"{speaker}: " if speaker else ""
                    self._transcript.write(prefix + text + "\n")
                    for target_lang, out in outs:
                        self._transcript.write(f"  →{target_lang} {out}\n")
                    self._transcript.flush()

        if force:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()


def _segment_id_for_current(vad, audio_pos: float, sample_rate: int, cur_len: int) -> str:
    segment = vad.current_segment
    start = getattr(segment, "start", None)
    if start is None:
        start = max(int(audio_pos * sample_rate) - cur_len, 0)
    return f"seg-{int(start)}"


def run_stream(
    chunks,
    vad,
    sample_rate: int,
    asr: RoutedASR,
    stats: SessionStats,
    printer: PartialPrinter,
    refiner: "Refiner | None" = None,
    history: AudioHistory | None = None,
    translator_worker: "LegacyTranslationWorker | None" = None,
    realtime_translator=None,
    speaker_labeler=None,
):
    audio_pos = 0.0
    last_partial = 0.0
    early_lang = None
    if history is None:
        history = AudioHistory(sample_rate)

    for chunk in chunks:
        vad.accept_waveform(chunk)
        history.push(chunk)
        audio_pos += len(chunk) / sample_rate
        stats.total_audio_s += len(chunk) / sample_rate

        if vad.is_speech_detected() and audio_pos - last_partial >= PARTIAL_EVERY_S:
            last_partial = audio_pos
            current_segment = vad.current_segment
            cur = np.asarray(current_segment.samples, dtype=np.float32)
            if len(cur) > int(PARTIAL_WINDOW_S * sample_rate):
                cur = cur[-int(PARTIAL_WINDOW_S * sample_rate):]
            if early_lang is None and len(cur) >= int(2.0 * sample_rate):
                early_lang = asr.identify(cur, sample_rate)

            needs_partial = printer.enabled or (
                realtime_translator is not None and realtime_translator.translate_partials
            )
            if needs_partial and len(cur) >= sample_rate // 2:
                text = asr.partial(cur, sample_rate, lang_hint=early_lang)
                if text.strip():
                    source_lang = early_lang or asr.last_lang or "auto"
                    segment_id = _segment_id_for_current(vad, audio_pos, sample_rate, len(cur))
                    printer.show(text, segment_id=segment_id, lang=source_lang)
                    if realtime_translator is not None:
                        realtime_translator.submit_partial(segment_id, source_lang, text)

        if drain_segments(
            vad,
            sample_rate,
            asr,
            stats,
            printer,
            history,
            early_lang,
            spans_out=refiner.spans if refiner else None,
            translator_worker=translator_worker,
            realtime_translator=realtime_translator,
            speaker_labeler=speaker_labeler,
        ):
            early_lang = None
        if refiner is not None and not vad.is_speech_detected():
            refiner.maybe_refine(int(audio_pos * sample_rate))


def build_realtime_translator(args, server):
    from translation.backends import HyMT2OpenAIBackend, OpenAICompatibleConfig
    from translation.coordinator import StreamingTranslationCoordinator
    from translation.runtime import RealtimeTranslationRuntime, load_glossary, parse_targets

    targets = parse_targets(args.translate or "")
    if not targets:
        raise ValueError("--translate must contain at least one target language")
    glossary = load_glossary(args.translation_glossary)
    backend = HyMT2OpenAIBackend(OpenAICompatibleConfig(
        base_url=args.translation_api_url,
        model=args.translation_model,
        timeout_s=args.translation_timeout,
        temperature=args.translation_temperature,
        max_tokens=args.translation_max_tokens,
    ))
    coordinator = StreamingTranslationCoordinator(backend)
    return RealtimeTranslationRuntime(
        coordinator,
        targets=targets,
        server=server,
        translate_partials=args.translate_partials,
        context_lines=args.translation_context_lines,
        glossary=glossary,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", help="wav file to simulate streaming from (resampled to 16kHz mono)")
    ap.add_argument("--no-realtime", action="store_true", help="don't pace --wav input in real time")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-partial", action="store_true", help="hide in-progress STT drafts")
    ap.add_argument("--min-silence", type=float, default=0.35)
    ap.add_argument("--max-speech", type=float, default=12.0)
    ap.add_argument("--max-resident", type=int, default=3)
    ap.add_argument("--serve", type=int, nargs="?", const=8765, default=None, metavar="PORT")
    ap.add_argument("--serve-host", default="127.0.0.1",
                    help="HTTP bind host; use 0.0.0.0 in Docker")
    ap.add_argument("--no-refine", action="store_true")
    ap.add_argument("--transcript", metavar="PATH")
    ap.add_argument("--hotwords", metavar="PATH", default="")
    ap.add_argument("--replace", metavar="PATH", default="")
    ap.add_argument("--lang-switch-guard", type=float, default=2.0, metavar="SEC")
    ap.add_argument("--speakers", action="store_true")

    ap.add_argument(
        "--translate",
        nargs="?",
        const="en",
        default=None,
        metavar="LANGS",
        help="comma-separated translation targets. legacy supports en/zh/ko; hymt2 supports its model languages",
    )
    ap.add_argument(
        "--translation-backend",
        choices=("legacy", "hymt2"),
        default="legacy",
        help="legacy CTranslate2 or realtime Hy-MT2 OpenAI-compatible backend",
    )
    ap.add_argument("--translation-api-url", default="http://127.0.0.1:8000")
    ap.add_argument("--translation-model", default="tencent/Hy-MT2-1.8B")
    ap.add_argument("--translation-timeout", type=float, default=5.0)
    ap.add_argument("--translation-temperature", type=float, default=0.0)
    ap.add_argument("--translation-max-tokens", type=int, default=512)
    ap.add_argument(
        "--translate-partials",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="translate in-progress STT revisions; use --no-translate-partials for final-only MT",
    )
    ap.add_argument("--translation-context-lines", type=int, default=4)
    ap.add_argument("--translation-glossary", metavar="PATH", default="")
    args = ap.parse_args()

    if args.translation_context_lines < 0:
        ap.error("--translation-context-lines must be >= 0")

    server = None
    if args.serve:
        from subtitle_server import SubtitleServer

        server = SubtitleServer(host=args.serve_host, port=args.serve).start()
        display_host = "localhost" if args.serve_host in ("0.0.0.0", "::") else args.serve_host
        print(
            f"subtitle overlay: http://{display_host}:{args.serve}/  "
            f"dashboard: http://{display_host}:{args.serve}/dashboard",
            file=sys.stderr,
        )

    print("loading ASR models...", file=sys.stderr)
    asr = RoutedASR(
        threads=args.threads,
        max_resident=args.max_resident if args.max_resident > 0 else None,
        hotwords_file=args.hotwords,
        replace_file=args.replace,
    )
    asr.min_switch_s = max(args.lang_switch_guard, 0.0)
    vad = build_vad(args.min_silence, args.max_speech)
    stats = SessionStats()
    printer = PartialPrinter(enabled=not args.no_partial, server=server)

    speaker_labeler = None
    if args.speakers:
        from speaker_id import SpeakerLabeler

        speaker_labeler = SpeakerLabeler()

    translators = {}
    legacy_worker = None
    realtime_translator = None
    if args.translate:
        if args.translation_backend == "legacy":
            print(f"loading legacy translators ({args.translate})...", file=sys.stderr)
            translators = build_translators(args.translate)
            if translators:
                legacy_worker = LegacyTranslationWorker(translators, server=server)
        else:
            print(
                f"translation backend: {args.translation_model} at {args.translation_api_url} "
                f"targets={args.translate}",
                file=sys.stderr,
            )
            realtime_translator = build_realtime_translator(args, server)

    history = AudioHistory(SAMPLE_RATE)
    refiner = None if args.no_refine else Refiner(
        asr,
        history,
        SAMPLE_RATE,
        printer,
        transcript_path=args.transcript,
        translators=translators,
        realtime_translator=realtime_translator,
    )

    finished = False

    def finish(sr):
        nonlocal finished
        if finished:
            return
        finished = True
        vad.flush()
        drain_segments(
            vad,
            sr,
            asr,
            stats,
            printer,
            history,
            spans_out=refiner.spans if refiner else None,
            translator_worker=legacy_worker,
            realtime_translator=realtime_translator,
            speaker_labeler=speaker_labeler,
        )
        if refiner is not None:
            refiner.maybe_refine(0, force=True)

    try:
        if server is not None:
            server.publish({"type": "session_start"})
        if args.wav:
            samples, sr = read_wave(args.wav)
            run_stream(
                wav_chunks(samples, sr, realtime=not args.no_realtime),
                vad,
                sr,
                asr,
                stats,
                printer,
                refiner,
                history,
                legacy_worker,
                realtime_translator,
                speaker_labeler,
            )
            finish(sr)
        else:
            run_stream(
                mic_chunks(),
                vad,
                SAMPLE_RATE,
                asr,
                stats,
                printer,
                refiner,
                history,
                legacy_worker,
                realtime_translator,
                speaker_labeler,
            )
    except KeyboardInterrupt:
        finish(SAMPLE_RATE)
    finally:
        if realtime_translator is not None:
            realtime_translator.close(wait=True)
        print(f"\n=== session summary: {stats.summary()} ===")


if __name__ == "__main__":
    main()
