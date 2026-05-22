#!/usr/bin/env python3
"""recall_person oracle — runs the *exact* Python SELECT path the bridge's
_voice_memory_person_fetch_blocking does, then dumps the rows as JSON.

Usage:
    python3 recall_person_oracle.py <brain.db> <person-id> [--limit=N]

Outputs a single JSON object {"rows": [...]} on stdout. The TS test
runner consumes this, runs fetchPersonMemories() against the same db, and
asserts byte-equal. If bridge.py and the TS port disagree on the SELECT
(namespace key, column set, ORDER BY), the test fails loudly.

Mirrors bridge.py:_voice_memory_person_fetch_blocking (#53) — only the
approved `person:<id>` namespace is read; `person_pending:<id>` is never
returned.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PERSON_MEMORY_MAX_FACTS = 8  # mirrors bridge.py _PERSON_MEMORY_MAX_FACTS


# Copied from bridge.py:_voice_memory_person_fetch_blocking. Do NOT
# refactor — this is the spec.
def _voice_memory_person_fetch_blocking(
    db: Path, person_id: str, limit: int,
) -> list[dict]:
    pid = (person_id or "").strip().lower()
    if not pid or not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT key, content, category, importance,
                       created_at, updated_at
                FROM memories
                WHERE namespace = ?
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?
                """,
                (f"person:{pid}", limit),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"oracle error: {e}", file=sys.stderr)
        return []


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print(
            "usage: recall_person_oracle.py <db> <person-id> [--limit=N]",
            file=sys.stderr,
        )
        return 2
    db = Path(args[0])
    person_id = args[1]
    limit = PERSON_MEMORY_MAX_FACTS
    for flag in args[2:]:
        if flag.startswith("--limit="):
            limit = int(flag.split("=", 1)[1])
        else:
            print(f"bad flag: {flag}", file=sys.stderr)
            return 2

    rows = _voice_memory_person_fetch_blocking(db, person_id, limit)
    print(json.dumps({"rows": rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
