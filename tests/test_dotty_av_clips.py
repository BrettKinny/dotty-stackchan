"""Clip provenance regression checks. AI-assisted: OpenAI Codex (GPT-6)."""
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location("dotty_av_clips", Path(__file__).parents[1] / "scripts/dotty_av_clips.py")
clips = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clips)


@pytest.fixture
def case(tmp_path, monkeypatch):
    folder = tmp_path / "cases" / "funny-1"
    folder.mkdir(parents=True)
    (folder / "raw.mp4").write_bytes(b"original evidence")
    (folder / "result.json").write_text(json.dumps({
        "verdict": "PASS", "commit": "abc123", "response_offset": 8,
        "started": "2026-09-12T23:59:59+10:00", "case": {"prompt": "<Hello>"}}))
    (folder / "response.json").write_text(json.dumps({"segments": [
        {"start": 2, "end": 4, "text": " Hello there! "}]}))
    monkeypatch.setattr(clips, "media_duration", lambda _: 30)
    commands = []
    monkeypatch.setattr(clips, "execute", lambda command: commands.append(command) or SimpleNamespace(stdout=""))
    return tmp_path, folder, commands


def test_captions_align_response_then_trim():
    srt, cues = clips.captions({"segments": [{"start": 2, "end": 6, "text": "Test"}]}, 8, 11, 13)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert cues[0]["source_start"] == 10
    assert cues[0]["source_end"] == 14


@pytest.mark.parametrize("name", ["../other", "/tmp/video", "foo/bar", "", "a\\b"])
def test_reject_path_identifiers(name):
    with pytest.raises(ValueError):
        clips.identifier(name)


def test_export_preserves_source_and_defaults_to_review(case):
    session, folder, commands = case
    destination = clips.export(session, "funny-1", name="take-one", start=9, end=14)
    assert destination.parent.name == "needs-review"
    assert (folder / "raw.mp4").read_bytes() == b"original evidence"
    manifest = clips.load(destination / "manifest.json")
    assert manifest["source"]["start_seconds"] == 9
    assert manifest["source"]["case_started"] == "2026-09-12T23:59:59+10:00"
    assert manifest["commit"] == "abc123"
    assert manifest["captions"]["state"] == "provisional"
    assert "00:00:01,000 --> 00:00:03,000" in (destination / "captions.srt").read_text()
    assert all("-n" in command for command in commands)
    assert "&lt;Hello&gt;" in (destination / "index.html").read_text()
    with pytest.raises(FileExistsError):
        clips.export(session, "funny-1", name="take-one", start=9, end=14)
    assert len(commands) == 2


def test_case_symlink_cannot_escape_session(case, tmp_path):
    session, _, _ = case
    (session / "cases" / "escape").symlink_to(session.parent)
    with pytest.raises(ValueError, match="escapes"):
        clips.export(session, "escape")


@pytest.mark.parametrize("start,end", [(-1, 2), (5, 4), (0, 31), (float("nan"), 10)])
def test_invalid_intervals_create_no_export(case, start, end):
    session, _, _ = case
    with pytest.raises(ValueError):
        clips.export(session, "funny-1", start=start, end=end)
    assert not (session / "clips").exists()


def test_failed_test_stays_labelled_even_with_review_gates(case):
    session, folder, _ = case
    result = clips.load(folder / "result.json")
    result["verdict"] = "FAIL"
    (folder / "result.json").write_text(json.dumps(result))
    destination = clips.export(session, "funny-1", reviewed=clips.GATES)
    assert destination.parent.name == "failures"


def test_missing_response_offset_does_not_guess_caption_timing(case):
    session, folder, _ = case
    result = clips.load(folder / "result.json")
    del result["response_offset"]
    (folder / "result.json").write_text(json.dumps(result))
    destination = clips.export(session, "funny-1")
    assert not (destination / "captions.srt").exists()
    assert "missing response_offset" in clips.load(destination / "manifest.json")["captions"]["state"]


def test_index_is_new_snapshot_and_local_links(case):
    session, _, _ = case
    clips.export(session, "funny-1")
    first, second = clips.index(session), clips.index(session)
    assert first != second and first.exists() and second.exists()
    assert "../cases/funny-1/raw.mp4" in first.read_text()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg unavailable")
def test_synthetic_media_round_trip(tmp_path):
    folder = tmp_path / "cases" / "synthetic"
    folder.mkdir(parents=True)
    # Generated patterns and tone only: no household media, microphone or speaker.
    clips.execute(["ffmpeg", "-nostdin", "-n", "-v", "error", "-f", "lavfi", "-i",
                   "color=c=blue:s=320x240:r=10:d=2", "-f", "lavfi", "-i",
                   "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-shortest", str(folder / "raw.mp4")])
    (folder / "result.json").write_text(json.dumps({"verdict": "INCONCLUSIVE"}))
    destination = clips.export(tmp_path, "synthetic")
    assert clips.media_duration(destination / "clean.mp4") == pytest.approx(2, abs=.2)
    streams = json.loads(clips.execute(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                        str(destination / "clean.mp4")]).stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert (destination / "thumbnail.jpg").stat().st_size > 0


# --- vertical crop for Shorts framing. AI-assisted: Claude. ---

def test_crop_fills_the_vertical_frame_and_is_recorded(case):
    session, _folder, commands = case
    destination = clips.export(session, "funny-1", name="cropped", start=9, end=14, crop="405:720:368:0")
    video_filter = commands[0][commands[0].index("-vf") + 1]
    assert video_filter.startswith("crop=405:720:368:0,scale=1080:1920")
    assert "pad=" not in video_filter
    manifest = clips.load(destination / "manifest.json")
    assert manifest["layout"] == "1080x1920 cropped from source region 405:720:368:0"


@pytest.mark.parametrize("crop", ["405x720", "405:720:368", "0:720:0:0", "720:405:0:0", "a:b:c:d", "405:720:-1:0"])
def test_crop_must_be_a_portrait_source_region(case, crop):
    session, _folder, commands = case
    with pytest.raises(ValueError):
        clips.export(session, "funny-1", name="bad-crop", start=9, end=14, crop=crop)
    assert commands == []
    assert not list((session / "clips").glob("*/bad-crop"))
