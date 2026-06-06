#!/usr/bin/env python3
"""Bridge.py memory_lookup oracle — runs the *exact* Python search +
format the production bridge does, against a snapshot of brain.db.

Usage:
    python3 oracle.py <brain.db> "<query1>" "<query2>" ...

Outputs one JSON line per query (NDJSON) to stdout:
    {"query": "...", "expected": "..."}

The TS test runner consumes this NDJSON and asserts the Node port
produces byte-identical strings for the same inputs. Keeping the oracle
in Python guarantees the spec stays anchored to bridge.py rather than
to whatever the TS port happens to do — if the two diverge, the test
fails loudly.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


# Ported from the (now-removed, #111) bridge.py:_voice_memory_search_blocking
# and kept as the spec the TS port must match. The ONE intentional change vs
# the original is the `namespace NOT LIKE 'person_pending:%'` kid-safety gate
# (#53) — added in both this oracle and brain_db.ts together. Don't otherwise
# refactor; if the TS port diverges, fix the TS, not this.
def _voice_memory_search_blocking(db: Path, query: str, limit: int = 5) -> list[dict]:
    if not db.exists():
        return []
    safe = (query or "").replace('"', '""').strip()
    if not safe:
        return []
    fts = f'"{safe}"'
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                # Kid-safety gate (#53): exclude unreviewed person_pending:<id>
                # facts about a minor from free-text search (mirrors brain_db.ts).
                """
                SELECT m.key, m.content, m.category, m.namespace, m.created_at
                FROM memories_fts
                JOIN memories m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ?
                  AND m.namespace NOT LIKE 'person_pending:%'
                ORDER BY rank
                LIMIT ?
                """,
                (fts, limit),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


# Copied from bridge.py:_voice_tool_memory_lookup (lines ~3982-3995).
def _voice_tool_memory_lookup(db: Path, query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "(empty query)"
    rows = _voice_memory_search_blocking(db, q, 5)
    if not rows:
        return "(no memories found)"
    snippets = []
    for r in rows[:3]:
        c = (r.get("content") or "").strip()
        if len(c) > 200:
            c = c[:197] + "..."
        snippets.append(c)
    return " | ".join(snippets)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: oracle.py <brain.db> <query1> [<query2>...]", file=sys.stderr)
        return 2
    db = Path(sys.argv[1])
    for query in sys.argv[2:]:
        expected = _voice_tool_memory_lookup(db, query)
        print(json.dumps({"query": query, "expected": expected}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
