"""Normalize a rendered singing track to the format expected by audio_to_data().

DiffSinger / OpenUtau / NNSVS / VISinger2 all output high-fidelity WAVs
(typically 44.1 kHz or 48 kHz, mono or stereo, float32 or int16). The xiaozhi
device expects 24 kHz mono int16 PCM. This script:

    1. Loads any WAV
    2. Downmixes to mono if stereo
    3. Resamples to 24000 Hz with scipy polyphase
    4. Optionally trims/pads to a fixed duration in milliseconds
    5. Writes 16-bit signed PCM WAV

Usage:
    python postprocess_song.py input.wav output.wav
    python postprocess_song.py input.wav output.wav --duration-ms 27936
"""

from __future__ import annotations

import argparse
import sys
import wave
from math import gcd
from pathlib import Path

import numpy as np
from scipy import signal


TARGET_SR = 24000


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth == 2:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        pcm = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)

    return pcm, sr


def resample_to_target(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return pcm
    g = gcd(src_sr, dst_sr)
    up = dst_sr // g
    down = src_sr // g
    return signal.resample_poly(pcm, up, down)


def fit_to_duration(pcm: np.ndarray, sr: int, duration_ms: int) -> np.ndarray:
    target_samples = int(duration_ms / 1000.0 * sr)
    if pcm.size > target_samples:
        return pcm[:target_samples]
    if pcm.size < target_samples:
        pad = np.zeros(target_samples - pcm.size, dtype=pcm.dtype)
        return np.concatenate([pcm, pad])
    return pcm


def write_int16_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    clipped = np.clip(pcm, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(int16.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize a singing render to 24kHz mono int16 WAV for Dotty."
    )
    parser.add_argument("input", type=Path, help="Input WAV (any sample rate, mono/stereo, int16/int32/float)")
    parser.add_argument("output", type=Path, help="Output WAV (24kHz mono int16)")
    parser.add_argument(
        "--duration-ms",
        type=int,
        default=None,
        help="Trim/pad output to this duration. Macarena choreography is 27936 ms (BEAT_MS=582 * 48 beats).",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    print(f"Loading {args.input}")
    pcm, src_sr = read_wav(args.input)
    print(f"Source: {src_sr} Hz, {pcm.size} samples ({pcm.size / src_sr:.2f}s)")

    pcm = resample_to_target(pcm, src_sr, TARGET_SR)
    print(f"Resampled to {TARGET_SR} Hz: {pcm.size} samples ({pcm.size / TARGET_SR:.2f}s)")

    if args.duration_ms is not None:
        pcm = fit_to_duration(pcm, TARGET_SR, args.duration_ms)
        print(f"Fit to {args.duration_ms} ms: {pcm.size} samples")

    write_int16_wav(args.output, pcm, TARGET_SR)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
