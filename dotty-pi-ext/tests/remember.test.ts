// Equivalence test: run a handful of facts through the TS storeMemory()
// and assert the inserted row is byte-identical to what bridge.py would
// have written via /api/voice/remember.
//
// Usage:
//   DOTTY_BRAIN_DB_SNAPSHOT=/path/to/brain.db \
//   node --experimental-strip-types tests/remember.test.ts
//
// Each test case gets its own tmp copy of the snapshot (one for the TS
// port, one for the Python oracle) so neither sees the other's writes.
// Both write with the same deterministic (now, id) seed; the row dumps
// must match field-by-field.

import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import Database from "better-sqlite3";

import { storeMemory, _resetForTests } from "../src/lib/brain_db.ts";
import { runRemember } from "../src/tools/remember.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ORACLE = join(__dirname, "remember_oracle.py");

interface Case {
  fact: string;
  /** Optional override for category (defaults to "core" via the /remember handler). */
  category?: string;
  importance?: number;
  /** Test label — also feeds into the synthetic UUID + ISO timestamp. */
  label: string;
}

const CASES: Case[] = [
  { label: "short_fact", fact: "Brett's favourite colour is blue." },
  {
    label: "trim_leading_trailing_whitespace",
    fact: "   wrapped in spaces   ",
  },
  {
    label: "exactly_300_chars",
    fact: "a".repeat(300),
  },
  {
    label: "over_300_truncated",
    fact: "x".repeat(305),
  },
  {
    label: "emoji_codepoint_boundary",
    fact: "😊".repeat(150) + "TAIL", // 150 emoji codepoints + 4 chars = 154 < 300 (no trunc)
  },
];

const EDGE_RUNREMEMBER: Array<{ label: string; fact: string; expected: string }> = [
  { label: "empty_fact", fact: "", expected: "(empty fact)" },
  { label: "whitespace_only", fact: "   \t  ", expected: "(empty fact)" },
];

interface OracleResult {
  ok: boolean;
  row?: {
    id: string;
    key: string;
    content: string;
    category: string;
    namespace: string;
    importance: number;
    created_at: string;
    updated_at: string;
    session_id: string | null;
  };
}

function callOracle(
  db: string,
  now: string,
  id: string,
  fact: string,
  opts: { category?: string; importance?: number } = {},
): OracleResult {
  const flags: string[] = [];
  if (opts.category) flags.push(`--category=${opts.category}`);
  if (opts.importance !== undefined) flags.push(`--importance=${opts.importance}`);
  const out = execFileSync(
    "python3",
    [ORACLE, db, now, id, fact, ...flags],
    { encoding: "utf8" },
  );
  return JSON.parse(out.trim()) as OracleResult;
}

function readBack(dbPath: string, id: string): OracleResult["row"] | null {
  const db = new Database(dbPath, { readonly: true, fileMustExist: true });
  try {
    const row = db.prepare(`
      SELECT id, key, content, category, namespace,
             importance, created_at, updated_at, session_id
      FROM memories WHERE id = ?
    `).get(id) as OracleResult["row"] | undefined;
    return row ?? null;
  } finally {
    db.close();
  }
}

function assertEq(label: string, actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    process.stdout.write(`  PASS  ${label}\n`);
    return;
  }
  process.stderr.write(
    `  FAIL  ${label}\n        expected: ${e}\n        actual:   ${a}\n`,
  );
  failures++;
}

let failures = 0;

function makeId(label: string): string {
  // Deterministic UUID-shaped string keyed off the label so each case has
  // a stable id across oracle + TS runs without colliding with other
  // cases or with the snapshot's existing UUIDs.
  // Format: 12345678-aaaa-4bbb-9ccc-<label-hash-12>
  const hash = Buffer.from(label).toString("hex").padEnd(12, "0").slice(0, 12);
  return `12345678-aaaa-4bbb-9ccc-${hash}`;
}

function makeNow(label: string): string {
  // Use a fixed past timestamp + the label hash so the value is stable
  // across runs but distinct per case (so it shows up in failure output).
  return `2026-05-18T00:00:00.${label.length.toString().padStart(3, "0")}Z`;
}

function main(): void {
  const snapshot = process.env.DOTTY_BRAIN_DB_SNAPSHOT;
  if (!snapshot || !existsSync(snapshot)) {
    process.stderr.write(
      `SKIP: set DOTTY_BRAIN_DB_SNAPSHOT to a readable brain.db copy.\n` +
        `      (default location for dev: ~/Repos/dotty-private/probes/runs/brain.db.snapshot-*)\n`,
    );
    process.exit(0);
  }

  // Edge cases first — pure return-value test, no db touch needed.
  process.stdout.write("Edge cases (pure return value):\n");
  for (const ec of EDGE_RUNREMEMBER) {
    const actual = runRemember(ec.fact);
    assertEq(`label=${ec.label}`, actual, ec.expected);
  }

  process.stdout.write(`\nSnapshot: ${snapshot}\n`);

  for (const c of CASES) {
    process.stdout.write(`\nCase: ${c.label}\n`);
    const tmp = mkdtempSync(join(tmpdir(), `dotty-remember-${c.label}-`));
    const tsDb = join(tmp, "ts.db");
    const oracleDb = join(tmp, "oracle.db");
    copyFileSync(snapshot, tsDb);
    copyFileSync(snapshot, oracleDb);
    try {
      const id = makeId(c.label);
      const now = makeNow(c.label);

      // TS port write (with deterministic seam). Use codepoint-aware
      // truncation (Array.from) to match Python's str[:N] semantics —
      // JS .slice() counts UTF-16 code units and splits surrogate pairs.
      const trimmed = c.fact.trim();
      const cp = Array.from(trimmed);
      const capped = cp.length > 300 ? cp.slice(0, 300).join("") : trimmed;
      _resetForTests(); // ensure no cached handle clings to a prior tmp path
      const ok = storeMemory({
        content: capped,
        category: c.category ?? "core",
        namespace: "voice",
        importance: c.importance ?? 0.7,
        sessionId: null,
        dbPath: tsDb,
        _now: now,
        _id: id,
      });
      _resetForTests();
      assertEq(`${c.label} storeMemory returned`, ok, true);

      const tsRow = readBack(tsDb, id);

      // Oracle write (same seed, parallel db).
      const oracle = callOracle(oracleDb, now, id, c.fact, {
        category: c.category,
        importance: c.importance,
      });
      assertEq(`${c.label} oracle.ok`, oracle.ok, true);

      assertEq(`${c.label} row equality`, tsRow, oracle.row ?? null);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  }

  process.stdout.write(`\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
