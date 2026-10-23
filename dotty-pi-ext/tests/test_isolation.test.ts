// Synthetic fixture isolation. AI-assisted: OpenAI Codex (GPT-6).
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Database from "better-sqlite3";
import { createFixture, isolatedEnvironment } from "./synthetic_fixture.mjs";

const directory = mkdtempSync(join(tmpdir(), "dotty-fixture-contract-"));
const path = join(directory, "synthetic.db");
try {
  createFixture(path);
  assert.throws(() => createFixture(path), /EEXIST/);
  const db = new Database(path);
  try {
    assert.equal(db.prepare("SELECT count(*) AS n FROM memories").get().n, 3);
    for (const query of ["Dotty", "Taiwan", "name"]) {
      assert.ok(db.prepare("SELECT count(*) AS n FROM memories_fts WHERE memories_fts MATCH ?").get(query).n > 0);
    }
    db.prepare("INSERT INTO memories(id,key,content,category,namespace) VALUES (?,?,?,?,?)")
      .run("trigger-test", "trigger-test", "fixtureonlytoken", "core", "voice");
    const count = () => db.prepare("SELECT count(*) AS n FROM memories_fts WHERE memories_fts MATCH 'fixtureonlytoken'").get().n;
    assert.equal(count(), 1);
    db.prepare("UPDATE memories SET content='replacementtoken' WHERE id='trigger-test'").run();
    assert.equal(count(), 0);
    db.prepare("DELETE FROM memories WHERE id='trigger-test'").run();
    assert.equal(db.prepare("SELECT count(*) AS n FROM memories_fts WHERE memories_fts MATCH 'replacementtoken'").get().n, 0);
  } finally {
    db.close();
  }
  const original = { PATH: "/usr/bin", DOTTY_BRAIN_DB: "/household.db",
    DOTTY_BRAIN_DB_SNAPSHOT: "/household-copy.db", DOTTY_LLAMA_SWAP_URL: "http://live",
    DOTTY_XIAOZHI_HOST: "live", XIAOZHI_HOST: "live", DOTTY_ADMIN_TOKEN: "secret" };
  const env = isolatedEnvironment(original, path);
  assert.equal(env.DOTTY_BRAIN_DB, path);
  assert.equal(env.DOTTY_BRAIN_DB_SNAPSHOT, path);
  for (const key of ["DOTTY_LLAMA_SWAP_URL", "DOTTY_XIAOZHI_HOST", "XIAOZHI_HOST", "DOTTY_ADMIN_TOKEN"]) {
    assert.equal(env[key], undefined);
  }
  assert.equal(env.PATH, "/usr/bin");
  assert.equal(original.DOTTY_BRAIN_DB, "/household.db");
  console.log("test_isolation: fixture hits, FTS insert/update/delete, no overwrite, environment isolation pass");
} finally {
  rmSync(directory, { recursive: true, force: true });
}
