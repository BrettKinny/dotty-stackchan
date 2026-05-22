// FTS5 client for Dotty's `brain.db`. Mirrors the contract of
// bridge.py:_voice_memory_search_blocking — read-only access, phrase
// match wrapping, top-N by rank.
//
// brain.db schema (frozen — managed by ZeroClaw, do NOT mutate from here):
//   memories(id, key, content, category, embedding, created_at,
//            updated_at, session_id, namespace, importance, superseded_by)
//   memories_fts(key, content)  -- virtual FTS5, content=memories
//
// The bind-mount inside the dotty-pi container puts brain.db at
//   /root/.pi/memory/brain.db
// (env-overridable via DOTTY_BRAIN_DB).

import { randomUUID } from "node:crypto";
import Database from "better-sqlite3";

export interface MemoryRow {
  key: string;
  content: string;
  category: string;
  namespace: string;
  created_at: string;
}

const DEFAULT_PATH = process.env.DOTTY_BRAIN_DB ?? "/root/.pi/memory/brain.db";

let cachedDb: Database.Database | null = null;
let cachedPath: string | null = null;
let cachedWriteDb: Database.Database | null = null;
let cachedWritePath: string | null = null;

function openReadOnly(path: string): Database.Database {
  // Reuse the handle across calls — SQLite read-only opens are cheap but
  // not free, and the dotty-pi process is long-lived per #36 Step-5.
  if (cachedDb && cachedPath === path) return cachedDb;
  if (cachedDb) cachedDb.close();
  cachedDb = new Database(path, { readonly: true, fileMustExist: true });
  cachedPath = path;
  return cachedDb;
}

function openWritable(path: string): Database.Database {
  // Separate handle from the readonly one. SQLite (WAL mode) supports
  // concurrent readers + a single writer; both bridge.py and ZeroClaw
  // already write to this file, so contention is expected. Match
  // bridge.py's 5 s timeout to keep behaviour parity under contention.
  if (cachedWriteDb && cachedWritePath === path) return cachedWriteDb;
  if (cachedWriteDb) cachedWriteDb.close();
  cachedWriteDb = new Database(path, { fileMustExist: true, timeout: 5000 });
  cachedWritePath = path;
  return cachedWriteDb;
}

export interface SearchOptions {
  /** Override brain.db path (defaults to DOTTY_BRAIN_DB env / canonical). */
  dbPath?: string;
  /** Cap rows returned by the FTS query (formatter trims further). */
  limit?: number;
}

/**
 * FTS5 phrase search. Returns empty array on missing db, empty query,
 * or any SQLite error (logged to stderr). Never throws into the caller —
 * the voice path must degrade gracefully.
 */
