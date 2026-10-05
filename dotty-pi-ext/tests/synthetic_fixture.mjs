// Invented test data only. AI-assisted: OpenAI Codex (GPT-6).
// Compatible with the memory tools' UUID-id/SQLite-rowid contract; this is
// a test fixture, not a production database migration or household snapshot.
import { closeSync, openSync } from "node:fs";
import Database from "better-sqlite3";

export function createFixture(path) {
  // Refuse even an empty existing file. Never overwrite a caller's database.
  closeSync(openSync(path, "wx", 0o600));
  const db = new Database(path);
  try {
    db.exec(`
      CREATE TABLE memories (
        id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
        category TEXT NOT NULL, embedding BLOB, created_at TEXT,
        updated_at TEXT, session_id TEXT, namespace TEXT NOT NULL,
        importance REAL DEFAULT 0.5, superseded_by TEXT
      );
      CREATE VIRTUAL TABLE memories_fts USING fts5(
        key, content, content='memories', content_rowid='rowid'
      );
      CREATE TRIGGER memories_insert AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid,key,content) VALUES (new.rowid,new.key,new.content);
      END;
      CREATE TRIGGER memories_delete AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts,rowid,key,content)
          VALUES ('delete',old.rowid,old.key,old.content);
      END;
      CREATE TRIGGER memories_update AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts,rowid,key,content)
          VALUES ('delete',old.rowid,old.key,old.content);
        INSERT INTO memories_fts(rowid,key,content) VALUES (new.rowid,new.key,new.content);
      END;
    `);
    const insert = db.prepare(`
      INSERT INTO memories(id,key,content,category,namespace,importance,created_at,updated_at)
      VALUES (?,?,?,'core','voice',0.5,'2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z')
    `);
    [
      "Dotty is a fictional robot in this isolated test fixture.",
      "Taiwan is a search keyword in this invented test record.",
      "The fictional robot name in this fixture is Dotty.",
    ].forEach((content, i) => insert.run(
      `00000000-0000-4000-8000-00000000000${i + 1}`, `synthetic-${i + 1}`, content,
    ));
  } finally {
    db.close();
  }
}

export function isolatedEnvironment(original, path) {
  const env = { ...original };
  for (const key of Object.keys(env)) {
    if (key.startsWith("DOTTY_") || key.startsWith("XIAOZHI_")) delete env[key];
  }
  env.DOTTY_BRAIN_DB = path;
  env.DOTTY_BRAIN_DB_SNAPSHOT = path;
  return env;
}
