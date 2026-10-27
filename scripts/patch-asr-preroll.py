"""Keep more audio from before the VAD fires, so ASR hears the first word.

Upstream ``core/providers/asr/base.py`` keeps the last 10 packets (~600 ms of
60 ms Opus frames) while nobody is speaking. The VAD needs a few voiced frames
before it fires, so a soft first word was lost: in the 2026-10-20 A/V run
"Do you remember…" reached ASR as "remember…" (about 0.8 s missing). Twenty
packets (~1.2 s) covers that.

Usage: patch-asr-preroll.py <core/providers/asr/base.py>
"""

from pathlib import Path
import sys


PREROLL_PACKETS = 20
OLD = "conn.asr_audio = conn.asr_audio[-10:]\n"
NEW = f"conn.asr_audio = conn.asr_audio[-{PREROLL_PACKETS}:]  # Dotty: pre-roll\n"


def patch_source(source: str) -> str:
    if NEW in source:
        return source
    if source.count(OLD) != 1:
        raise ValueError("ASR pre-roll anchor not found exactly once")
    return source.replace(OLD, NEW, 1)


def main(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    try:
        updated = patch_source(source)
    except ValueError as exc:
        raise SystemExit(f"{path}: {exc}")
    if updated != source:
        path.write_text(updated, encoding="utf-8")
    print(f"patched {path.name}: ASR pre-roll {PREROLL_PACKETS} packets")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
