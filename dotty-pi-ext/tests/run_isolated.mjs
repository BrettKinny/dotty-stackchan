// Run every voice-tool test against invented data with live smoke disabled.
// AI-assisted: OpenAI Codex (GPT-6).
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createFixture, isolatedEnvironment } from "./synthetic_fixture.mjs";

const tests = dirname(fileURLToPath(import.meta.url));
const directory = mkdtempSync(join(tmpdir(), "dotty-tool-fixture-"));
const snapshot = join(directory, "synthetic.db");
let failures = 0;
try {
  createFixture(snapshot);
  const env = isolatedEnvironment(process.env, snapshot);
  // Automatic discovery includes take_photo and prevents silently omitted suites.
  const files = readdirSync(tests).filter(name => name.endsWith(".test.ts")).sort();
  for (const name of files) {
    console.log(`\n--- ${name} (synthetic fixture; live smoke disabled) ---`);
    const result = spawnSync(process.execPath, ["--experimental-strip-types", join(tests, name)], {
      cwd: dirname(tests), env, stdio: "inherit", timeout: 60000,
    });
    if (result.error || result.status !== 0) {
      failures++;
      console.error(`${name}: ${result.error?.message ?? `exit ${result.status}, signal ${result.signal}`}`);
    }
  }
  console.log(`\nIsolated suites: ${files.length - failures}/${files.length} passed; no household snapshot used.`);
} finally {
  // Exact directory created above, never a caller-supplied path.
  rmSync(directory, { recursive: true, force: true });
}
process.exitCode = failures ? 1 : 0;
