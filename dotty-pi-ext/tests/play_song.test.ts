// play_song tests:
//   1. Matcher equivalence vs Python oracle (exact, substring, no-match).
//   2. Wrapper behaviour (empty input, missing host, empty catalogue,
//      dispatch success/failure) with mocked fetch.
//   3. Optional live smoke gated by DOTTY_XIAOZHI_HOST.

import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  _resetCacheForTests,
  matchSong,
  runPlaySong,
} from "../src/tools/play_song.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ORACLE = join(__dirname, "play_song_oracle.py");

let failures = 0;

function assertEq(label: string, actual: unknown, expected: unknown): void {
  const a = typeof actual === "string" || actual === null
    ? String(actual)
    : JSON.stringify(actual);
  const e = typeof expected === "string" || expected === null
    ? String(expected)
    : JSON.stringify(expected);
  if (a === e) {
    process.stdout.write(`  PASS  ${label}\n`);
    return;
  }
  process.stderr.write(
    `  FAIL  ${label}\n        expected: ${e.slice(0, 240)}\n        actual:   ${a.slice(0, 240)}\n`,
  );
  failures++;
}

function callOracle(query: string, files: string[]): string | null {
  const out = execFileSync("python3", [ORACLE, query, ...files], {
    encoding: "utf8",
  });
  return (JSON.parse(out.trim()) as { match: string | null }).match;
}

// --- 1. Matcher equivalence ---------------------------------------------

const CATALOGUE = [
  "twinkle.opus",
  "twinkle-twinkle-little-star.opus",
  "happy_birthday.opus",
  "wheels-on-the-bus.opus",
  "old-macdonald.mp3",
  "row-row-row-your-boat.wav",
];

const MATCHER_QUERIES = [
  "twinkle.opus",                         // exact-stem (extension match)
  "twinkle",                              // exact-stem (no extension)
  "happy birthday",                       // substring (with space — won't match underscore version)
  "happy_birthday",                       // substring (underscore form)
  "WHEELS",                               // case-insensitive substring
  "bus",                                  // file_stem contains query
  "wheels-on-the-bus-extra",              // query contains file_stem
  "old macdonald.mp3",                    // case + space (won't match dash-form)
  "old-macdonald",                        // exact via dash
  "totally-not-in-the-list",              // no match
  "",                                     // empty
  "   ",                                  // whitespace only
];

function testMatcher(): void {
  process.stdout.write("Matcher vs Python oracle:\n");
  for (const q of MATCHER_QUERIES) {
    let expected: string | null;
    try {
      expected = callOracle(q, CATALOGUE);
    } catch {
      // empty / whitespace queries make the oracle CLI argv-empty; the
      // matcher contract is: empty/whitespace → null. Assert directly.
      expected = null;
    }
    const actual = matchSong(q, CATALOGUE);
    assertEq(`matchSong(${JSON.stringify(q)})`, actual, expected);
  }
}

// --- 2. Wrapper behaviour with mocked fetch -----------------------------

interface FetchRoute {
  match: (url: string, init?: RequestInit) => boolean;
  respond: () => { status: number; body: unknown } | Promise<{ status: number; body: unknown }>;
}

