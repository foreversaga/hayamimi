# Realtime Translation v2

Status: **implemented** on the Docker service path.

See [`DOCKER.md`](../DOCKER.md) for deployment and startup instructions.

## Architecture

```text
Audio
  -> Silero VAD
  -> language ID + specialist hayamimi STT
  -> partial/final transcript
  -> RealtimeTranslationRuntime
  -> final-first / latest-wins worker
  -> Hy-MT2 OpenAI-compatible backend
  -> immutable-span validation
  -> Local Agreement / LCP
  -> SSE
  -> dashboard / OBS overlay / transcript
```

STT and translation stay decoupled. The `hayamimi` Docker service runs VAD/STT/UI while the `translator` service runs `tencent/Hy-MT2-1.8B` through vLLM.

## Why this design

Recent simultaneous-translation systems converge on a cascaded re-translation design: use strong ASR, re-translate the current source prefix, compare consecutive target hypotheses, then commit only stable target prefixes. This provides a better quality/latency trade-off than showing every raw re-translation.

References:

- IWSLT 2026 Simultaneous Translation: https://iwslt.org/2026/simultaneous
- IWSLT 2026 baseline: https://github.com/owaski/iwslt-2026-baselines
- Pinch-AST: https://aclanthology.org/2026.iwslt-1.30/
- NeMo IWSLT 2026 system: https://aclanthology.org/2026.iwslt-1.23/
- Hy-MT2: https://github.com/Tencent-Hunyuan/Hy-MT2

## Translation model

Default:

```text
tencent/Hy-MT2-1.8B
```

The model is translation-specific, small enough for low-latency serving, supports Japanese/English/Korean/Chinese/Cantonese/Traditional Chinese and many other languages, and has an official vLLM deployment path.

The realtime backend uses the official task shape—translate to a named target language and output only the translation—but defaults to deterministic decoding:

```text
temperature = 0
repetition_penalty = 1.05
```

Deterministic output improves Local Agreement stability. The vendor sampling recipe should be treated as a benchmark alternative rather than the realtime default.

## Partial translation

STT drafts are decoded about every 0.5 seconds. Every changed draft can become a translation revision.

Partial requests are **latest-wins**. If revisions 10, 11 and 12 arrive while revision 10 is still translating, only the newest pending revision is retained. This prevents translation backlog from turning into increasing end-to-end latency.

Final requests:

- replace any pending partial for the same segment/target;
- are processed before queued partials;
- are never dropped as stale.

## Segment identity

Each VAD utterance gets a stable segment id based on its absolute sample start:

```text
seg-<sample-index>
```

Partial and final STT events use the same segment id. Translation events also carry this id, so the dashboard attaches translations to the correct source card instead of assuming the latest translation belongs to the latest source sentence.

Refined groups use:

```text
refine-<first-sample>-<last-sample>
```

## Local Agreement

For CJK targets (`ja`, `zh`, `zh-Hant`, `yue`, `ko`), agreement is character-based.

For whitespace languages, agreement is token-based so incomplete words are not committed.

Example:

```text
revision A: 今天要介紹新的
revision B: 今天要介紹一個新的模型
committed:  今天要介紹
speculative: 一個新的模型
```

If a later partial contradicts already committed text, the speculative suffix is frozen rather than slicing and splicing incompatible strings. The utterance-final translation is authoritative and may correct the displayed sentence.

## Correctness protection

Before inference, immutable source spans are converted to placeholders:

```text
__HAYAMIMI_KEEP_0000__
```

Protected categories include:

- URLs;
- email addresses;
- version strings;
- ASCII numbers and percentages;
- mixed alphanumeric technical/product identifiers such as `Qwen3.8`, `RTX5070Ti`, `CUDA12.9` and `H3`.

Validation requires every expected placeholder to occur **exactly once** in the translation. A result is rejected if a placeholder is missing, duplicated, or invented.

Additional rejection checks cover:

- empty translations;
- implausibly long outputs;
- obvious token repetition loops.

After validation, placeholders are restored exactly.

## Context and glossary

Every target language can keep a short history of finalized `(source, translation)` pairs. Default history length:

```text
4 lines
```

History is prompt context only and is explicitly excluded from requested output.

A glossary can be mounted from `config/` using:

```text
source=target
```

or tab / arrow separators. Example: [`config/glossary.example.txt`](../config/glossary.example.txt).

## SSE event types

Source:

```text
partial
final
refine
```

Realtime MT:

```text
translation_partial
translation_final
translation_refine
translation_error
```

The legacy finalized translation event remains available as:

```text
translation
```

for backward compatibility when `--translation-backend legacy` is used outside the lean Docker image.

## UI

`subtitle_server.py` provides:

- `/` — OBS-friendly transparent overlay;
- `/dashboard` — live source + partial/final translations + refined transcript;
- `/transcript` — refined transcript history;
- `/events` — SSE stream;
- `/healthz` — health endpoint.

SSE clients receive heartbeat comments and use bounded queues so a stalled browser cannot create unbounded server memory growth.

## Docker deployment

The compose stack contains:

```text
translator -> vLLM + Hy-MT2 GPU service
hayamimi   -> CPU STT + translation coordinator + HTTP/SSE UI
```

Model storage is outside both images:

```text
./models/              hayamimi ASR/VAD models
./models/huggingface/  Hy-MT2/Hugging Face cache
./models/vllm-cache/   vLLM cache
```

Container rebuilds therefore do not redownload model weights.

The translation service uses a fixed KV-cache budget rather than a percentage-based GPU memory reservation:

```text
VLLM_KV_CACHE_MEMORY=2G
VLLM_KV_CACHE_DTYPE=auto
VLLM_MAX_MODEL_LEN=8192
VLLM_MAX_NUM_SEQS=4
```

Native KV dtype is the quality-first default. FP8 KV can be evaluated later as an explicit speed/memory experiment.

Host ports bind to loopback by default. Set `HAYAMIMI_BIND_ADDRESS=0.0.0.0` only when the dashboard/overlay must be reachable from another machine.

## CLI compatibility

The non-Docker script supports both backends:

```text
--translation-backend legacy|hymt2
--translation-api-url URL
--translation-model MODEL
--translation-timeout SEC
--translation-temperature FLOAT
--translation-max-tokens N
--translate-partials / --no-translate-partials
--translation-context-lines N
--translation-glossary PATH
```

The existing FuguMT/M2M-100 path is not removed. The Docker runtime is intentionally lean and defaults to Hy-MT2.

## Benchmarking

Use:

```bash
python scripts/bench_translation_v2.py benchmark.jsonl \
  --api-url http://127.0.0.1:8000 \
  --model tencent/Hy-MT2-1.8B \
  --output results.jsonl
```

The harness records acceptance/rejection and p50/p95/p99/max backend latency. Reference translations can be retained in JSONL for external COMET/XCOMET/chrF scoring.

Recommended evaluation corpus categories:

- normal conversation;
- broadcast/news;
- technical discussion;
- names and terminology;
- mixed CJK/English terms;
- numbers, dates, time and money;
- long utterances;
- incomplete STT prefixes.

## Tests

`tests/test_translation_v2.py` covers model-free behavior including:

- placeholder round-trip;
- technical-identifier protection;
- missing and duplicated placeholder rejection;
- CJK and whitespace Local Agreement;
- committed-prefix conflict handling;
- latest-wins partial scheduling;
- final priority;
- target parsing;
- glossary parsing;
- runtime final event publication;
- exact same-language target bypass.

The GitHub workflow additionally compiles the Python entrypoints and validates `docker compose config` on Linux.
