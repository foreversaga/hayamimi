# Realtime Translation v2

## Goal

Add low-latency, stable, high-quality subtitle translation without replacing hayamimi's existing specialist STT routing.

The target pipeline is:

```text
Audio
  -> Silero VAD
  -> hayamimi specialist STT
  -> partial/final transcript
  -> StreamingTranslationWorker
  -> translation backend
  -> validation + placeholder restore
  -> Local Agreement / LCP
  -> dashboard / OBS / transcript
```

The translation layer is intentionally decoupled from STT. Hayamimi remains CPU-capable, while translation can run on a local GPU, another machine, or a dedicated inference server.

## Research conclusion

The 2025-2026 simultaneous translation systems with the best quality/latency trade-offs converge on a cascaded design:

1. obtain a strong, stable ASR hypothesis;
2. re-translate the current stable source prefix;
3. compare consecutive target hypotheses;
4. emit only the target prefix that remains stable across revisions.

This is the Local Agreement / Longest Common Prefix (LCP) approach used by strong IWSLT simultaneous translation systems. It avoids the flicker produced by blindly displaying every re-translation.

Relevant references:

- IWSLT 2026 Simultaneous Translation: https://iwslt.org/2026/simultaneous
- IWSLT 2026 baseline: https://github.com/owaski/iwslt-2026-baselines
- Pinch-AST, IWSLT 2026: https://aclanthology.org/2026.iwslt-1.30/
- NeMo IWSLT 2026 system: https://aclanthology.org/2026.iwslt-1.23/
- Hy-MT2: https://github.com/Tencent-Hunyuan/Hy-MT2
- MiLMMT: https://github.com/xiaomi-research/gemmax

## Model candidates

### Primary realtime candidate: Hy-MT2-1.8B

Use `tencent/Hy-MT2-1.8B` as the first realtime candidate.

Reasons:

- translation-specific 1.8B model;
- native Japanese, English, Korean, Chinese, Cantonese and Traditional Chinese targets;
- official vLLM, SGLang and llama.cpp deployment paths;
- small enough for aggressive low-latency testing;
- terminology and instruction-following support;
- current model family is explicitly positioned for translation and subtitle scenarios.

The backend follows Hy-MT2's official default translation instruction:

```text
Translate the following text into {target language}.
Only output the translated result without additional explanation.
```

Hy-MT2's published inference example recommends sampling. Realtime v2 initially uses `temperature=0` instead because Local Agreement needs deterministic hypotheses. This is a deliberate deviation that must be benchmarked against the official sampling configuration.

### Quality challenger: MiLMMT-46-4B-v1.0

Benchmark `xiaomi-research/MiLMMT-46-4B-v1.0` against Hy-MT2-1.8B. It is a translation-specialized multilingual model with native Simplified and Traditional Chinese support and should be treated as a quality challenger, not assumed to be faster.

### Optional finalizer: Hy-MT2-7B

Only add a second, larger finalization model if measurements show that its quality gain is large enough to justify the additional latency and memory. Do not start with a two-model architecture by default.

### Existing FuguMT / M2M-100

Keep the existing models as compatibility and CPU baselines until the new path has measured coverage and regression tests.

## Streaming policy

### Partial translations

Every new stable STT partial may trigger a translation request, but partial requests are **latest-wins**.

If revisions 10, 11 and 12 arrive while revision 10 is still translating, only revision 12 should remain queued. This prevents a slow model from creating an ever-growing delay backlog.

### Final translations

Final requests are never dropped and are processed before queued partials.

### Local Agreement

For CJK targets (`ja`, `zh`, `zh-Hant`, `yue`, `ko`), agreement is character-based.

Example:

```text
revision A: 今天要介紹新的
revision B: 今天要介紹一個新的模型
stable:     今天要介紹
```

The stable prefix becomes append-only committed text. The remainder stays speculative and may change on the next revision.

For whitespace-delimited languages, agreement is token-based so an incomplete word is never committed.

## Correctness protection

Before translation, immutable spans are replaced by placeholders such as:

```text
__HAYAMIMI_KEEP_0000__
```

Initial protected types:

- URLs;
- email addresses;
- version strings;
- ASCII numbers and percentages.

After translation:

1. every placeholder must still be present;
2. missing placeholders reject the translation update;
3. placeholders are restored to their exact original value;
4. empty, implausibly long, or obvious repetition-loop outputs are rejected.

This is stricter than the legacy `digits_consistent()` guard because the model never gets permission to rewrite protected values.

Future entity protection should add typed handling for money, dates, times, units, IDs and named entities. Typed entities are intentionally separate from the first implementation because blindly restoring a whole source-language money expression would preserve the number but prevent correct unit translation.

