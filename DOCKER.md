# Docker deployment

This stack runs hayamimi as two Docker services:

- `hayamimi`: CPU VAD + language routing + specialist STT + subtitle server.
- `translator`: GPU `tencent/Hy-MT2-1.8B` served through vLLM's OpenAI-compatible API.

The default target is Traditional Chinese (`zh-Hant`) and partial translation is enabled.

## Requirements

- Linux with Docker Engine + Docker Compose v2.
- NVIDIA driver and NVIDIA Container Toolkit configured for Docker.
- An NVIDIA GPU visible to containers.
- `/dev/snd` available when using a microphone from inside the container.

The translator image is pinned in `.env.example`. The model caches are mounted from the repository so rebuilding the containers does not download the models again.

## Start

```bash
cp .env.example .env
mkdir -p models data config
docker compose pull translator
docker compose up -d --build
docker compose logs -f translator hayamimi
```

On first startup:

1. vLLM downloads Hy-MT2 into `./models/huggingface/`.
2. hayamimi downloads its ASR/VAD models into `./models/`.
3. `hayamimi` starts after the translator health check succeeds.

Open locally:

- Dashboard: `http://localhost:8765/dashboard`
- OBS overlay: `http://localhost:8765/`
- Refined transcript: `http://localhost:8765/transcript`
- Health check: `http://localhost:8765/healthz`

Both published ports bind to localhost by default. The translator API stays local. To expose only the subtitle UI/dashboard to another machine on the LAN, set:

```dotenv
HAYAMIMI_BIND_ADDRESS=0.0.0.0
```

Stop without deleting models:

```bash
docker compose down
```

## Default realtime settings

```dotenv
HAYAMIMI_TRANSLATE=zh-Hant
TRANSLATION_MODEL=tencent/Hy-MT2-1.8B
HAYAMIMI_TRANSLATE_PARTIALS=true
HAYAMIMI_TRANSLATION_CONTEXT_LINES=4
HAYAMIMI_TRANSLATION_TEMPERATURE=0
VLLM_KV_CACHE_MEMORY=2G
VLLM_KV_CACHE_DTYPE=auto
VLLM_MAX_MODEL_LEN=8192
VLLM_MAX_NUM_SEQS=4
```

`temperature=0` is intentionally used for the realtime path. Local Agreement compares consecutive re-translations; deterministic hypotheses reduce subtitle flicker. Hy-MT2's published sampling configuration can still be benchmarked by changing the environment value.

The KV cache has a fixed 2 GB budget rather than relying on vLLM's GPU-memory-utilization percentage. This is especially important on unified-memory systems where reserving a large fraction of memory for a small translation workload is unnecessary.

## Microphone

The default compose file maps:

```yaml
devices:
  - /dev/snd:/dev/snd
```

List available host recording devices with:

```bash
arecord -l
```

If Docker cannot open the microphone, first confirm that the host user/session can record and that `/dev/snd` exists. The container runs as root by default, so ordinary ALSA group ownership usually does not require an extra group mapping.

## Translate a WAV instead of the microphone

Copy the file into `data/`:

```bash
cp sample.wav data/input.wav
```

Set in `.env`:

```dotenv
HAYAMIMI_WAV=/app/data/input.wav
HAYAMIMI_NO_REALTIME=false
```

Then restart:

```bash
docker compose up -d --force-recreate hayamimi
```

Use `HAYAMIMI_NO_REALTIME=true` for batch-speed testing.

## Multiple target languages

Targets are comma separated:

```dotenv
HAYAMIMI_TRANSLATE=zh-Hant,en,ko
```

The same Hy-MT2 server is reused for every target. Each target maintains its own Local Agreement state and short translation history.

## Glossary

```bash
cp config/glossary.example.txt config/glossary.txt
```

Edit `config/glossary.txt`:

```text
Qwen=Qwen
vLLM=vLLM
早耳=早耳
```

Set:

```dotenv
HAYAMIMI_TRANSLATION_GLOSSARY=/app/config/glossary.txt
```

Restart only hayamimi:

```bash
docker compose up -d --force-recreate hayamimi
```

## Model selection

The default is the low-latency candidate:

```dotenv
TRANSLATION_MODEL=tencent/Hy-MT2-1.8B
```

For a quality/latency comparison, change to another OpenAI-compatible translation model only after verifying its prompt format. `scripts/bench_translation_v2.py` is provided for latency and safety benchmarking.

Example benchmark against the running translator:

```bash
python scripts/bench_translation_v2.py data/translation-benchmark.jsonl \
  --api-url http://localhost:8000 \
  --model tencent/Hy-MT2-1.8B \
  --output data/translation-results.jsonl
```

## Useful commands

```bash
# Service state
docker compose ps

# Translator logs
docker compose logs -f translator

# STT/subtitle logs
docker compose logs -f hayamimi

# Check translator API
curl http://localhost:8000/v1/models

# Check subtitle server
curl http://localhost:8765/healthz

# Restart only translation service
docker compose restart translator

# Rebuild only hayamimi
docker compose up -d --build hayamimi
```

## Architecture

```text
microphone / WAV
      |
      v
Silero VAD
      |
      v
language ID + specialist STT
      |
      +---- partial transcript every ~0.5 s --------+
      |                                             |
      |                                      latest-wins queue
      |                                             |
      v                                             v
final transcript ---------------------------> Hy-MT2-1.8B
                                                    |
                                                    v
                                    placeholder validation
                                                    |
                                                    v
                                     Local Agreement / LCP
                                                    |
                          +-------------------------+------------------+
                          v                                            v
                    SSE/dashboard                                  OBS overlay
```

Final translation requests have priority and are never replaced by newer partials. Stale partials are coalesced, preventing a slow translator from creating an ever-growing latency backlog.
