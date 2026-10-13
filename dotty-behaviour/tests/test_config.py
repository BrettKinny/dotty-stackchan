"""Configuration defaults and environment override behaviour."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def _read_vlm_models(*, env: dict[str, str] | None = None) -> tuple[str, str]:
    process_env = os.environ.copy()
    process_env.pop("VISION_MODEL", None)
    process_env.pop("VLM_MODEL", None)
    if env:
        process_env.update(env)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.VISION_MODEL); print(config.VLM_MODEL)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=process_env,
        check=True,
        capture_output=True,
        text=True,
    )
    vision_model, vlm_model = result.stdout.splitlines()
    return vision_model, vlm_model


def test_vision_defaults_to_live_low_latency_multimodal_model() -> None:
    assert _read_vlm_models() == (
        "google/gemini-3.1-flash-lite",
        "google/gemini-3.1-flash-lite",
    )


_OUTBOUND = (
    "NARRATIVE_LLM_URL", "VISION_API_URL", "VLM_API_URL",
    "AUDIO_CAPTION_API_URL", "WEATHER_LOCATION",
)


def _read_outbound(env: dict[str, str]) -> dict[str, str]:
    process_env = os.environ.copy()
    for name in (*_OUTBOUND, "DOTTY_LOCAL_ONLY", "CALENDAR_ID"):
        process_env.pop(name, None)
    process_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config, json; print(json.dumps({"
            "**{n: getattr(config, n) for n in " + repr(_OUTBOUND) + "},"
            "'CALENDAR_IDS': list(config.CALENDAR_IDS)}))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=process_env,
        check=True,
        capture_output=True,
        text=True,
    )
    import json
    return json.loads(result.stdout.splitlines()[-1])


def test_cloud_defaults_unchanged_without_local_only() -> None:
    got = _read_outbound({"CALENDAR_ID": "family@example.com"})
    assert got["VLM_API_URL"].startswith("https://openrouter.ai/")
    assert got["AUDIO_CAPTION_API_URL"].startswith("https://openrouter.ai/")
    assert got["WEATHER_LOCATION"] == "Brisbane"
    assert got["CALENDAR_IDS"] == ["family@example.com"]


def test_local_only_blanks_cloud_defaults() -> None:
    got = _read_outbound(
        {"DOTTY_LOCAL_ONLY": "1", "CALENDAR_ID": "family@example.com"}
    )
    assert got["VISION_API_URL"] == ""
    assert got["VLM_API_URL"] == ""
    assert got["AUDIO_CAPTION_API_URL"] == ""
    assert got["WEATHER_LOCATION"] == ""
    assert got["CALENDAR_IDS"] == []
    # The narrative default is loopback, so it survives.
    assert got["NARRATIVE_LLM_URL"] == "http://127.0.0.1:8080/v1"


def test_local_only_keeps_lan_override_and_blanks_cloud_override() -> None:
    got = _read_outbound({
        "DOTTY_LOCAL_ONLY": "1",
        "VLM_API_URL": "http://192.168.0.50:8080/v1/chat/completions",
        "NARRATIVE_LLM_URL": "https://openrouter.ai/api/v1",
    })
    assert got["VLM_API_URL"] == "http://192.168.0.50:8080/v1/chat/completions"
    assert got["NARRATIVE_LLM_URL"] == ""
    # Audio has no local override, so it stays closed.
    assert got["AUDIO_CAPTION_API_URL"] == ""


def test_is_lan_url() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config

    for url in (
        "http://127.0.0.1:8080/v1", "http://localhost:8080", "http://[::1]:1",
        "http://192.168.0.50:8080/v1", "http://10.0.0.5", "http://172.16.3.4",
        "http://ollama:11434/v1", "http://server.local:8080", "http://nas.lan",
    ):
        assert config.is_lan_url(url), url
    for url in (
        "https://openrouter.ai/api/v1/chat/completions", "http://8.8.8.8",
        "https://api.openai.com/v1", "", "not a url", "http://",
    ):
        assert not config.is_lan_url(url), url
