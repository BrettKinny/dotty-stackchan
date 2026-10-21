"""Capture failure must never trigger speaker playback (Codex GPT-6 assisted)."""
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil

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


def fake_run_harness(tmp_path, volume_output, mute_output="Mute: no"):
    """Run `run` mode with fake devices; pactl only reports, and logs its argv."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "pactl.calls"
    programs = {
        "pactl": f'''echo "$*" >> "{calls}"
case "$1" in
  get-sink-volume) printf '%s\\n' "{volume_output}" ;;
  get-sink-mute) echo "{mute_output}" ;;
  get-default-sink) echo test-sink ;;
esac''',
        "espeak-ng": 'while [ "$1" != "-w" ]; do shift; done; shift; printf speech > "$1"',
        "ffprobe": "echo 2",
        # Stays alive past the 2 s pre-roll and leaves a non-empty output.
        "ffmpeg": 'for last; do :; done; printf x > "$last"; sleep 3',
        "pw-play": f'touch "{tmp_path / "PLAYED"}"',
    }
    for name, body in programs.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    result = subprocess.run(["bash", ROOT / "scripts/dotty-av-test.sh", "run", "hello", "5", tmp_path / "capture.mp4"],
        env={**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"],
             "XDG_RUNTIME_DIR": str(tmp_path), "DOTTY_AV_VOLUME": "20", "DOTTY_AV_SINK": "test-sink"},
        capture_output=True, text=True, timeout=20)
    return result, calls.read_text().splitlines()


@pytest.mark.parametrize("volume, mute", [
    ("Volume: front-left: 19661 /  30% / -31.37 dB,   front-right: 19661 /  30% / -31.37 dB", "Mute: no"),
    ("Volume: front-left: 13107 /  20% / -41.94 dB,   front-right: 19661 /  30% / -31.37 dB", "Mute: no"),
    ("Volume: front-left: 13107 /  20% / -41.94 dB,   front-right: 13107 /  20% / -41.94 dB", "Mute: yes"),
    ("", "Mute: no"),
])
def test_run_refuses_playback_when_speaker_changed_and_never_sets_it(tmp_path, volume, mute):
    # 2026-10-21 review: run mode must not unmute or turn up a household speaker.
    result, calls = fake_run_harness(tmp_path, volume, mute)
    assert result.returncode == 3, result.stderr
    assert "speaker volume/mute changed since check" in result.stderr
    assert not (tmp_path / "PLAYED").exists()
    assert not any(call.startswith("set-") for call in calls)


def test_run_plays_at_the_checked_level_without_setting_it(tmp_path):
    result, calls = fake_run_harness(
        tmp_path, "Volume: front-left: 13107 /  20% / -41.94 dB,   front-right: 13107 /  20% / -41.94 dB")
    assert (tmp_path / "PLAYED").exists(), result.stderr
    assert "get-sink-volume test-sink" in calls and "get-sink-mute test-sink" in calls
    assert not any(call.startswith("set-") for call in calls)


def test_unbounded_recording_rejected(tmp_path):
    result = subprocess.run(["bash", ROOT / "scripts/dotty-av-test.sh", "record", "181", tmp_path / "capture.mp4"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (tmp_path / "capture.mp4").exists()


def capture_function(name, *args, lossless="1", backend="pulse"):
    # Load definitions only: no command dispatch, devices, or playback.
    definitions = (ROOT / "scripts/dotty-av-test.sh").read_text().split('\ncmd=', 1)[0]
    return subprocess.run(["bash", "-c", 'source /dev/stdin; "$@"', "fixture", name,
                           *map(str, args)], input=definitions, capture_output=True, text=True, timeout=5,
                          env={**os.environ, "DOTTY_AV_LOSSLESS_AUDIO": lossless,
                               "DOTTY_AV_AUDIO_BACKEND": backend})


def test_lossless_sidecar_maps_same_audio_and_bounds_each_output(tmp_path):
    output = tmp_path / "unique-case.mp4"
    result = capture_function("capture_output_args", "7", output)
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    split = argv.index(str(output))
    mp4, wav = argv[:split], argv[split + 1:]
    assert mp4[mp4.index("-t") + 1] == "7"
    assert wav[wav.index("-t") + 1] == "7"
    assert mp4[:4] == ["-map", "0:v:0", "-map", "1:a:0"]
    assert wav[:2] == ["-map", "1:a:0"]
    assert wav[wav.index("-c:a") + 1] == "pcm_f32le"
    assert wav[wav.index("-ar") + 1] == "32000"
    assert wav[wav.index("-ac") + 1] == "2"
    assert wav[-1] == str(output.with_suffix(".capture.wav"))


def test_default_capture_has_no_lossless_output_or_float_input(tmp_path):
    result = capture_function("capture_output_args", "7", tmp_path / "default.mp4", lossless="0")
    assert result.returncode == 0, result.stderr
    assert ".capture.wav" not in result.stdout and "pcm_f32le" not in result.stdout
    inputs = capture_function("capture_args", lossless="0")
    assert "pcm_f32le" not in inputs.stdout


def test_opt_in_pulse_requests_float_before_reading_input():
    result = capture_function("capture_args")
    argv = result.stdout.splitlines()
    pulse = argv.index("pulse")
    assert argv.index("pcm_f32le") > pulse
    assert argv.index("pcm_f32le") < len(argv) - 2  # before final -i source
    assert "pcm_f32le" not in capture_function("capture_args", backend="alsa").stdout


@pytest.mark.parametrize("kind", ["record", "run"])
def test_existing_sidecar_is_never_overwritten_or_played(tmp_path, kind):
    sidecar = tmp_path / "capture.capture.wav"
    sidecar.write_bytes(b"existing evidence")
    argv = ["bash", ROOT / "scripts/dotty-av-test.sh", kind]
    if kind == "run":
        argv.append("fixture no playback")
    argv.extend(["5", tmp_path / "capture.mp4"])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                            env={**os.environ, "DOTTY_AV_LOSSLESS_AUDIO": "1"})
    assert result.returncode != 0
    assert "output already exists" in result.stderr
    assert sidecar.read_bytes() == b"existing evidence"
    assert not (tmp_path / "capture.mp4").exists()


def test_same_process_synthetic_float_sidecar_preserves_overshoots(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg unavailable")
    output = tmp_path / "synthetic.mp4"
    rendered = capture_function("capture_output_args", "1", output)
    assert rendered.returncode == 0, rendered.stderr
    subprocess.run(["ffmpeg", "-nostdin", "-n", "-v", "error",
                    "-f", "lavfi", "-i", "color=size=160x120:rate=10",
                    "-f", "lavfi", "-i", "aevalsrc=1.05*sin(2*PI*440*t)|0.2*sin(2*PI*660*t):s=32000",
                    *rendered.stdout.splitlines()], check=True, capture_output=True, timeout=15)
    sidecar = output.with_suffix(".capture.wav")
    streams = media.probe(sidecar)["streams"]
    assert len(streams) == 1
    assert streams[0]["codec_name"] == "pcm_f32le"
    assert streams[0]["channels"] == 2 and streams[0]["sample_rate"] == "32000"
    assert float(streams[0]["duration"]) == pytest.approx(1, abs=.01)
    assert media.audio_metrics(sidecar)["peak"] > 1.04
    assert media.verify(output)["audio_seconds"] == pytest.approx(1, abs=.05)
