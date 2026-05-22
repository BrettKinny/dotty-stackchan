// dotty-behaviour client — sibling to xiaozhi_admin.ts. Talks to the
// Unraid-resident behaviour daemon (the bridge.py successor; see
// ../../dotty-behaviour/README.md).
//
// Default URL is localhost:8090 because both dotty-pi and
// dotty-behaviour run on the Unraid host networked together. Override
// via DOTTY_BEHAVIOUR_URL for dev/test.

const DEFAULT_URL =
  process.env.DOTTY_BEHAVIOUR_URL ?? "http://127.0.0.1:8090";
const DEFAULT_TIMEOUT_MS = Number(
  process.env.DOTTY_BEHAVIOUR_TIMEOUT_MS ?? "3000",
);

export interface BehaviourOptions {
  baseUrl?: string;
  timeoutMs?: number;
}

async function behaviourFetch(
  path: string,
  init: RequestInit,
  opts: BehaviourOptions,
): Promise<Response> {
  const base = (opts.baseUrl ?? DEFAULT_URL).replace(/\/+$/, "");
  const url = `${base}${path.startsWith("/") ? path : "/" + path}`;
  const ac = new AbortController();
  const timer = setTimeout(
    () => ac.abort(),
    opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  );
  try {
    return await fetch(url, { ...init, signal: ac.signal });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * GET /api/voice/take_photo — returns the latest cached vision
 * description if ≤30 s old, otherwise a fixed "can't see" string.
 *
 * Returns the daemon's description string verbatim. On any failure
 * (network, non-2xx, malformed JSON) returns the same fallback string
 * dotty-behaviour itself produces, so the LLM's behaviour stays
 * identical to the bridge.py path.
 */
export async function fetchTakePhoto(
  opts: BehaviourOptions = {},
): Promise<string> {
  const fallback = "(I can't see anything fresh right now)";
  try {
    const resp = await behaviourFetch(
      "/api/voice/take_photo",
      { method: "GET" },
      opts,
    );
    if (!resp.ok) return fallback;
    const data = (await resp.json()) as { description?: unknown };
    const desc = data.description;
    if (typeof desc !== "string" || desc.length === 0) return fallback;
    return desc;
  } catch {
    return fallback;
  }
}

/**
 * GET /api/voice/person_review_status — the #53 kid-safety classifier.
 * Returns true when a declared fact about `personId` must be routed to
 * the review queue (a minor, an unknown person, or an unclassifiable
 * registry entry).
 *
 * Fail-safe: any failure (network, non-2xx, malformed JSON) returns
 * `true`. When the gate is unreachable we route to review rather than
 * risk auto-committing an unreviewed fact about a minor.
 */
export async function fetchPersonReviewStatus(
  personId: string,
  opts: BehaviourOptions = {},
): Promise<boolean> {
  try {
    const resp = await behaviourFetch(
      `/api/voice/person_review_status?person_id=${encodeURIComponent(personId)}`,
      { method: "GET" },
      opts,
    );
    if (!resp.ok) return true;
    const data = (await resp.json()) as { needs_review?: unknown };
    // Only an explicit `false` clears the gate — missing/garbage → review.
    return data.needs_review !== false;
  } catch {
    return true;
  }
}
