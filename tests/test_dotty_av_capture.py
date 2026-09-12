"""Capture failure must never trigger speaker playback (Codex GPT-6 assisted)."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("media", ROOT / "scripts/dotty_av_media.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


@pytest.mark.parametrize("streams", [[], [{"codec_type": "video", "duration": "1"}],
    [{"codec_type": "video", "duration": "nan"}, {"codec_type": "audio", "duration": "1"}],
    [{"codec_type": "video", "duration": "5"}, {"codec_type": "audio", "duration": "1"}]])
def test_invalid_streams_rejected(monkeypatch, streams):
    monkeypatch.setattr(media, "probe", lambda _: {"streams": streams})
    with pytest.raises(ValueError):
        media.verify("unused")


def test_complete_but_truncated_capture_rejected(monkeypatch):
    monkeypatch.setattr(media, "probe", lambda _: {"streams": [
        {"codec_type": "video", "duration": "5", "width": 1280, "height": 720},
        {"codec_type": "audio", "duration": "5", "sample_rate": "32000", "channels": 2}]})
    with pytest.raises(ValueError, match="truncated"):
        media.verify("unused", expected=30)


def test_timestamp_stretched_audio_rejected(monkeypatch):
    monkeypatch.setattr(media, "probe", lambda _: {"streams": [
        {"codec_type": "video", "duration": "36", "width": 1280, "height": 720},
        {"codec_type": "audio", "duration": "36", "sample_rate": "32000", "channels": 2}]})
    monkeypatch.setattr(media, "audio_continuity", lambda *_: {
        "decoded_seconds": 2.14, "decoded_samples": 68544, "max_gap_seconds": 2.3})
    with pytest.raises(ValueError, match="audio sample loss"):
        media.verify("unused")


def test_failed_camera_prevents_speaker_playback(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    programs = {
        "pactl": "echo test-sink",
        "espeak-ng": 'while [ "$1" != "-w" ]; do shift; done; shift; printf speech > "$1"',
        "ffprobe": "echo 2",
        "ffmpeg": "exit 42",
        "pw-play": f'touch "{tmp_path / "PLAYED"}"',
    }
    for name, body in programs.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    result = subprocess.run(["bash", ROOT / "scripts/dotty-av-test.sh", "run", "hello", "5", tmp_path / "capture.mp4"],
        env={**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"],
             "XDG_RUNTIME_DIR": str(tmp_path), "DOTTY_AV_VOLUME": "20"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "capture failed before playback" in result.stderr
    assert not (tmp_path / "PLAYED").exists()


def test_unbounded_recording_rejected(tmp_path):
    result = subprocess.run(["bash", ROOT / "scripts/dotty-av-test.sh", "record", "181", tmp_path / "capture.mp4"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (tmp_path / "capture.mp4").exists()
