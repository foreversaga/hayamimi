FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libasound2 \
        libgomp1 \
        libportaudio2 \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-docker.txt

COPY . .

RUN mkdir -p /app/models /app/data /app/config

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=3s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)" || exit 1

CMD ["python", "docker/entrypoint.py"]
