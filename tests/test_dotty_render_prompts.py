"""Prompt render binding tests (no Piper needed). AI-assisted: Claude (Opus 5.5)."""
import hashlib
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("dotty_render_prompts", SCRIPTS / "dotty_render_prompts.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

CATALOGUE = {"cases": [{"id": "A", "prompt": "Hello there."},
                       {"id": "B", "prompt": "Off case.", "enabled": False}]}


def fake_synth(calls):
    def synth(voice, text, destination, length_scale):
        calls.append(text)
        destination.write_bytes(b"RIFF" + text.encode())
    return synth


def test_render_binds_text_and_hash_and_keeps_current(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"host": "h"}))
    calls = []
    voice = "voices/en_US-ryan-high.onnx"
    out = render.render(tmp_path, voice, synth=fake_synth(calls), catalogue=CATALOGUE)
    assert out["rendered"] == ["A"] and calls == ["Hello there."]
    entry = json.loads((tmp_path / "config.json").read_text())["prompt_wavs"]["A"]
    assert entry["text"] == "Hello there."
    assert entry["renderer"] == "piper en_US-ryan-high length_scale=1.1"
    assert entry["sha256"] == hashlib.sha256((tmp_path / "prompts/A.wav").read_bytes()).hexdigest()
    again = render.render(tmp_path, voice, synth=fake_synth(calls), catalogue=CATALOGUE)
    assert again["kept"] == ["A"] and len(calls) == 1
    changed = {"cases": [{"id": "A", "prompt": "Hello again."}]}
    render.render(tmp_path, voice, synth=fake_synth(calls), catalogue=changed)
    assert calls[-1] == "Hello again."
    assert json.loads((tmp_path / "config.json").read_text())["host"] == "h"


def wav_synth(calls, seconds=0.1, rate=16000):
    import wave

    def synth(voice, text, destination, length_scale):
        calls.append(text)
        with wave.open(str(destination), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x01\x00" * int(seconds * rate))
    return synth


def test_prompt_pause_renders_two_parts_with_silence_between(tmp_path):
    # 2026-10-23: Dotty greets the wake word at once, so "Hi E S P. What is
    # your name?" in one breath had the question spoken over the greeting.
    import wave
    (tmp_path / "config.json").write_text(json.dumps({"host": "h"}))
    case = {"id": "W", "prompt": "Hi E S P. What is your name?",
            "prompt_pause_after": "Hi E S P.", "prompt_pause_seconds": 4}
    calls = []
    out = render.render(tmp_path, "voices/en_US-ryan-high.onnx", synth=wav_synth(calls),
                        catalogue={"cases": [case]})
    assert out["rendered"] == ["W"]
    assert calls == ["Hi E S P.", "What is your name?"]
    with wave.open(str(tmp_path / "prompts/W.wav")) as w:
        frames = w.readframes(w.getnframes())
        assert abs(w.getnframes() / w.getframerate() - 4.2) < 0.01
    assert frames[1600 * 2:(1600 + 64000) * 2] == b"\x00" * 128000
    entry = json.loads((tmp_path / "config.json").read_text())["prompt_wavs"]["W"]
    assert entry["text"] == case["prompt"]
    assert entry["renderer"].endswith("pause=4s after 'Hi E S P.'")
