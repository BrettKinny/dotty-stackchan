// admin_auth tests: adminFetch attaches X-Admin-Token from DOTTY_ADMIN_TOKEN
// when set, and preserves caller-supplied headers (content-type). The module
// reads the token at load time, so the env var is set BEFORE a dynamic import
// (static ESM imports are hoisted and would run first).

process.env.DOTTY_ADMIN_TOKEN = "test-token";

const { playAsset } = await import("../src/lib/xiaozhi_admin.ts");

let failures = 0;
function assertEq(label: string, got: unknown, want: unknown): void {
  if (got !== want) {
    console.error(`FAIL ${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    failures++;
  } else {
    console.log(`ok - ${label}`);
  }
}

const original = globalThis.fetch;
let token: string | null = null;
let contentType: string | null = null;
globalThis.fetch = (async (_url: unknown, init: { headers?: HeadersInit }) => {
  const h = new Headers(init?.headers);
  token = h.get("X-Admin-Token");
  contentType = h.get("content-type");
  return new Response(null, { status: 200 });
}) as typeof fetch;

try {
  await playAsset("/opt/xiaozhi-esp32-server/config/assets/songs/x.opus");
  assertEq("X-Admin-Token sent when DOTTY_ADMIN_TOKEN set", token, "test-token");
  assertEq("caller content-type header preserved", contentType, "application/json");
} finally {
  globalThis.fetch = original;
}

if (failures > 0) process.exit(1);
console.log("admin_auth.test.ts passed");
