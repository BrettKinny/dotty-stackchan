"""Decoded PCM quality checks. AI-assisted: OpenAI Codex (GPT-6)."""
import importlib.util
import math
from pathlib import Path
import struct

import pytest


spec = importlib.util.spec_from_file_location("dotty_av_media", Path(__file__).parents[1] / "scripts/dotty_av_media.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def pcm(values):
    return struct.pack(f"<{len(values)}f", *values)


def test_silent_recording_warns_without_claiming_interaction():
    result = media.pcm_metrics(pcm([0] * 100))
    assert result["near_silence"] is True
    assert result["rms_dbfs"] is None
    assert result["clipping_ratio"] == 0
    assert "near_silence" in result["warnings"][0]
    assert "verdict" not in result and "interaction" not in result


def test_known_amplitude_metrics():
    result = media.pcm_metrics(pcm([.5, -.5] * 100))
    assert result["rms"] == .5
    assert result["peak"] == .5
    assert result["rms_dbfs"] == pytest.approx(-6.0205999)
    assert result["warnings"] == []


def test_severe_clipping_strictly_above_point_one_percent():
    threshold = media.pcm_metrics(pcm([1.] + [.1] * 999))
    assert threshold["clipping_ratio"] == .001
    assert threshold["warnings"] == []
    severe = media.pcm_metrics(pcm([1., -1.] + [.1] * 998))
    assert severe["clipping_ratio"] == .002
    assert "severe_clipping" in severe["warnings"][0]


def test_lossy_overshoot_retained_and_explained():
    result = media.pcm_metrics(pcm([1.1, -1.2]))
    assert result["peak_dbfs"] > 0
    assert result["clipped_samples"] == 2
    assert "Lossy" in result["note"]


@pytest.mark.parametrize("data", [b"", b"abc", pcm([math.nan]), pcm([math.inf])])
def test_rejects_invalid_pcm(data):
    with pytest.raises(ValueError):
        media.pcm_metrics(data)


def test_decode_is_bounded_and_preserves_channels(monkeypatch):
    commands = []

    def decode(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 45
        return pcm([.1, -.1])

    monkeypatch.setattr(media.subprocess, "check_output", decode)
    result = media.audio_metrics(Path("capture.mp4"))
    assert commands[0][commands[0].index("-t") + 1] == "180"
    assert "-ac" not in commands[0] and "-ar" not in commands[0]
    assert commands[0][-1] == "pipe:1"
    assert result["analysis_limit_seconds"] == 180


def test_verify_adds_quality_without_replacing_continuity(monkeypatch):
    monkeypatch.setattr(media, "probe", lambda _: {"streams": [
        {"codec_type": "video", "duration": "10", "width": 640, "height": 480},
        {"codec_type": "audio", "duration": "10", "sample_rate": "48000", "channels": 2}]})
    monkeypatch.setattr(media, "audio_continuity", lambda *_: {
        "decoded_seconds": 10, "max_gap_seconds": 0})
    monkeypatch.setattr(media, "audio_metrics", lambda _: {"near_silence": True, "warnings": ["silent"]})
    result = media.verify("silent.mp4")
    assert result["decoded_seconds"] == 10
    assert result["audio_quality"]["near_silence"]
    assert "verdict" not in result
