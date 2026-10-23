// memory_lookup voice tool — pi-extension port of
// bridge.py:_voice_tool_memory_lookup (lines ~3982-3995).
//
// Contract (must match the Python original so the LLM that was tuned
// against it sees no behaviour change):
//   - Empty / whitespace query → "(empty query)"
//   - No matches             → "(no memories found)"
//   - Otherwise              → top-3 of top-5 FTS results, each
//                              truncated to 200 chars (197 + "...") and
//                              pipe-joined with " | ".

import { Type } from "typebox";
import { searchMemories, type MemoryRow } from "../lib/brain_db.ts";
import { cleanStoredTurn } from "../lib/turn_text.ts";

const MAX_SNIPPETS = 3;
const SNIPPET_MAX_CHARS = 200;
const SNIPPET_TRUNC_HEAD = 197; // 200 - len("...")

/**
 * Pure formatter — separated from the tool wrapper so the test rig can
 * exercise it without going through pi's `execute` callback shape.
 */
export function formatLookupResult(rows: MemoryRow[]): string {
  if (rows.length === 0) return "(no memories found)";
  const snippets: string[] = [];
  for (const r of rows.slice(0, MAX_SNIPPETS)) {
    const trimmed = (r.content ?? "").trim();
    // Slice by Unicode codepoints, not UTF-16 code units — bridge.py
    // uses Python's str[:N] which counts codepoints, so a BMP-overflow
    // char like 😊 (one codepoint, two UTF-16 units) would otherwise
    // make us truncate earlier than the oracle on every emoji-bearing
    // memory. Array.from(str) splits on codepoint boundaries.
    const cp = Array.from(trimmed);
    const c =
      cp.length > SNIPPET_MAX_CHARS
        ? cp.slice(0, SNIPPET_TRUNC_HEAD).join("") + "..."
        : trimmed;
    snippets.push(c);
  }
  return snippets.join(" | ");
}

/**
 * Drop prompt scaffolding from stored conversation rows before they are
 * formatted. Kept apart from formatLookupResult so that function stays
 * byte-compatible with the bridge.py oracle.
 */
export function cleanLookupRows(rows: MemoryRow[]): MemoryRow[] {
  return rows.map((row) => ({ ...row, content: cleanStoredTurn(row.content ?? "") }));
}

/** Top-level dispatch used by both the pi tool and the test rig. */
export function runMemoryLookup(query: string, dbPath?: string): string {
  const q = (query ?? "").trim();
  if (!q) return "(empty query)";
  const rows = searchMemories(q, { limit: 5, dbPath });
  return formatLookupResult(cleanLookupRows(rows));
}

/** Pi tool descriptor — passed to `pi.registerTool` from index.ts. */
export const memoryLookupTool = {
  name: "memory_lookup",
  label: "Memory Lookup",
  description:
    "Search Dotty's long-term memory for relevant facts about the user, " +
    "the household, or prior conversations. Use when the user asks about " +
    "something Dotty might have been told before.",
  promptSnippet:
    "Search Dotty's long-term memory store for a query string.",
  promptGuidelines: [
    "Use memory_lookup when the user asks about prior conversations, " +
      "the household, or details Dotty might have been told before. " +
      "Don't guess — query first.",
  ],
  parameters: Type.Object({
    query: Type.String({
      description: "Free-text search query. Will be phrase-matched against the FTS5 index.",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { query: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = runMemoryLookup(params.query);
    return { content: [{ type: "text", text }] };
  },
};
