// Regression: a recall query with one misheard word must still find the
// memory (robot, 2026-10-20: "the test work for tonight" missed "the test word
// for tonight is pineapple lantern"), while one shared word alone must not
// surface an unrelated memory, and pending person facts stay hidden (#53).
// AI-assisted (Claude Opus 5.5).
//
// Hermetic — builds its own throwaway brain.db.

import Database from "better-sqlite3";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { searchMemories, _resetForTests } from "../src/lib/brain_db.ts";

const dir = mkdtempSync(join(tmpdir(), "dotty-memfallback-"));
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
ins.run("k1", "the test word for tonight is pineapple lantern", "core", "voice", "2026-01-01");
ins.run("k2", "we watched a test cricket match", "core", "voice", "2026-01-01");
ins.run("k3", "kiddo has a spelling test word list tonight", "fact", "person_pending:bob", "2026-01-01");
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
  const exact = searchMemories("the test word for tonight", { dbPath });
  check("exact phrase still matches", exact.length === 1 && exact[0].key === "k1");

  const misheard = searchMemories("the test work for tonight", { dbPath });
  check("one misheard word still finds the memory", misheard.length === 1 && misheard[0].key === "k1");

  check("one shared word alone surfaces nothing",
    searchMemories("test results", { dbPath }).length === 0);
  check("a single-keyword miss gets no fallback",
    searchMemories("nonexistent_zzz_token_qqq", { dbPath }).length === 0);
  check("fallback never returns a pending person fact",
    searchMemories("spelling test word tonight please", { dbPath })
      .every((r) => !r.namespace.startsWith("person_pending:")));
} finally {
  _resetForTests();
  rmSync(dir, { recursive: true, force: true });
}

if (failures > 0) process.exit(1);
process.stdout.write("memory_fallback.test.ts passed\n");
