"""Patch pinned xiaozhi TTS consumers for explicit server-push arbitration."""

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


def patch_source(source: str) -> str:
    if NEW in source:
        return source
    if OLD not in source:
        raise ValueError("sentence-id predicate anchor not found")
    return source.replace(OLD, NEW, 1)


def main(root: Path) -> None:
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


if __name__ == "__main__":
    main(Path(sys.argv[1]))
