#!/usr/bin/env python3
"""Local A/V evidence helpers. AI-assisted: OpenAI Codex (GPT-6).

Verification never infers a robot response from a nonempty audio track.
"""
import argparse
from array import array
import json
import math
from pathlib import Path
import subprocess
import sys


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
            "sample_rate": audio[0]["sample_rate"], "channels": audio[0]["channels"],
            **continuity, "audio_quality": audio_metrics(path)}


def pcm_metrics(pcm):
    """Measure decoded float32 PCM without inferring speech or interaction success.

    Near-full-scale samples include lossy-decoder overshoots; this is a clipping
    warning, not proof that the microphone's analogue input clipped.
    """
    if len(pcm) % 4:
        raise ValueError("incomplete float32 PCM sample")
    samples = array("f")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ValueError("empty decoded PCM")
    peak = squared_sum = clipped = 0
    for sample in samples:
        if not math.isfinite(sample):
            raise ValueError("nonfinite decoded PCM sample")
        magnitude = abs(sample)
        peak = max(peak, magnitude)
        squared_sum += sample * sample
        clipped += magnitude >= .999
    rms = math.sqrt(squared_sum / len(samples))
    ratio = clipped / len(samples)
    silent = rms < .001 and peak < 10 ** (-45 / 20)
    warnings = []
    if ratio > .001:
        warnings.append("severe_clipping: over 0.1% of decoded samples near or above full scale")
    if silent:
        warnings.append("near_silence: RMS below -60 dBFS and peak below -45 dBFS")
    return {"pcm_samples": len(samples), "rms": rms, "peak": peak,
            "rms_dbfs": 20 * math.log10(rms) if rms else None,
            "peak_dbfs": 20 * math.log10(peak) if peak else None,
            "clipped_samples": clipped, "clipping_ratio": ratio,
            "clipping_threshold": .999, "near_silence": silent, "warnings": warnings,
            "note": "Decoded PCM only; signal presence does not verify speech or a robot response. "
                    "Lossy decoding can produce full-scale overshoots."}


def audio_metrics(path):
    # Preserve native channels/rate: downmixing or resampling can mask saturation.
    # Bound decoding to the maximum permitted case duration; never play audio.
    pcm = subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", "180",
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"
    ], timeout=45)
    return {**pcm_metrics(pcm), "analysis_limit_seconds": 180}


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
