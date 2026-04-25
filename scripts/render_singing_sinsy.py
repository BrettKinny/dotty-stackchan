"""Render a singing track using Sinsy (HMM-based singing voice synthesis).

Phase 2 alternative path that avoids OpenUtau's GUI: this script takes a MIDI
file + lyrics text, builds a MusicXML score with lyrics embedded note-by-note,
synthesizes with pysinsy (local libsinsy via Cython), and writes a WAV.

pysinsy ships only the Japanese voice + dictionary, but Japanese mora
pronunciations of "ma-ka-re-na" are essentially identical to the Spanish/
English ones — write the lyrics in hiragana and the synthesis sounds right.

Usage:
    python scripts/render_singing_sinsy.py macarena.mid lyrics.txt out.wav \
        --tempo 103 --part-index 2 --max-duration-ms 27936

Lyrics file: one syllable per line. Syllables loop if shorter than note count.
"""

from __future__ import annotations

import argparse
import sys
import wave
from math import gcd
from pathlib import Path

import numpy as np
from music21 import converter, note as m21_note, tempo as m21_tempo
from scipy import signal as scipy_signal
import pyphen


TARGET_SR = 24000  # Device's negotiated downlink rate


def syllabify(words: list[str]) -> list[str]:
    """Split English words into syllables using pyphen."""
    dic = pyphen.Pyphen(lang="en")
    out: list[str] = []
    for word in words:
        clean = "".join(c for c in word.lower() if c.isalpha() or c == "'")
        if not clean:
            continue
        sylls = dic.inserted(clean).split("-")
        out.extend(s for s in sylls if s)
    return out


def load_lyrics(path: Path) -> list[str]:
    """Return a flat syllable list. Treat each line as either a syllable or a
    space-separated word run that needs syllabifying."""
    syllables: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # If the line contains spaces, treat as word run; else single syllable.
        if " " in line:
            syllables.extend(syllabify(line.split()))
        else:
            syllables.append(line)
    return syllables


def build_singing_score(
    midi_path: Path, part_index: int, syllables: list[str], bpm: float
):
    """Parse MIDI, isolate the melody part, attach lyrics syllable-by-syllable,
    set tempo. Returns a music21 Score ready to write as MusicXML."""
    src = converter.parse(str(midi_path))
    if part_index >= len(src.parts):
        raise SystemExit(
            f"part-index {part_index} out of range (score has {len(src.parts)})"
        )
    melody_part = src.parts[part_index]

    # Replace tempo marks with the requested BPM.
    for old in list(melody_part.flatten().getElementsByClass(m21_tempo.MetronomeMark)):
        old.activeSite.remove(old)
    melody_part.insert(0, m21_tempo.MetronomeMark(number=bpm))

    notes = [
        n for n in melody_part.flatten().notesAndRests if isinstance(n, m21_note.Note)
    ]
    if not notes:
        raise SystemExit(f"part {part_index} has no notes")

    print(f"Attaching {len(syllables)} syllables (looped) to {len(notes)} notes")
    for i, n in enumerate(notes):
        syl = syllables[i % len(syllables)]
        n.addLyric(syl)

    # Wrap as a Score with just the melody part so Sinsy doesn't render harmony.
    from music21 import stream
    score = stream.Score()
    score.insert(0, melody_part)
    return score


def write_musicxml(score, out_path: Path) -> None:
    score.write("musicxml", fp=str(out_path))


def synthesize_with_pysinsy(musicxml_path: Path):
    """Run libsinsy on the MusicXML. Returns (pcm_float, sample_rate)."""
    import pysinsy  # local import: optional dep
    print("Synthesizing with libsinsy...")
    wav, sr = pysinsy.synthesize(str(musicxml_path))
    return np.asarray(wav, dtype=np.float64), int(sr)


