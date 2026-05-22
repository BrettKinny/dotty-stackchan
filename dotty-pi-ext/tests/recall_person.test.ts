// Equivalence test (#53): seed known person:<id> rows, then assert the
// TS fetchPersonMemories() returns rows byte-identical to the bridge's
// _voice_memory_person_fetch_blocking SELECT (via recall_person_oracle.py).
//
// Usage:
//   DOTTY_BRAIN_DB_SNAPSHOT=/path/to/brain.db \
//   node --experimental-strip-types tests/recall_person.test.ts
//
// The pure-formatter edge cases run unconditionally; the row-equality
// cases need a brain.db snapshot to seed against and SKIP without one.

import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  fetchPersonMemories,
  storeMemory,
  _resetForTests,
  type PersonMemoryRow,
} from "../src/lib/brain_db.ts";
import {
  formatPersonRecall,
  runRecallPerson,
} from "../src/tools/recall_person.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ORACLE = join(__dirname, "recall_person_oracle.py");

let failures = 0;

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

function callOracle(db: string, personId: string): { rows: PersonMemoryRow[] } {
  const out = execFileSync("python3", [ORACLE, db, personId], {
    encoding: "utf8",
  });
  return JSON.parse(out.trim()) as { rows: PersonMemoryRow[] };
}

function main(): void {
  // ----- Pure formatter / dispatch edge cases (no db needed) -----
  process.stdout.write("Edge cases (pure return value):\n");
  assertEq("empty name", runRecallPerson(""), "(no person specified)");
  assertEq("whitespace name", runRecallPerson("   "), "(no person specified)");
  assertEq(
    "no facts → miss",
    formatPersonRecall("ghost", []),
    "(nothing remembered about ghost)",
  );
  assertEq(
    "blank-content rows → miss",
    formatPersonRecall("ghost", [
      { key: "k", content: "   ", category: "core", importance: 0.5, created_at: "", updated_at: "" },
    ]),
    "(nothing remembered about ghost)",
  );
  assertEq(
    "facts pipe-joined",
    formatPersonRecall("kid", [
      { key: "k1", content: "loves dinosaurs", category: "core", importance: 0.7, created_at: "", updated_at: "" },
      { key: "k2", content: "afraid of the dark", category: "core", importance: 0.6, created_at: "", updated_at: "" },
    ]),
    "loves dinosaurs | afraid of the dark",
  );

  const snapshot = process.env.DOTTY_BRAIN_DB_SNAPSHOT;
  if (!snapshot || !existsSync(snapshot)) {
    process.stderr.write(
      "SKIP: set DOTTY_BRAIN_DB_SNAPSHOT to a readable brain.db copy " +
        "for the row-equality cases.\n",
    );
    process.stdout.write(
      `\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`,
    );
    process.exit(failures === 0 ? 0 : 1);
  }

  // ----- Row-equality: TS SELECT vs bridge SELECT on identical data -----
  process.stdout.write(`\nSnapshot: ${snapshot}\n`);
  const tmp = mkdtempSync(join(tmpdir(), "dotty-recall-person-"));
  const db = join(tmp, "seeded.db");
  copyFileSync(snapshot, db);
  try {
    // Seed three facts under person:testkid with distinct importance so
    // the importance-DESC ordering is observable, plus one unrelated
    // namespace=voice row that must NOT leak into the result.
    _resetForTests();
    const seed: Array<[string, number, string]> = [
      ["testkid likes drawing", 0.5, "2026-05-01T00:00:00.000Z"],
      ["testkid has a dog named Rex", 0.9, "2026-05-02T00:00:00.000Z"],
      ["testkid is learning to read", 0.7, "2026-05-03T00:00:00.000Z"],
    ];
    seed.forEach(([content, importance, now], i) => {
      storeMemory({
        content,
        category: "core",
        namespace: "person:testkid",
        importance,
        sessionId: null,
        dbPath: db,
        _now: now,
        _id: `aaaaaaaa-0000-4000-8000-00000000000${i}`,
      });
    });
    storeMemory({
      content: "unrelated voice memory",
      category: "core",
      namespace: "voice",
      importance: 0.99,
      sessionId: null,
      dbPath: db,
      _now: "2026-05-04T00:00:00.000Z",
      _id: "bbbbbbbb-0000-4000-8000-000000000000",
    });
    _resetForTests();

    const tsRows = fetchPersonMemories("testkid", { dbPath: db });
    const oracle = callOracle(db, "testkid");
    _resetForTests();

    assertEq("row equality (TS vs bridge SELECT)", tsRows, oracle.rows);
    assertEq(
      "importance-DESC ordering",
      tsRows.map((r) => r.content),
      [
        "testkid has a dog named Rex",
        "testkid is learning to read",
        "testkid likes drawing",
      ],
    );
    assertEq("namespace isolation (voice row excluded)", tsRows.length, 3);
    assertEq(
      "case-insensitive id match",
      fetchPersonMemories("TestKid", { dbPath: db }).length,
      3,
    );
    assertEq(
      "unknown person → empty",
      fetchPersonMemories("nobody-here", { dbPath: db }),
      [],
    );
    _resetForTests();
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }

  process.stdout.write(
    `\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`,
  );
  process.exit(failures === 0 ? 0 : 1);
}

main();
