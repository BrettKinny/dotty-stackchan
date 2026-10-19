"""Patch pinned xiaozhi TTS consumers for explicit server-push arbitration.

Server-pushed speech (/xiaozhi/admin/say) registers its sentence ID in
``conn._dotty_server_push_sentence_ids`` so a chat turn that replaces
``conn.sentence_id`` cannot silently drop it (#104). Upstream filters stale
sentences in two places and both need the exemption:

* the TTS text consumers (``core/providers/tts/*.py``) — before synthesis;
* ``sendAudioMessage`` (``core/handle/sendAudioHandle.py``) — after
  synthesis, before the Opus frames go to the device.

Usage: patch-tts-server-push.py <core/providers/tts dir> [<sendAudioHandle.py>]
"""

from pathlib import Path
import sys


OLD = "if message.sentence_id != self.conn.sentence_id:\n"
NEW = (
    "if (\n"
    "                    message.sentence_id != self.conn.sentence_id\n"
    "                    and message.sentence_id not in getattr(\n"
    "                        self.conn, \"_dotty_server_push_sentence_ids\", set()\n"
    "                    )\n"
    "                ):\n"
)

SEND_OLD = "    if sentence_id is not None and sentence_id != conn.sentence_id:\n"
SEND_NEW = (
    "    if (\n"
    "        sentence_id is not None\n"
    "        and sentence_id != conn.sentence_id\n"
    "        and sentence_id not in getattr(\n"
    "            conn, \"_dotty_server_push_sentence_ids\", set()\n"
    "        )\n"
    "    ):\n"
)


def _patch(source: str, old: str, new: str, what: str) -> str:
    if new in source:
        return source
    if old not in source:
        raise ValueError(f"{what} anchor not found")
    return source.replace(old, new, 1)


def patch_source(source: str) -> str:
    return _patch(source, OLD, NEW, "sentence-id predicate")


def patch_send_audio_source(source: str) -> str:
    return _patch(source, SEND_OLD, SEND_NEW, "sendAudioMessage sentence-id")


def main(root: Path, send_audio: Path | None = None) -> None:
    patched = 0
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if OLD not in source and NEW not in source:
            continue
        updated = patch_source(source)
        if updated != source:
            path.write_text(updated, encoding="utf-8")
        patched += 1
    if patched == 0:
        raise SystemExit("no xiaozhi TTS consumers contained the expected predicate")
    print(f"patched {patched} TTS consumer(s) for server-push arbitration")
    if send_audio is not None:
        source = send_audio.read_text(encoding="utf-8")
        try:
            updated = patch_send_audio_source(source)
        except ValueError as exc:
            raise SystemExit(f"{send_audio}: {exc}")
        if updated != source:
            send_audio.write_text(updated, encoding="utf-8")
        print(f"patched {send_audio.name} for server-push arbitration")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