export function searchMemories(
  query: string,
  opts: SearchOptions = {},
): MemoryRow[] {
  const limit = opts.limit ?? 5;
  const path = opts.dbPath ?? DEFAULT_PATH;
  const safe = (query ?? "").replace(/"/g, '""').trim();
  if (!safe) return [];
  // Wrap in double quotes for FTS5 phrase match — same as bridge.py.
  // Plain MATCH on multi-word queries would treat tokens as AND;
  // phrase-quoting keeps the user's word order, matching the existing
  // tool's behaviour the LLM was tuned against.
  const fts = `"${safe}"`;

  try {
    const db = openReadOnly(path);
    const stmt = db.prepare(`
      SELECT m.key, m.content, m.category, m.namespace, m.created_at
      FROM memories_fts
      JOIN memories m ON m.rowid = memories_fts.rowid
      WHERE memories_fts MATCH ?
      ORDER BY rank
      LIMIT ?
    `);
    return stmt.all(fts, limit) as MemoryRow[];
  } catch (err) {
    process.stderr.write(
      `[brain_db] search failed for query=${JSON.stringify(safe.slice(0, 60))}: ${err}\n`,
    );
    return [];
  }
}

/** Mirrors bridge.py `_PERSON_MEMORY_MAX_FACTS` — per-person fact budget. */
export const PERSON_MEMORY_MAX_FACTS = 8;

export interface PersonMemoryRow {
  key: string;
  content: string;
  category: string;
  importance: number;
  created_at: string;
  updated_at: string;
}

export interface PersonFetchOptions {
  /** Override brain.db path (defaults to DOTTY_BRAIN_DB env / canonical). */
  dbPath?: string;
  /** Cap rows returned (defaults to PERSON_MEMORY_MAX_FACTS). */
  limit?: number;
}

/**
 * Direct per-person memory fetch — mirrors
 * bridge.py:_voice_memory_person_fetch_blocking (#53). A namespace-scoped
 * SELECT against `namespace='person:<id>'`, NOT an FTS search, ordered by
 * importance then recency.
 *
 * Only the approved `person:<id>` namespace is read — the kid-safety
 * pending namespace (`person_pending:<id>`) is deliberately never
 * returned, so unreviewed facts about minors cannot reach a turn. Empty
 * array on missing db, empty id, or any sqlite error.
 */
export function fetchPersonMemories(
  personId: string,
  opts: PersonFetchOptions = {},
): PersonMemoryRow[] {
  const limit = opts.limit ?? PERSON_MEMORY_MAX_FACTS;
  const path = opts.dbPath ?? DEFAULT_PATH;
  const pid = (personId ?? "").trim().toLowerCase();
  if (!pid) return [];

  try {
    const db = openReadOnly(path);
    const stmt = db.prepare(`
      SELECT key, content, category, importance, created_at, updated_at
      FROM memories
      WHERE namespace = ?
      ORDER BY importance DESC, updated_at DESC
      LIMIT ?
    `);
    return stmt.all(`person:${pid}`, limit) as PersonMemoryRow[];
  } catch (err) {
    process.stderr.write(
      `[brain_db] person fetch failed for person_id=${JSON.stringify(pid.slice(0, 60))}: ${err}\n`,
    );
    return [];
  }
}

export interface StoreOptions {
  content: string;
  /** Defaults to "core" (long-retention fact, mirrors bridge.py /remember). */
  category?: string;
  /** Always "voice" today; parameterised for symmetry with bridge.py. */
  namespace?: string;
  /** 0.0–1.0; bridge.py uses 0.3 for conversation, 0.7 for fact. */
  importance?: number;
  sessionId?: string | null;
  dbPath?: string;
  /** Test seam — overrides the ISO timestamp + UUID for deterministic asserts. */
  _now?: string;
  _id?: string;
}

/**
 * Mirrors bridge.py:_voice_memory_store_blocking. Returns true on insert,
 * false on empty content / missing db / sqlite error. Never throws — the
 * voice path must degrade gracefully when memory is unavailable.
 *
 * Schema (frozen — managed by ZeroClaw):
 *   memories(id, key, content, category, namespace, importance,
 *            created_at, updated_at, session_id, ...)
 * FTS5 triggers maintain the index on insert.
 */
export function storeMemory(opts: StoreOptions): boolean {
  const trimmed = (opts.content ?? "").trim();
  if (!trimmed) return false;
  const category = opts.category ?? "core";
  const namespace = opts.namespace ?? "voice";
  const importance = opts.importance ?? 0.7;
  const sessionId = opts.sessionId ?? null;
  const path = opts.dbPath ?? DEFAULT_PATH;
  // bridge.py: datetime.now(ZoneInfo("UTC")).isoformat() — yields
  // `2026-05-18T00:43:00.123456+00:00`. JS's toISOString() yields
  // `2026-05-18T00:43:00.123Z` (Z suffix, ms precision). Both are valid
  // ISO 8601 and SQLite stores them as TEXT; the schema doesn't compare
  // them. Match JS-native format here rather than fake microsecond
  // precision — the column is opaque text downstream.
  const now = opts._now ?? new Date().toISOString();
  const id = opts._id ?? randomUUID();
  // bridge.py key format: f"voice_{category}_{now}_{mem_id[:8]}"
  const key = `voice_${category}_${now}_${id.slice(0, 8)}`;

  try {
    const db = openWritable(path);
    db.prepare(`
      INSERT INTO memories
        (id, key, content, category, namespace,
         importance, created_at, updated_at, session_id)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(id, key, trimmed, category, namespace, importance, now, now, sessionId);
    return true;
  } catch (err) {
    process.stderr.write(
      `[brain_db] store failed (category=${category}): ${err}\n`,
    );
    return false;
  }
}

/** Test-only helper: close the cached handles. */
export function _resetForTests(): void {
  if (cachedDb) cachedDb.close();
  cachedDb = null;
  cachedPath = null;
  if (cachedWriteDb) cachedWriteDb.close();
  cachedWriteDb = null;
  cachedWritePath = null;
}
