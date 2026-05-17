#!/usr/bin/env python3
"""Bridge.py remember oracle — runs the *exact* Python INSERT path the
production bridge does, then dumps the inserted row as JSON.

Usage:
    python3 remember_oracle.py <brain.db> <now-iso> <id-uuid> <fact> \\
        [--category=<c>] [--namespace=<n>] [--importance=<f>] [--session-id=<s>]

Outputs a single JSON object on stdout matching the row written to the
`memories` table. The TS test runner consumes this, writes the same row
via storeMemory() with the same (now, id) seed, reads it back, and
asserts byte-equal. If bridge.py and the TS port disagree on key
format, column ordering, or truncation, the test fails loudly.

Defaults match bridge.py's /api/voice/remember handler:
    category=core, namespace=voice, importance=0.7, session_id=null
And the 300-char fact truncation that handler applies before storing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


FACT_MAX_CHARS = 300  # bridge.py /api/voice/remember truncates here.


# Copied from bridge.py:_voice_memory_store_blocking (lines ~3865-3899).
# Do NOT refactor — this is the spec.
def _voice_memory_store_blocking(
    db: Path, *, content: str, category: str, namespace: str,
    importance: float, session_id: str | None, now: str, mem_id: str,
) -> dict | None:
    if not content or not content.strip():
        return None
    if not db.exists():
        return None
    trimmed = content.strip()
    base_key = f"voice_{category}_{now}_{mem_id[:8]}"
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            conn.execute(
                """
                INSERT INTO memories
                  (id, key, content, category, namespace,
                   importance, created_at, updated_at, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mem_id, base_key, trimmed, category, namespace,
                 importance, now, now, session_id),
            )
            conn.commit()
            cur = conn.execute(
                """
                SELECT id, key, content, category, namespace,
                       importance, created_at, updated_at, session_id
                FROM memories WHERE id = ?
                """,
                (mem_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = ["id", "key", "content", "category", "namespace",
                    "importance", "created_at", "updated_at", "session_id"]
            return dict(zip(cols, row))
        finally:
            conn.close()
    except Exception as e:
        print(f"oracle error: {e}", file=sys.stderr)
        return None


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 4:
        print(
            "usage: remember_oracle.py <db> <now> <id> <fact> "
            "[--category=c] [--namespace=n] [--importance=f] "
            "[--session-id=s]",
            file=sys.stderr,
        )
        return 2
    db = Path(args[0])
    now = args[1]
    mem_id = args[2]
    fact = args[3]
    opts: dict = {
        "category": "core",
        "namespace": "voice",
        "importance": 0.7,
        "session_id": None,
    }
    for flag in args[4:]:
        if not flag.startswith("--") or "=" not in flag:
            print(f"bad flag: {flag}", file=sys.stderr)
            return 2
        key, val = flag[2:].split("=", 1)
        key = key.replace("-", "_")
        if key == "importance":
            opts[key] = float(val)
        elif key == "session_id" and val == "":
            opts[key] = None
        else:
            opts[key] = val

    # Match the /api/voice/remember handler's trim+truncate before store.
    truncated = (fact or "").strip()[:FACT_MAX_CHARS]
    row = _voice_memory_store_blocking(
        db, content=truncated, now=now, mem_id=mem_id, **opts,
    )
    if row is None:
        print(json.dumps({"ok": False}))
        return 0
    print(json.dumps({"ok": True, "row": row}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
