#!/usr/bin/env python3
"""Render overnight A/V prompts with Piper and bind them into a session.

AI-assisted: Claude (Opus 5.5). Renders each selected case prompt from
scripts/dotty-av-cases.json to ``<session>/prompts/<case-id>.wav`` with a Piper
voice, then records ``prompt_wavs`` (path, exact text, SHA-256, renderer) in the
session ``config.json`` so dotty_overnight.py plays exactly that waveform.

This writes local files only. It never plays audio, records, or touches the
robot. Run it with a Python that has ``piper-tts`` installed, e.g.

    python -m venv ~/.cache/dotty-piper-venv
    ~/.cache/dotty-piper-venv/bin/pip install 'piper-tts==1.8.0'
    ~/.cache/dotty-piper-venv/bin/python scripts/dotty_render_prompts.py \\
        --session SESSION --voice PATH/en_US-ryan-high.onnx

Existing WAVs whose recorded text and hash still match are kept, so re-running
after adding a case renders only the new prompt. ``--force`` re-renders all.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import wave

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "scripts/dotty-av-cases.json"
DEFAULT_LENGTH_SCALE = 1.1


def renderer_label(voice, length_scale):
    name = Path(voice).name
    if name.endswith(".onnx"):
        name = name[:-len(".onnx")]
    return f"piper {name} length_scale={length_scale:g}"


def selected_cases(catalogue, ids=None, include_disabled=False):
    cases = [c for c in catalogue["cases"] if include_disabled or c.get("enabled", True)]
    if ids:
        known = {c["id"] for c in catalogue["cases"]}
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ValueError("unknown case IDs: " + ",".join(unknown))
        cases = [c for c in catalogue["cases"] if c["id"] in ids]
    return cases


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def is_current(session, entry, case, renderer):
    """True when the recorded WAV still matches this exact text and renderer."""
    if not isinstance(entry, dict) or entry.get("text") != case["prompt"]:
        return False
    if entry.get("renderer") != renderer:
        return False
    path = Path(entry.get("path", ""))
    path = path if path.is_absolute() else session / path
    return path.is_file() and path.stat().st_size > 0 and sha256(path) == entry.get("sha256")


def synthesize(voice, text, destination, length_scale):
    from piper import SynthesisConfig  # imported lazily: tests need no Piper

    temporary = destination.with_name(destination.name + ".tmp")
    with wave.open(str(temporary), "wb") as handle:
        voice.synthesize_wav(text, handle, syn_config=SynthesisConfig(length_scale=length_scale))
    if temporary.stat().st_size <= 44:
        temporary.unlink()
        raise RuntimeError(f"Piper produced no audio for {destination.name}")
    temporary.replace(destination)


@contextlib.contextmanager
def runner_lock(session):
    # Same lock as dotty_overnight.py run: never rewrite config under a live run.
    with (session / "runner.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("session runner is active; stop it before rendering prompts") from None
        yield


def write_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def render(session, voice_path, length_scale=DEFAULT_LENGTH_SCALE, ids=None,
           include_disabled=False, force=False, synth=None, catalogue=None):
    session = Path(session).resolve()
    config_path = session / "config.json"
    if not config_path.exists():
        raise SystemExit(f"{config_path} missing: run dotty_overnight.py init first")
    catalogue = catalogue or json.loads(CASES.read_text())
    cases = selected_cases(catalogue, ids, include_disabled)
    renderer = renderer_label(voice_path, length_scale)
    with runner_lock(session):
        config = json.loads(config_path.read_text())
        entries = dict(config.get("prompt_wavs") or {})
        prompts = session / "prompts"
        prompts.mkdir(exist_ok=True)
        voice = None
        rendered, kept = [], []
        for case in cases:
            if not force and is_current(session, entries.get(case["id"]), case, renderer):
                kept.append(case["id"])
                continue
            if synth is None and voice is None:
                from piper import PiperVoice
                voice = PiperVoice.load(str(voice_path))
            destination = prompts / f"{case['id']}.wav"
            (synth or synthesize)(voice, case["prompt"], destination, length_scale)
            entries[case["id"]] = {"path": f"prompts/{case['id']}.wav", "text": case["prompt"],
                                   "sha256": sha256(destination), "renderer": renderer}
            rendered.append(case["id"])
        config["prompt_wavs"] = entries
        write_json(config_path, config)
    return {"rendered": rendered, "kept": kept, "renderer": renderer}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--voice", required=True, type=Path, help="Piper .onnx voice (its .onnx.json beside it)")
    parser.add_argument("--length-scale", type=float, default=DEFAULT_LENGTH_SCALE)
    parser.add_argument("--cases", help="comma-separated case IDs (default: every enabled case)")
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--force", action="store_true", help="re-render even when the WAV is current")
    args = parser.parse_args()
    if not args.voice.is_file() or not Path(str(args.voice) + ".json").is_file():
        parser.error("--voice must name a Piper .onnx file with its .onnx.json config beside it")
    print(json.dumps(render(args.session, args.voice, args.length_scale,
                            args.cases.split(",") if args.cases else None,
                            args.include_disabled, args.force), indent=2))


if __name__ == "__main__":
    main()
