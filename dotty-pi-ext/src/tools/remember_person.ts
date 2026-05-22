// remember_person voice tool (#53) — per-person memory write with the
// kid-safety review gate.
//
// Unlike `remember` (a fact in the generic `voice` namespace, no gate),
// remember_person attributes a fact to a named household member and must
// respect the #53 kid-safety gate: a fact about a minor is held in a
// `person_pending:<id>` review queue until a human approves it, and is
// never read into a turn meanwhile.
//
// The gate DECISION needs the household registry, so it is NOT duplicated
// here — the tool asks dotty-behaviour's /api/voice/person_review_status
// classifier (single-source, Python), then writes to the approved
// (`person:<id>`) or pending (`person_pending:<id>`) namespace. The
// brain.db write itself stays local via storeMemory(), consistent with
// the `remember` tool.
//
// Contract:
//   - Empty / whitespace name → "(no person specified)"
//   - Empty / whitespace fact → "(empty fact)"
//   - Stored, adult           → "(remembered about <name>)"
//   - Stored, needs review    → "(saved — a grown-up will check that)"
//   - Insert failure          → "(remember failed)"

import { Type } from "typebox";
import { storeMemory } from "../lib/brain_db.ts";
import { fetchPersonReviewStatus } from "../lib/dotty_behaviour.ts";

// bridge.py /api/voice/remember_person truncates `fact` to 300 chars.
const FACT_MAX_CHARS = 300;

/**
 * `person:<id>` when approved, `person_pending:<id>` when held for
 * review. Pure — separated so the test rig can exercise it directly.
 */
export function personNamespace(
  personId: string,
  needsReview: boolean,
): string {
  const pid = (personId ?? "").trim().toLowerCase();
  return `${needsReview ? "person_pending" : "person"}:${pid}`;
}

export interface RememberPersonOptions {
  /** dotty-behaviour base URL override (for the review classifier). */
  baseUrl?: string;
  timeoutMs?: number;
  /** brain.db path override. */
  dbPath?: string;
  sessionId?: string | null;
}

/** Top-level dispatch used by both the pi tool and the test rig. */
export async function runRememberPerson(
  name: string,
  fact: string,
  opts: RememberPersonOptions = {},
): Promise<string> {
  const n = (name ?? "").trim();
  if (!n) return "(no person specified)";
  const trimmedFact = (fact ?? "").trim();
  if (!trimmedFact) return "(empty fact)";
  // Codepoint-aware truncation — matches Python str[:N] (see remember.ts).
  const cp = Array.from(trimmedFact);
  const capped =
    cp.length > FACT_MAX_CHARS
      ? cp.slice(0, FACT_MAX_CHARS).join("")
      : trimmedFact;

  const needsReview = await fetchPersonReviewStatus(n, {
    baseUrl: opts.baseUrl,
    timeoutMs: opts.timeoutMs,
  });
  const ok = storeMemory({
    content: capped,
    category: "core",
    namespace: personNamespace(n, needsReview),
    importance: 0.7,
    sessionId: opts.sessionId ?? null,
    dbPath: opts.dbPath,
  });
  if (!ok) return "(remember failed)";
  return needsReview
    ? "(saved — a grown-up will check that)"
    : `(remembered about ${n})`;
}

/** Pi tool descriptor — passed to `pi.registerTool` from index.ts. */
export const rememberPersonTool = {
  name: "remember_person",
  label: "Remember Person",
  description:
    "Store a durable fact about a specific named household member — a " +
    "preference, relationship, or recent context. Use when the user " +
    "tells you something worth keeping about a particular person. For " +
    "general facts not about one named person, use remember instead.",
  promptSnippet:
    "Persist a fact about a named household member to Dotty's memory.",
  promptGuidelines: [
    "Call remember_person when the user shares a stable fact about a " +
      "specific named person. Keep it short and self-contained (≤300 chars).",
    "Facts about children are automatically held for a grown-up to " +
      "review before Dotty uses them — just store the fact; the gate " +
      "is handled for you.",
  ],
  parameters: Type.Object({
    name: Type.String({
      description: "The person the fact is about (their name).",
    }),
    fact: Type.String({
      description: "The fact to remember about them (≤300 chars).",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { name: string; fact: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = await runRememberPerson(params.name, params.fact);
    return { content: [{ type: "text", text }] };
  },
};
