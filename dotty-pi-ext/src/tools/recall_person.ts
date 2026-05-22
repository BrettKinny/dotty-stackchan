// recall_person voice tool (#53) — per-person memory read.
//
// The pi runtime exposes no turn-prep / system-prompt injection seam
// (index.ts only registers tools and listens to `agent_end`), so unlike
// bridge.py — which injects a [Person memory] block via _voice_preparer
// — the pi-runtime path surfaces per-person memory as a TOOL the agent
// calls. Same brain.db, different retrieval mechanism per runtime.
//
// Reads only the approved `person:<id>` namespace via fetchPersonMemories;
// the kid-safety pending namespace is never read here.
//
// Contract:
//   - Empty / whitespace name → "(no person specified)"
//   - Name given, nothing stored → "(nothing remembered about <name>)"
//   - Otherwise → up to PERSON_MEMORY_MAX_FACTS facts, each truncated to
//     200 chars (197 + "..."), pipe-joined with " | " (matches the
//     memory_lookup tool's result shape).

import { Type } from "typebox";
import {
  fetchPersonMemories,
  PERSON_MEMORY_MAX_FACTS,
  type PersonMemoryRow,
} from "../lib/brain_db.ts";

const FACT_MAX_CHARS = 200;
const FACT_TRUNC_HEAD = 197; // 200 - len("...")

/**
 * Pure formatter — separated from the tool wrapper so the test rig can
 * exercise it without going through pi's `execute` callback shape.
 */
export function formatPersonRecall(
  name: string,
  rows: PersonMemoryRow[],
): string {
  const facts: string[] = [];
  for (const r of rows) {
    const trimmed = (r.content ?? "").trim();
    if (!trimmed) continue;
    // Slice by Unicode codepoints, not UTF-16 units — matches Python's
    // str[:N] semantics (see memory_lookup.ts for the full rationale).
    const cp = Array.from(trimmed);
    facts.push(
      cp.length > FACT_MAX_CHARS
        ? cp.slice(0, FACT_TRUNC_HEAD).join("") + "..."
        : trimmed,
    );
  }
  if (facts.length === 0) return `(nothing remembered about ${name})`;
  return facts.join(" | ");
}

/** Top-level dispatch used by both the pi tool and the test rig. */
export function runRecallPerson(name: string, dbPath?: string): string {
  const n = (name ?? "").trim();
  if (!n) return "(no person specified)";
  // The household-registry id is the person's name lowercased — that is
  // the `person:<id>` namespace convention bridge.py writes against.
  // fetchPersonMemories applies the lowercasing.
  const rows = fetchPersonMemories(n, {
    limit: PERSON_MEMORY_MAX_FACTS,
    dbPath,
  });
  return formatPersonRecall(n, rows);
}

/** Pi tool descriptor — passed to `pi.registerTool` from index.ts. */
export const recallPersonTool = {
  name: "recall_person",
  label: "Recall Person",
  description:
    "Recall durable facts Dotty has learned about a specific household " +
    "member — their preferences, relationships, recent context. Use when " +
    "the conversation turns to a named person and you want what Dotty " +
    "already knows about them.",
  promptSnippet:
    "Look up what Dotty remembers about a named household member.",
  promptGuidelines: [
    "Call recall_person when a named person comes up and you want their " +
      "stored preferences or context. For general past-conversation " +
      "recall that isn't about one specific person, use memory_lookup.",
  ],
  parameters: Type.Object({
    name: Type.String({
      description:
        "The person's name — matched case-insensitively against the " +
        "household registry id.",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { name: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = runRecallPerson(params.name);
    return { content: [{ type: "text", text }] };
  },
};
