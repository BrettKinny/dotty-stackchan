// think_hard equivalence + behaviour tests.
//
// Split into three groups:
//   1. Request-body shape vs Python oracle (deterministic).
//   2. Wrapper behaviour with mocked fetch (success / timeout / error).
//   3. Optional live smoke test against llama-swap, gated by
//      DOTTY_LLAMA_SWAP_URL.

import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildThinkRequest, runThinkHard } from "../src/tools/think_hard.ts";
import { DEFAULT_TIMEOUT_SEC, TimeoutError } from "../src/lib/llama_swap.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ORACLE = join(__dirname, "think_hard_oracle.py");

let failures = 0;

function assertEq(label: string, actual: unknown, expected: unknown): void {
  const a = typeof actual === "string" ? actual : JSON.stringify(actual);
  const e = typeof expected === "string" ? expected : JSON.stringify(expected);
  if (a === e) {
    process.stdout.write(`  PASS  ${label}\n`);
    return;
  }
  process.stderr.write(
    `  FAIL  ${label}\n        expected: ${e.slice(0, 240)}\n        actual:   ${a.slice(0, 240)}\n`,
  );
  failures++;
}

function callOracle(question: string): unknown {
  const out = execFileSync("python3", [ORACLE, question], { encoding: "utf8" });
  return JSON.parse(out.trim());
}

// --- 1. Request-body shape -----------------------------------------------

function testRequestBodies(): void {
  process.stdout.write("Request body vs Python oracle:\n");
  const questions = [
    "What is 2+2?",
    "Capital of Australia.",
    "Tell me about the territorial dispute over Taiwan.",
    "", // even empty produces a valid body; the wrapper short-circuits before the call
  ];
  for (const q of questions) {
    const expected = callOracle(q);
    const actual = buildThinkRequest(q);
    assertEq(`buildThinkRequest(${JSON.stringify(q)})`, actual, expected);
  }
}

// --- 2. Wrapper behaviour with mocked fetch ------------------------------

interface FetchMock {
  status?: number;
  json?: unknown;
  throws?: Error;
}

function installFetchMock(mock: FetchMock): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = (async (_url: any, _init: any) => {
    if (mock.throws) throw mock.throws;
    return {
      ok: (mock.status ?? 200) >= 200 && (mock.status ?? 200) < 300,
      status: mock.status ?? 200,
      async json() {
        return mock.json;
      },
    } as Response;
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

async function testEmptyInput(): Promise<void> {
  process.stdout.write("\nEmpty / whitespace input short-circuits:\n");
  for (const q of ["", "   ", "\n\t"]) {
    const got = await runThinkHard(q);
    assertEq(`runThinkHard(${JSON.stringify(q)})`, got, "(empty question)");
  }
}

async function testSuccess(): Promise<void> {
  process.stdout.write("\nSuccess path:\n");
  const restore = installFetchMock({
    json: { choices: [{ message: { content: "  Pong.  " } }] },
  });
  try {
    const got = await runThinkHard("What is the answer?");
    assertEq("trims whitespace", got, "Pong.");
  } finally {
    restore();
  }
}

async function testLongResponseCap(): Promise<void> {
  process.stdout.write("\n500-char output cap:\n");
  const restore = installFetchMock({
    json: { choices: [{ message: { content: "a".repeat(600) } }] },
  });
  try {
    const got = await runThinkHard("Q?");
    assertEq("length", got.length, 500);
    assertEq("contents", got, "a".repeat(500));
  } finally {
    restore();
  }
}

async function testTimeout(): Promise<void> {
  process.stdout.write("\nTimeout fallback:\n");
  const restore = installFetchMock({ throws: new TimeoutError("test") });
  try {
    const got = await runThinkHard("Q?");
    assertEq(
      "timeout reply",
      got,
      "(I'm slow today, try again in a moment)",
    );
  } finally {
    restore();
  }
}

async function testGenericError(): Promise<void> {
  process.stdout.write("\nGeneric error fallback:\n");
  const restore = installFetchMock({ throws: new Error("ECONNREFUSED") });
  try {
    const got = await runThinkHard("Q?");
    assertEq("generic-error reply", got, "(thinking failed)");
  } finally {
    restore();
  }
}

async function testHttpError(): Promise<void> {
  process.stdout.write("\nNon-2xx HTTP response → generic-error fallback:\n");
  const restore = installFetchMock({ status: 503, json: { error: "busy" } });
  try {
    const got = await runThinkHard("Q?");
    assertEq("http-503 reply", got, "(thinking failed)");
  } finally {
    restore();
  }
}

// --- 3. Optional live smoke test ----------------------------------------

async function testLiveSmoke(): Promise<void> {
  const url = process.env.DOTTY_LLAMA_SWAP_URL;
  if (!url) {
    process.stdout.write(
      "\nLive smoke: SKIPPED (set DOTTY_LLAMA_SWAP_URL=http://192.168.1.67:8080/v1/chat/completions to run).\n",
    );
    return;
  }
  process.stdout.write(`\nLive smoke against ${url}:\n`);
  const got = await runThinkHard("Reply with exactly the word: pong", {
    url,
    timeoutSec: 60,
  });
  const ok = got.length > 0 && got.length <= 500 && !got.startsWith("(");
  if (ok) {
    process.stdout.write(`  PASS  got non-empty bounded reply: ${JSON.stringify(got.slice(0, 120))}\n`);
  } else {
    process.stderr.write(`  FAIL  unexpected reply: ${JSON.stringify(got)}\n`);
    failures++;
  }
}

async function main(): Promise<void> {
  testRequestBodies();
  await testEmptyInput();
  if (!process.env.VOICE_THINKER_TIMEOUT) {
    // Must outlast a cold reasoner load (30-50 s) plus generation.
    assertEq("default timeout outlasts a cold load", String(DEFAULT_TIMEOUT_SEC >= 75), "true");
  }
  await testSuccess();
  await testLongResponseCap();
  await testTimeout();
  await testGenericError();
  await testHttpError();
  await testLiveSmoke();

  process.stdout.write(`\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
