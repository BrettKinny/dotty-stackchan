#!/usr/bin/env python3
"""Local A/V evidence helpers. AI-assisted: OpenAI Codex (GPT-6).

Verification never infers a robot response from a nonempty audio track.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess


def probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], text=True, timeout=20))


def verify(path, expected=None):
    data = probe(path)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise ValueError("expected exactly one video and one audio stream")
    vd, ad = [float(s.get("duration", 0)) for s in (video[0], audio[0])]
    if not all(math.isfinite(d) and d > 0 for d in (vd, ad)):
        raise ValueError("empty/nonfinite stream duration")
    if abs(vd - ad) > 1:
        raise ValueError("audio/video duration mismatch")
    if expected is not None and min(vd, ad) < expected - 1:
        raise ValueError("truncated capture")
    continuity = audio_continuity(path, int(audio[0]["sample_rate"]))
    if continuity["decoded_seconds"] < ad - max(.25, ad * .02):
        raise ValueError(f"audio sample loss: {continuity['decoded_seconds']:.3f}s decoded across {ad:.3f}s timeline")
    if continuity["max_gap_seconds"] > .15:
        raise ValueError(f"audio discontinuity: {continuity['max_gap_seconds']:.3f}s gap")
    return {"video_seconds": vd, "audio_seconds": ad,
            "width": video[0]["width"], "height": video[0]["height"],
            "sample_rate": audio[0]["sample_rate"], "channels": audio[0]["channels"], **continuity}


def audio_continuity(path, rate):
    frames = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_frames",
        "-show_entries", "frame=pts_time,nb_samples", "-of", "json", str(path)
    ], text=True, timeout=30)).get("frames", [])
    samples, end, gap = 0, None, 0.0
    for frame in frames:
        count = int(frame.get("nb_samples", 0))
        start = float(frame["pts_time"])
        if end is not None:
            gap = max(gap, start - end)
        end = start + count / rate
        samples += count
    return {"decoded_samples": samples, "decoded_seconds": samples / rate, "max_gap_seconds": gap}


def transcribe(path, model_path, output):
    # Explicit disk path + local_files_only prevent network model/media access.
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                         cpu_threads=4, local_files_only=True)
    segments, info = model.transcribe(str(path), language="en", beam_size=3,
                                      vad_filter=True, condition_on_previous_text=False,
                                      word_timestamps=True)
    result = {"model": str(model_path), "language_probability": info.language_probability,
              "segments": [{"start": s.start, "end": s.end, "text": s.text,
                            "no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob,
                            "words": [{"start": w.start, "end": w.end, "word": w.word,
                                       "probability": w.probability} for w in (s.words or [])]}
                           for s in segments]}
    Path(output).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("verify")
    p.add_argument("file", type=Path)
    p.add_argument("--expected", type=float)
    p = sub.add_parser("transcribe")
    p.add_argument("file", type=Path)
    p.add_argument("--model", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify(args.file, args.expected)))
    else:
        print(json.dumps(transcribe(args.file, args.model, args.output)))
