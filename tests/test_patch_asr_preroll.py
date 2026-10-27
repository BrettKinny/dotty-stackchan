"""ASR pre-roll build patch. AI-assisted: Claude (Opus 5.5)."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-asr-preroll.py"
spec = importlib.util.spec_from_file_location("patch_asr_preroll", SCRIPT)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)

UPSTREAM = (
    "            if not audio_have_voice and not conn.client_have_voice:\n"
    "                conn.asr_audio = conn.asr_audio[-10:]\n"
    "                return\n"
)


def test_preroll_widened_once_and_idempotent():
    once = patch.patch_source(UPSTREAM)
    assert "conn.asr_audio[-20:]" in once and "[-10:]" not in once
    assert once.startswith("            if not audio_have_voice")
    assert patch.patch_source(once) == once


@pytest.mark.parametrize("source", ["", UPSTREAM + UPSTREAM])
def test_missing_or_ambiguous_anchor_fails_the_build(source):
    with pytest.raises(ValueError):
        patch.patch_source(source)


def test_main_rewrites_the_file(tmp_path):
    base = tmp_path / "base.py"
    base.write_text(UPSTREAM)
    patch.main(base)
    assert "[-20:]" in base.read_text()
