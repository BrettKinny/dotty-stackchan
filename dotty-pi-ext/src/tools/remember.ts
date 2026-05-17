// remember voice tool — pi-extension port of
// bridge.py:/api/voice/remember (lines ~4131-4145).
//
// Contract (must match the Python original so the LLM that was tuned
// against it sees no behaviour change):
//   - Empty / whitespace fact → "(empty fact)"
//   - Insert success          → "(remembered)"
//   - Insert failure          → "(remember failed)"
//
// Calls storeMemory() with category="core" + importance=0.7, matching
// bridge.py's /api/voice/remember handler (longer-retention namespace
// than the per-turn /memory_log writes).

import { Type } from "typebox";
import { storeMemory } from "../lib/brain_db.ts";

// bridge.py truncates `fact` to 300 chars before storing — keep parity.
const FACT_MAX_CHARS = 300;

/** Top-level dispatch used by both the pi tool and the test rig. */
export function runRemember(
  fact: string,
  sessionId: string | null = null,
  dbPath?: string,
): string {
  const trimmed = (fact ?? "").trim();
  if (!trimmed) return "(empty fact)";
  // Slice by codepoint, matching memory_lookup's truncation convention.
  const cp = Array.from(trimmed);
  const capped =
    cp.length > FACT_MAX_CHARS
      ? cp.slice(0, FACT_MAX_CHARS).join("")
      : trimmed;
  const ok = storeMemory({
    content: capped,
    category: "core",
    namespace: "voice",
    importance: 0.7,
    sessionId,
    dbPath,
  });
  return ok ? "(remembered)" : "(remember failed)";
}

/** Pi tool descriptor — passed to `pi.registerTool` from index.ts. */
export const rememberTool = {
  name: "remember",
  label: "Remember",
  description:
    "Store a durable fact about the user, the household, or something " +
    "Dotty has learned that should persist across conversations. Use " +
    "sparingly — short, specific facts only (≤300 chars).",
  promptSnippet:
    "Persist a short fact to Dotty's long-term memory.",
  promptGuidelines: [
    "Call remember when the user shares a stable fact worth keeping " +
      "(name, preference, relationship, recurring event). Don't store " +
      "ephemeral chat — that's already logged automatically.",
    "Keep the fact short and self-contained (≤300 chars). Longer text " +
      "is silently truncated.",
  ],
  parameters: Type.Object({
    fact: Type.String({
      description:
        "The fact to remember. Will be stored verbatim (trimmed + " +
        "truncated to 300 codepoints) into the core memory namespace.",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { fact: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = runRemember(params.fact);
    return { content: [{ type: "text", text }] };
  },
};