def postprocess(
    wav: np.ndarray, src_sr: int, *, target_sr: int = TARGET_SR,
    max_duration_ms: int | None = None, normalize: bool = True,
) -> np.ndarray:
    """Resample to target_sr, optional duration trim, return int16 PCM."""
    if normalize:
        peak = np.max(np.abs(wav))
        if peak > 0 and peak < 1e10:
            wav = wav / peak * 0.92
        else:
            print(f"WARNING: weird peak {peak}; skipping normalization")

    if src_sr != target_sr:
        g = gcd(src_sr, target_sr)
        up = target_sr // g
        down = src_sr // g
        wav = scipy_signal.resample_poly(wav, up, down)

    if max_duration_ms is not None:
        max_samples = int(max_duration_ms / 1000.0 * target_sr)
        if len(wav) > max_samples:
            print(f"Trimming {len(wav)} → {max_samples} samples ({max_duration_ms}ms)")
            wav = wav[:max_samples]
        elif len(wav) < max_samples:
            wav = np.concatenate([wav, np.zeros(max_samples - len(wav))])

    return np.clip(wav * 32767, -32768, 32767).astype(np.int16)


def write_int16_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render singing via Sinsy.jp from MIDI + lyrics."
    )
    parser.add_argument("midi", type=Path, help="Input MIDI file")
    parser.add_argument("lyrics", type=Path, help="Lyrics file (syllables, one per line)")
    parser.add_argument("output", type=Path, help="Output WAV path")
    parser.add_argument(
        "--part-index", type=int, default=0,
        help="Which MIDI part contains the melody (use --inspect to list)",
    )
    parser.add_argument("--tempo", type=float, default=103.0, help="BPM (default 103 = Macarena)")
    parser.add_argument(
        "--max-duration-ms", type=int, default=27936,
        help="Trim/pad output to this duration. Default 27936 ms = Macarena dance "
             "(BEAT_MS=582 * 48). Pass 0 to disable trimming.",
    )
    parser.add_argument(
        "--keep-xml", action="store_true",
        help="Keep the intermediate MusicXML file next to the output for debugging",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Print MIDI part summary and exit (use this to pick --part-index)",
    )
    args = parser.parse_args()

    if args.inspect:
        score = converter.parse(str(args.midi))
        print(f"{args.midi}: {len(score.parts)} parts")
        for i, part in enumerate(score.parts):
            notes = [
                n for n in part.flatten().notesAndRests
                if isinstance(n, m21_note.Note)
            ]
            if not notes:
                print(f"  [{i}] (no notes)")
                continue
            pitches = [n.pitch.midi for n in notes]
            print(
                f"  [{i}] notes={len(notes):4d}  pitch={min(pitches)}..{max(pitches)}  "
                f"mean={sum(pitches) / len(pitches):.0f}"
            )
        return 0

    if not args.midi.exists():
        print(f"ERROR: midi not found: {args.midi}", file=sys.stderr)
        return 1
    if not args.lyrics.exists():
        print(f"ERROR: lyrics not found: {args.lyrics}", file=sys.stderr)
        return 1

    syllables = load_lyrics(args.lyrics)
    print(f"Loaded {len(syllables)} syllables from {args.lyrics}")

    score = build_singing_score(args.midi, args.part_index, syllables, args.tempo)

    xml_path = args.output.with_suffix(".xml")
    write_musicxml(score, xml_path)
    print(f"Wrote MusicXML: {xml_path}")

    raw_wav, src_sr = synthesize_with_pysinsy(xml_path)
    print(
        f"Raw output: {len(raw_wav)} samples @ {src_sr}Hz "
        f"({len(raw_wav) / src_sr:.2f}s, peak={np.max(np.abs(raw_wav)):.0f})"
    )

    max_ms = args.max_duration_ms if args.max_duration_ms > 0 else None
    pcm = postprocess(raw_wav, src_sr, target_sr=TARGET_SR, max_duration_ms=max_ms)
    write_int16_wav(args.output, pcm, TARGET_SR)
    print(f"Wrote {args.output} ({len(pcm)} samples @ {TARGET_SR}Hz)")

    if not args.keep_xml:
        xml_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
