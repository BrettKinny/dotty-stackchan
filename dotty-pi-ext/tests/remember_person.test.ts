// Unit test (#53) for the remember_person tool's pure logic — the
// namespace router and the pre-dispatch edge cases.
//
// The full write path (review-status HTTP → storeMemory) is exercised
// end-to-end on the deployed stack; the kid-safety gate decision itself
// is covered by dotty-behaviour/tests/test_routes_voice.py. Here we only
// pin the bits that don't need a daemon or a brain.db.
//
// Usage:
//   node --experimental-strip-types tests/remember_person.test.ts

import {
  personNamespace,
  runRememberPerson,
} from "../src/tools/remember_person.ts";

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

async function main(): Promise<void> {
  // personNamespace — pure namespace router.
  process.stdout.write("personNamespace:\n");
  assertEq("approved", personNamespace("Brett", false), "person:brett");
  assertEq("pending", personNamespace("Kid", true), "person_pending:kid");
  assertEq(
    "trims + lowercases",
    personNamespace("  Hudson  ", false),
    "person:hudson",
  );

  // runRememberPerson edge cases — these return before any HTTP / db
  // call, so they need neither the daemon nor brain.db.
  process.stdout.write("\nrunRememberPerson edge cases:\n");
  assertEq(
    "empty name",
    await runRememberPerson("", "loves trains"),
    "(no person specified)",
  );
  assertEq(
    "whitespace name",
    await runRememberPerson("   ", "loves trains"),
    "(no person specified)",
  );
  assertEq(
    "empty fact",
    await runRememberPerson("brett", "   "),
    "(empty fact)",
  );

  process.stdout.write(
    `\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`,
  );
  process.exit(failures === 0 ? 0 : 1);
}

main();