## Context and glossary

`TranslationContext` supports:

- recent finalized source/target subtitle pairs;
- terminology pairs.

Only a short history should be sent on realtime requests. The history is context, not content to be re-translated.

The default cap for the first integration should be the latest four finalized subtitle pairs.

## Code structure

```text
scripts/translation/
  contracts.py
  coordinator.py
  protection.py
  validation.py
  worker.py
  backends/
    openai_compatible.py
  policies/
    local_agreement.py
```

Responsibilities:

- `contracts.py`: backend-independent request/update objects and protocol;
- `coordinator.py`: protection, validation and per-segment Local Agreement state;
- `worker.py`: background scheduling; final-first and latest-wins partials;
- `backends/`: inference adapters;
- `policies/`: target emission policy;
- `protection.py`: immutable source spans;
- `validation.py`: output safety checks.

The realtime STT pipeline should depend only on these contracts, not on Hy-MT2 or a particular inference framework.

## Deployment

Recommended first test:

```bash
vllm serve tencent/Hy-MT2-1.8B --host 0.0.0.0 --port 8000
```

The hayamimi translation backend defaults to:

```text
http://127.0.0.1:8000/v1/chat/completions
```

SGLang or another OpenAI-compatible local server can be substituted without changing the STT pipeline.

## Benchmarking

Use:

```bash
python scripts/bench_translation_v2.py benchmark.jsonl \
  --api-url http://127.0.0.1:8000 \
  --model tencent/Hy-MT2-1.8B \
  --output results-hymt2-1.8b.jsonl
```

Input is JSONL. Example:

```json
{"id":"ja-001","source_lang":"ja","target_lang":"zh-Hant","text":"会議は午後3時から始まります。"}
{"id":"ja-002","source_lang":"ja","target_lang":"zh-Hant","text":"Qwen3.8をvLLM 0.10.0で動かします。","glossary":[["Qwen3.8","Qwen3.8"],["vLLM","vLLM"]]}
```

The built-in harness records:

- accepted/rejected updates;
- backend latency;
- p50 / p95 / p99 / max latency;
- raw source/reference/output for external quality scoring.

For model selection, add external translation quality scoring with XCOMET/COMET and evaluate at least:

- normal conversation;
- broadcast/news speech;
- technical content;
- proper nouns;
- mixed Japanese/English terms;
- numbers;
- dates/times;
- money;
- long utterances;
- incomplete STT prefixes.

## Acceptance targets

Initial engineering targets:

| Metric | Target |
|---|---:|
| Partial STT cadence | <= 500 ms |
| Translation queue growth | bounded, no stale backlog |
| Stable partial translation | < 1 s after usable source prefix on target hardware |
| Final STT -> final translation p50 | < 300 ms |
| Final STT -> final translation p95 | < 1 s |
| Committed subtitle erasure | 0 |
| Protected placeholder survival | 100% |
| Catastrophic repetition | 0 |
| Empty accepted translations | 0 |

Actual latency targets must be validated on the intended hardware. Vendor benchmark latency is not treated as production evidence.

## Integration phases

### Phase 1 - foundation

Implemented on `feature/realtime-translation-v2`:

- model-independent contracts;
- Hy-MT2 OpenAI-compatible backend;
- Local Agreement policy;
- immutable span protection;
- output validation;
- latest-wins background worker;
- benchmark harness;
- model-free unit tests.

### Phase 2 - realtime_transcribe integration

Add CLI options without removing legacy translation:

```text
--translation-backend legacy|hymt2
--translation-api-url URL
--translation-model MODEL
--translate-partials
--translation-temperature FLOAT
--translation-context-lines N
--translation-glossary PATH
```

Then:

- assign a segment id and monotonic revision to each partial STT hypothesis;
- submit partial translation requests only when source text changes;
- use detected/sticky source language;
- submit final requests with priority;
- publish `translation_partial` and `translation_final` SSE events;
- keep the current legacy `translation` event for compatibility during migration.

### Phase 3 - model bake-off

Benchmark Hy-MT2-1.8B, MiLMMT-46-4B-v1.0, Hy-MT2-7B and current legacy MT on the same subtitle corpus and hardware.

Choose the production default from measured quality/latency, not model size or vendor claims.

### Phase 4 - optional high-quality finalizer

Only if required by benchmark results:

```text
partial STT -> 1.8B realtime translation -> Local Agreement
final STT   -> larger finalizer          -> final replace/commit
```

The realtime path must remain useful even if the finalizer is unavailable.
