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


def _read_kid_mode(*, state_file: Path, env_value: str) -> bool:
    process_env = os.environ.copy()
    process_env.update(
        {
            "DOTTY_KID_MODE_STATE": str(state_file),
            "DOTTY_KID_MODE": env_value,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.read_kid_mode())"],
        cwd=Path(__file__).resolve().parents[1],
        env=process_env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "True"


def test_kid_mode_reads_canonical_state_file_over_env(tmp_path: Path) -> None:
    state_file = tmp_path / "kid-mode"
    state_file.write_text("true", encoding="utf-8")
    assert _read_kid_mode(state_file=state_file, env_value="false") is True

    state_file.write_text("false", encoding="utf-8")
    assert _read_kid_mode(state_file=state_file, env_value="true") is False


def test_kid_mode_malformed_state_falls_back_to_safe_env_default(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "kid-mode"
    state_file.write_text("not-a-boolean", encoding="utf-8")
    assert _read_kid_mode(state_file=state_file, env_value="true") is True
