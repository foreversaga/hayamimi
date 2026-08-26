from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def add_flag(command: list[str], condition: bool, flag: str) -> None:
    if condition:
        command.append(flag)


def ensure_models() -> None:
    if not env_bool("HAYAMIMI_AUTO_DOWNLOAD_MODELS", True):
        return
    if (MODELS_DIR / "silero_vad.onnx").exists():
        return

    model_set = os.getenv("HAYAMIMI_MODEL_SET", "full").strip().lower()
    command = [sys.executable, str(ROOT / "scripts" / "download_models.py")]
    if model_set == "minimal":
        command.append("--minimal")
    elif model_set != "full":
        raise SystemExit("HAYAMIMI_MODEL_SET must be 'minimal' or 'full'")
    print(f"downloading hayamimi {model_set} model set into {MODELS_DIR}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def wait_for_translator() -> None:
    if os.getenv("HAYAMIMI_TRANSLATION_BACKEND", "hymt2") != "hymt2":
        return
    if not env_bool("HAYAMIMI_WAIT_TRANSLATOR", True):
        return

    base = os.getenv("HAYAMIMI_TRANSLATION_API_URL", "http://translator:8000").rstrip("/")
    url = base + "/v1/models"
    timeout_s = float(os.getenv("HAYAMIMI_TRANSLATOR_STARTUP_TIMEOUT", "900"))
    deadline = time.monotonic() + timeout_s
    print(f"waiting for translation server: {url}", flush=True)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if 200 <= response.status < 300:
                    print("translation server is ready", flush=True)
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2)
    raise SystemExit(f"translation server did not become ready within {timeout_s:.0f}s")


def build_command() -> list[str]:
    command = [sys.executable, str(ROOT / "scripts" / "realtime_transcribe.py")]

    command += ["--threads", os.getenv("HAYAMIMI_THREADS", "4")]
    command += ["--min-silence", os.getenv("HAYAMIMI_MIN_SILENCE", "0.35")]
    command += ["--max-speech", os.getenv("HAYAMIMI_MAX_SPEECH", "12")]
    command += ["--max-resident", os.getenv("HAYAMIMI_MAX_RESIDENT", "3")]
    command += ["--lang-switch-guard", os.getenv("HAYAMIMI_LANG_SWITCH_GUARD", "2.0")]

    serve_port = os.getenv("HAYAMIMI_SERVE_PORT", "8765")
    command += ["--serve", serve_port]
    command += ["--serve-host", os.getenv("HAYAMIMI_SERVE_HOST", "0.0.0.0")]

    translate = os.getenv("HAYAMIMI_TRANSLATE", "zh-Hant").strip()
    if translate:
        command += ["--translate", translate]
        backend = os.getenv("HAYAMIMI_TRANSLATION_BACKEND", "hymt2")
        command += ["--translation-backend", backend]
        if backend == "hymt2":
            command += ["--translation-api-url", os.getenv(
                "HAYAMIMI_TRANSLATION_API_URL", "http://translator:8000"
            )]
            command += ["--translation-model", os.getenv(
                "HAYAMIMI_TRANSLATION_MODEL", "tencent/Hy-MT2-1.8B"
            )]
            command += ["--translation-timeout", os.getenv("HAYAMIMI_TRANSLATION_TIMEOUT", "5")]
            command += ["--translation-temperature", os.getenv(
                "HAYAMIMI_TRANSLATION_TEMPERATURE", "0"
            )]
            command += ["--translation-max-tokens", os.getenv(
                "HAYAMIMI_TRANSLATION_MAX_TOKENS", "512"
            )]
            command += ["--translation-context-lines", os.getenv(
                "HAYAMIMI_TRANSLATION_CONTEXT_LINES", "4"
            )]
            if not env_bool("HAYAMIMI_TRANSLATE_PARTIALS", True):
                command.append("--no-translate-partials")
            glossary = os.getenv("HAYAMIMI_TRANSLATION_GLOSSARY", "").strip()
            if glossary:
                command += ["--translation-glossary", glossary]

    wav_path = os.getenv("HAYAMIMI_WAV", "").strip()
    if wav_path:
        command += ["--wav", wav_path]
        add_flag(command, env_bool("HAYAMIMI_NO_REALTIME", False), "--no-realtime")

    transcript = os.getenv("HAYAMIMI_TRANSCRIPT", "").strip()
    if transcript:
        command += ["--transcript", transcript]
    hotwords = os.getenv("HAYAMIMI_HOTWORDS", "").strip()
    if hotwords:
        command += ["--hotwords", hotwords]
    replacements = os.getenv("HAYAMIMI_REPLACE", "").strip()
    if replacements:
        command += ["--replace", replacements]

    add_flag(command, env_bool("HAYAMIMI_SPEAKERS", False), "--speakers")
    add_flag(command, env_bool("HAYAMIMI_NO_REFINE", False), "--no-refine")
    add_flag(command, env_bool("HAYAMIMI_NO_PARTIAL", False), "--no-partial")
    return command


def main() -> None:
    os.chdir(ROOT)
    ensure_models()
    wait_for_translator()
    command = build_command()
    print("starting: " + " ".join(command), flush=True)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