function installFetchRoutes(routes: FetchRoute[]): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: any, init: any) => {
    const u = typeof url === "string" ? url : url.toString();
    for (const r of routes) {
      if (r.match(u, init)) {
        const { status, body } = await r.respond();
        return {
          ok: status >= 200 && status < 300,
          status,
          async json() {
            return body;
          },
          async text() {
            return typeof body === "string" ? body : JSON.stringify(body);
          },
        } as Response;
      }
    }
    throw new Error(`unmocked fetch: ${u}`);
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

async function testEmptyName(): Promise<void> {
  process.stdout.write("\nEmpty / whitespace name short-circuits:\n");
  _resetCacheForTests();
  process.env.XIAOZHI_HOST = "10.0.0.99";
  const got = await runPlaySong("   ");
  assertEq("empty-name reply", got, "(no song name given)");
  delete process.env.XIAOZHI_HOST;
}

async function testMissingHost(): Promise<void> {
  process.stdout.write("\nMissing XIAOZHI_HOST short-circuits:\n");
  _resetCacheForTests();
  delete process.env.XIAOZHI_HOST;
  const got = await runPlaySong("twinkle");
  assertEq("no-host reply", got, "(can't reach xiaozhi-server)");
}

async function testEmptyCatalogue(): Promise<void> {
  process.stdout.write("\nEmpty catalogue:\n");
  _resetCacheForTests();
  const restore = installFetchRoutes([
    {
      match: (u) => u.endsWith("/xiaozhi/admin/songs"),
      respond: () => ({ status: 200, body: { files: [] } }),
    },
  ]);
  try {
    const got = await runPlaySong("twinkle", { catalogOverride: [] });
    assertEq("empty-catalogue reply", got, "(song catalogue is empty)");
  } finally {
    restore();
  }
}

async function testNoMatch(): Promise<void> {
  process.stdout.write("\nNo match → suggests sample:\n");
  _resetCacheForTests();
  const got = await runPlaySong("zzzz", {
    catalogOverride: ["a.opus", "b.opus", "c.opus", "d.opus", "e.opus", "f.opus"],
  });
  assertEq(
    "no-match reply",
    got,
    "(no match for 'zzzz'; have: a, b, c, d, e)",
  );
}

async function testDispatchOk(): Promise<void> {
  process.stdout.write("\nDispatch success:\n");
  _resetCacheForTests();
  let captured: { url?: string; body?: unknown } = {};
  const restore = installFetchRoutes([
    {
      match: (u, init) =>
        u.endsWith("/xiaozhi/admin/play-asset") && init?.method === "POST",
      respond: async () => ({ status: 200, body: { ok: true } }),
    },
  ]);
  globalThis.fetch = (async (url: any, init: any) => {
    captured = {
      url: String(url),
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    };
    return {
      ok: true,
      status: 200,
      async json() { return { ok: true }; },
      async text() { return "ok"; },
    } as Response;
  }) as typeof fetch;
  try {
    const got = await runPlaySong("twinkle", {
      catalogOverride: ["twinkle.opus"],
      assetBaseOverride: "/opt/songs",
    });
    assertEq("dispatch-success reply", got, "playing twinkle");
    assertEq("posted url", captured.url, "http://localhost:8003/xiaozhi/admin/play-asset");
    assertEq(
      "posted body",
      captured.body,
      { asset: "/opt/songs/twinkle.opus", after_turn: true },
    );
  } finally {
    restore();
  }
}

async function testDispatchFailure(): Promise<void> {
  process.stdout.write("\nDispatch failure surfaces HTTP status:\n");
  _resetCacheForTests();
  const restore = installFetchRoutes([
    {
      match: (u) => u.endsWith("/xiaozhi/admin/play-asset"),
      respond: () => ({ status: 500, body: "kaboom" }),
    },
  ]);
  try {
    const got = await runPlaySong("twinkle", {
      catalogOverride: ["twinkle.opus"],
      assetBaseOverride: "/opt/songs",
    });
    if (got.startsWith("(couldn't play twinkle.opus: HTTP 500")) {
      process.stdout.write(`  PASS  http-500 reply (${JSON.stringify(got)})\n`);
    } else {
      process.stderr.write(`  FAIL  http-500 reply: ${JSON.stringify(got)}\n`);
      failures++;
    }
  } finally {
    restore();
  }
}

// --- 3. Optional live smoke --------------------------------------------

async function testLiveSmoke(): Promise<void> {
  const host = process.env.DOTTY_XIAOZHI_HOST;
  if (!host) {
    process.stdout.write(
      "\nLive smoke: SKIPPED (set DOTTY_XIAOZHI_HOST=<ip> to fetch real catalogue).\n",
    );
    return;
  }
  process.stdout.write(`\nLive smoke against ${host}:8003 — fetch catalogue only:\n`);
  _resetCacheForTests();
  process.env.XIAOZHI_HOST = host;
  const { fetchSongCatalog } = await import("../src/lib/xiaozhi_admin.ts");
  const files = await fetchSongCatalog({ host });
  if (files.length > 0) {
    process.stdout.write(`  PASS  fetched ${files.length} files, e.g. ${JSON.stringify(files[0])}\n`);
  } else {
    process.stderr.write(`  FAIL  empty catalogue from ${host}\n`);
    failures++;
  }
}

async function main(): Promise<void> {
  // Restore default XIAOZHI_HOST handling between tests.
  const originalHost = process.env.XIAOZHI_HOST;
  testMatcher();
  await testEmptyName();
  await testMissingHost();
  // Set host so subsequent tests get past the early-return.
  process.env.XIAOZHI_HOST = "localhost";
  await testEmptyCatalogue();
  await testNoMatch();
  await testDispatchOk();
  await testDispatchFailure();
  if (originalHost === undefined) delete process.env.XIAOZHI_HOST;
  else process.env.XIAOZHI_HOST = originalHost;
  await testLiveSmoke();

  process.stdout.write(`\n${failures === 0 ? "OK" : "FAIL"} — ${failures} failure(s)\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
