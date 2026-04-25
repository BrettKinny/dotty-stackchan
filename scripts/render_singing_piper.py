"""Render Dotty's Macarena vocal track using Piper with per-phrase pitch shift.

This is a quick-and-dirty "singing" renderer: it synthesizes each lyric phrase
through the existing Piper voice model at varying pitch_scale + length_scale
values, then places each rendered phrase at its beat-aligned position in a
master buffer. Output is a 24 kHz mono int16 WAV that drops directly into the
audio injection path used by `_handle_dance()`.

This sounds like pitched/melodic speech, not real singing. For higher quality,
use Phase 2 (DiffSinger via OpenUtau) and `postprocess_song.py`.

Run inside the xiaozhi-server container (which has piper, scipy, numpy):

    docker compose exec xiaozhi-esp32-server python /opt/xiaozhi-esp32-server/scripts/render_singing_piper.py

(Mount the scripts/ directory into the container, or copy the script in first.)

Or run from a local venv with: pip install piper-tts numpy scipy
"""

from __future__ import annotations

import argparse
import sys
import wave
from math import gcd
from pathlib import Path

import numpy as np
from piper.voice import PiperVoice, SynthesisConfig
from scipy import signal


# Match dances.py — keep these in sync.
BEAT_MS = 582
TOTAL_BEATS = 48
TARGET_SR = 24000

# (start_beat, end_beat, lyric, pitch_scale, length_scale)
# pitch_scale > 1.0 = higher pitch (chipmunk via faster resample)
# length_scale: Piper's native speed; <1.0 faster, >1.0 slower (preserves pitch)
PHRASES: list[tuple[int, int, str, float, float]] = [
    # --- Verse 1 (beats 0-16) ---
    (0,  4,  "Dale a tu cuerpo alegria Macarena",            1.10, 1.00),
    (4,  8,  "Que tu cuerpo es pa darle alegria y cosa buena", 1.15, 0.95),
    (8,  12, "Dale a tu cuerpo alegria Macarena",            1.10, 1.00),
    (12, 16, "Hey Macarena",                                 1.30, 1.10),

    # --- "Heeey Macarena!" (beats 16-21) ---
    (16, 21, "Heeeey Macarena",                              1.35, 1.50),

    # --- Jump turn (beats 21-24) — short shout ---
    (21, 24, "Aye!",                                         1.40, 0.80),

    # --- Verse 2 (beats 24-40) ---
    (24, 28, "Macarena tiene un novio que se llama",         1.10, 1.00),
    (28, 32, "Que se llama de apellido Vitorino",            1.15, 0.95),
    (32, 36, "Y en la jura de bandera del muchacho",         1.10, 1.00),
    (36, 40, "Se la dio con dos amigos",                     1.15, 0.95),

    # --- Second "Heeey Macarena!" (beats 40-43) ---
    (40, 43, "Heeeey Macarena",                              1.35, 1.20),

    # --- Final shout + bow (beats 43-48) ---
    (43, 48, "Aye Macarena",                                 1.40, 1.00),
]


def synthesize_phrase(
    voice: PiperVoice, text: str, length_scale: float
) -> np.ndarray:
    """Synthesize text via Piper, return int16 PCM at the voice's native rate."""
    syn_cfg = SynthesisConfig(length_scale=length_scale)
    raw = bytearray()
    for chunk in voice.synthesize(text, syn_config=syn_cfg):
        if chunk and chunk.audio_int16_bytes:
            raw.extend(chunk.audio_int16_bytes)
    if not raw:
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(bytes(raw), dtype=np.int16).copy()


def pitch_shift_to_target(
    pcm: np.ndarray, src_rate: int, pitch_scale: float, target_rate: int
) -> np.ndarray:
    """Pitch-shift PCM via the resample-poly trick used in piper_local.py.

    Treat the source as if it were faster than reality. Resampling to the real
    target rate then produces fewer output samples → higher pitch + shorter.
    """
    if pcm.size == 0:
        return pcm
    effective_rate = max(1, int(round(src_rate * pitch_scale)))
    if effective_rate == target_rate:
        return pcm
    g = gcd(effective_rate, target_rate)
    up = target_rate // g
    down = effective_rate // g
    out = signal.resample_poly(pcm, up, down)
    return np.clip(out, -32768, 32767).astype(np.int16)


def build_master_track(voice: PiperVoice, src_rate: int) -> np.ndarray:
    """Render each phrase, place it in the master buffer at its beat offset."""
    total_samples = int(TOTAL_BEATS * BEAT_MS / 1000.0 * TARGET_SR)
    master = np.zeros(total_samples, dtype=np.int16)

    for start_beat, end_beat, lyric, pitch, length in PHRASES:
        start_sample = int(start_beat * BEAT_MS / 1000.0 * TARGET_SR)
        slot_samples = int((end_beat - start_beat) * BEAT_MS / 1000.0 * TARGET_SR)

        print(f"[beats {start_beat:>2}-{end_beat:>2}] {lyric!r}  pitch={pitch} length={length}")
        raw = synthesize_phrase(voice, lyric, length)
        shifted = pitch_shift_to_target(raw, src_rate, pitch, TARGET_SR)

        # Trim if longer than the beat slot, leave silence-padding if shorter.
        if shifted.size > slot_samples:
            shifted = shifted[:slot_samples]

        end_sample = start_sample + shifted.size
        if end_sample > total_samples:
            end_sample = total_samples
            shifted = shifted[: end_sample - start_sample]

        master[start_sample:end_sample] = shifted

    return master


def write_wav(path: Path, pcm: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_SR)
        wf.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render Dotty's Macarena vocal track via Piper pitch-shifting."
    )
    parser.add_argument(
        "--model",
        default="/opt/xiaozhi-esp32-server/models/piper/en_GB-cori-medium.onnx",
        help="Path to Piper .onnx voice model",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to Piper .onnx.json config (defaults to <model>.json)",
    )
    parser.add_argument(
        "--out",
        default="/opt/xiaozhi-esp32-server/config/assets/songs/macarena.wav",
        help="Output WAV path",
    )
    args = parser.parse_args()

    model_path = Path(args.model)
    config_path = Path(args.config) if args.config else Path(str(model_path) + ".json")
    out_path = Path(args.out)

    if not model_path.exists():
        print(f"ERROR: model not found: {model_path}", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 1

    print(f"Loading Piper voice: {model_path}")
    voice = PiperVoice.load(str(model_path), str(config_path))
    src_rate = int(voice.config.sample_rate)
    print(f"Source rate: {src_rate} Hz, target: {TARGET_SR} Hz, total beats: {TOTAL_BEATS}")

    master = build_master_track(voice, src_rate)
    write_wav(out_path, master)
    duration = master.size / TARGET_SR
    print(f"Wrote {out_path} ({duration:.2f}s, {master.size} samples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
