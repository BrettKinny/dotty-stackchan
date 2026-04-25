#!/usr/bin/env python3
"""Generate bridge/assets/purr.opus — cat purr loop for head-pet audio feedback.

Usage
-----
    python scripts/generate-purr.py [--output PATH] [--duration SECS]

Encoding path (tried in order)
-------------------------------
A) sox + opusenc:          apt install sox opus-tools
B) numpy + scipy:          pip install numpy scipy soundfile

Run from the repo root. Output defaults to bridge/assets/purr.opus.
If Opus encoding is unavailable, a WAV is written with instructions.
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SAMPLE_RATE = 24_000
DEFAULT_DURATION = 1.5   # seconds
FADE_IN = 0.15            # seconds
FADE_OUT = 0.20           # seconds
DEFAULT_OUTPUT = Path("bridge/assets/purr.opus")


def _try_sox(output: Path, duration: float) -> bool:
    """Attempt purr generation via sox. Returns True on success."""
    if not shutil.which("sox"):
        return False

    def _run_sox(dest: Path) -> bool:
        cmd = [
            "sox", "-n",
            "-r", str(SAMPLE_RATE),
            "-c", "1",
            str(dest),
            "synth", str(duration), "square", "80",
            "tremolo", "22", "50",
            "lowpass", "200",
            "fade", "t", str(FADE_IN), "0", str(FADE_OUT),
            "gain", "-12",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0

    # Direct .opus output (sox built with libopus).
    if output.suffix == ".opus":
        if _run_sox(output):
            return True
        # Intermediate WAV + opusenc.
        if shutil.which("opusenc"):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                if _run_sox(tmp_path):
                    r = subprocess.run(
                        ["opusenc", "--bitrate", "24", "--framesize", "60",
                         str(tmp_path), str(output)],
                        capture_output=True, timeout=30,
                    )
                    return r.returncode == 0 and output.exists()
            finally:
                tmp_path.unlink(missing_ok=True)
        return False
    return _run_sox(output)


def _write_wav(path: Path, pcm16: "np.ndarray") -> None:  # type: ignore[name-defined]
    """Write a minimal 16-bit mono WAV (no soundfile dependency)."""
    data = pcm16.astype("<i2").tobytes()
    byte_rate = SAMPLE_RATE * 2
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, byte_rate, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


def _generate_numpy(output: Path, duration: float) -> bool:
    """Synthesise purr with numpy/scipy; encode with opusenc or soundfile."""
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed — pip install numpy scipy soundfile", file=sys.stderr)
        return False

    n = int(SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)

    # Square-ish fundamental + harmonics weighted toward the low end.
    wave = np.zeros(n, dtype=np.float64)
    for h, amp in [(1, 1.0), (2, 0.55), (3, 0.22), (4, 0.07)]:
        wave += amp * np.sign(np.sin(2 * np.pi * 80.0 * h * t))
    wave /= max(np.max(np.abs(wave)), 1e-9)

    # 22 Hz tremolo at 50% depth (amplitude range 0.5–1.0).
    wave *= 0.75 + 0.25 * np.sin(2 * np.pi * 22.0 * t)

    # Low-pass at 250 Hz (Butterworth if scipy present, else box-filter).
    try:
        from scipy.signal import butter, lfilter
        b, a = butter(4, 250.0 / (SAMPLE_RATE / 2), btype="low")
        wave = lfilter(b, a, wave)
    except ImportError:
        width = max(1, SAMPLE_RATE // 300)
        wave = np.convolve(wave, np.ones(width) / width, mode="same")

    # Fade-in / fade-out envelope.
    fi = int(FADE_IN * SAMPLE_RATE)
    fo = int(FADE_OUT * SAMPLE_RATE)
    env = np.ones(n)
    env[:fi] = np.linspace(0, 1, fi)
    env[n - fo:] = np.linspace(1, 0, fo)
    wave *= env

    # Scale to -12 dBFS peak.
    peak = np.max(np.abs(wave))
    if peak > 0:
        wave *= (10 ** (-12 / 20)) / peak
    pcm16 = np.clip(wave, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)

    need_opus = output.suffix == ".opus"
    if not need_opus:
        try:
            import soundfile as sf
            sf.write(str(output), pcm16, SAMPLE_RATE)
        except ImportError:
            _write_wav(output, pcm16)
        return output.exists()

    # Opus output: WAV → opusenc.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _write_wav(tmp_path, pcm16)
        if shutil.which("opusenc"):
            r = subprocess.run(
                ["opusenc", "--bitrate", "24", "--framesize", "60",
                 str(tmp_path), str(output)],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and output.exists():
                return True
            print(f"opusenc failed (rc={r.returncode}): {r.stderr.decode()[:200]}",
                  file=sys.stderr)

        # soundfile Opus (rare but supported on some builds).
        try:
            import soundfile as sf
            sf.write(str(output), pcm16, SAMPLE_RATE, subtype="OPUS")
            if output.exists():
                return True
        except Exception:
            pass

        # Final fallback: write WAV and show instructions.
        wav_path = output.with_suffix(".wav")
        _write_wav(wav_path, pcm16)
        print(
            f"⚠  Opus encoding unavailable. WAV written to {wav_path}.\n"
            "   Finish with:\n"
            f"     apt install opus-tools\n"
            f"     opusenc --bitrate 24 --framesize 60 {wav_path} {output}",
            file=sys.stderr,
        )
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", "-o", default=str(DEFAULT_OUTPUT),
        help=f"Output file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=DEFAULT_DURATION,
        help=f"Duration in seconds (default: {DEFAULT_DURATION})",
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating purr -> {output}  ({args.duration}s, {SAMPLE_RATE} Hz mono)")

    if _try_sox(output, args.duration):
        print(f"✓ sox path  -> {output} ({output.stat().st_size} bytes)")
        return

    if _generate_numpy(output, args.duration):
        print(f"✓ numpy path -> {output} ({output.stat().st_size} bytes)")
        return

    print(
        "✗ Both paths failed. Install one of:\n"
        "    apt install sox opus-tools\n"
        "    pip install numpy scipy soundfile",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
