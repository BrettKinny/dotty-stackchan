// Regression: memory_lookup's FTS search must NOT surface unreviewed
// `person_pending:<id>` facts about a minor (#53 kid-safety gate). Builds a
// throwaway brain.db (real schema) with one approved and one pending row that
// both phrase-match the query, and asserts only the approved row comes back.
//
// Hermetic — no DOTTY_BRAIN_DB_SNAPSHOT needed (unlike memory_lookup.test.ts).

import Database from "better-sqlite3";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { searchMemories, _resetForTests } from "../src/lib/brain_db.ts";

const dir = mkdtempSync(join(tmpdir(), "dotty-memgate-"));
const dbPath = join(dir, "brain.db");

const db = new Database(dbPath);
db.exec(`
  CREATE TABLE memories (
    id INTEGER PRIMARY KEY, key TEXT, content TEXT, category TEXT,
    embedding BLOB, created_at TEXT, updated_at TEXT, session_id TEXT,
    namespace TEXT, importance INTEGER, superseded_by TEXT
  );
  CREATE VIRTUAL TABLE memories_fts
    USING fts5(key, content, content='memories', content_rowid='id');
`);
const ins = db.prepare(
  "INSERT INTO memories (key, content, category, namespace, created_at) VALUES (?,?,?,?,?)",
);
ins.run("k1", "alice likes peanuts", "fact", "person:alice", "2026-01-01");
ins.run("k2", "kiddo is allergic to peanuts", "fact", "person_pending:bob", "2026-01-01");
db.exec("INSERT INTO memories_fts(rowid, key, content) SELECT id, key, content FROM memories;");
db.close();

let failures = 0;
function check(label: string, cond: boolean): void {
  if (cond) {
    process.stdout.write(`ok - ${label}\n`);
  } else {
    process.stderr.write(`FAIL - ${label}\n`);
    failures++;
  }
}

try {
  _resetForTests();
  const rows = searchMemories("peanuts", { dbPath, limit: 5 });
  const namespaces = rows.map((r) => r.namespace);

  check("approved person: row is returned", namespaces.includes("person:alice"));
  check(
    "pending minor fact is excluded",
    !namespaces.some((n) => n.startsWith("person_pending:")),
  );
  check("only the approved row remains", rows.length === 1);
} finally {
  _resetForTests();
  rmSync(dir, { recursive: true, force: true });
}

if (failures > 0) process.exit(1);
process.stdout.write("memory_gate.test.ts passed\n");
