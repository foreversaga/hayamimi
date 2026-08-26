from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from translation.backends import HyMT2OpenAIBackend, OpenAICompatibleConfig
from translation.contracts import TranslationContext, TranslationRequest
from translation.coordinator import StreamingTranslationCoordinator


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        case = json.loads(raw)
        if "text" not in case:
            raise ValueError(f"{path}:{line_no}: missing 'text'")
        cases.append(case)
    return cases


def build_context(case: dict) -> TranslationContext:
    glossary = tuple((str(a), str(b)) for a, b in case.get("glossary", []))
    history = tuple((str(a), str(b)) for a, b in case.get("history", []))
    return TranslationContext(history=history, glossary=glossary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark realtime translation backend latency/safety")
    parser.add_argument("input", type=Path, help="JSONL benchmark cases")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="tencent/Hy-MT2-1.8B")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--output", type=Path, default=None, help="write per-case JSONL results")
    args = parser.parse_args()

    backend = HyMT2OpenAIBackend(OpenAICompatibleConfig(
        base_url=args.api_url,
        model=args.model,
        timeout_s=args.timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    ))
    coordinator = StreamingTranslationCoordinator(backend)
    cases = load_cases(args.input)

    results = []
    latencies = []
    for index, case in enumerate(cases):
        request = TranslationRequest(
            segment_id=str(case.get("id", f"case-{index:04d}")),
            revision=1,
            source_lang=str(case.get("source_lang", "ja")),
            target_lang=str(case.get("target_lang", "zh-Hant")),
            text=str(case["text"]),
            is_final=True,
            context=build_context(case),
        )
        update = coordinator.translate(request)
        if update.accepted:
            latencies.append(update.latency_ms)
        results.append({
            "id": request.segment_id,
            "source_lang": request.source_lang,
            "target_lang": request.target_lang,
            "source": request.text,
            "reference": case.get("reference"),
            "translation": update.full_text,
            "accepted": update.accepted,
            "latency_ms": round(update.latency_ms, 2),
            "error": update.error,
        })
        status = "ok" if update.accepted else "rejected"
        print(f"[{status}] {request.segment_id}: {update.latency_ms:.1f} ms -> {update.full_text}")

    accepted = sum(1 for result in results if result["accepted"])
    summary = {
        "model": args.model,
        "cases": len(results),
        "accepted": accepted,
        "accept_rate": accepted / len(results) if results else 0.0,
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies) if latencies else 0.0,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output is not None:
        with args.output.open("w", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
