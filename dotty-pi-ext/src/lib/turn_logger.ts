// Per-turn conversation auto-log. Subscribes to pi's `agent_end` event
// (once per user prompt) and writes a `category=conversation` row to
// brain.db, mirroring bridge.py's /api/voice/memory_log handler.
//
// The #36 cutover executed 2026-05-19 — xiaozhi now runs PiVoiceLLM, so
// this is the LIVE conversation write path. It took over from bridge.py's
// retired /api/voice/memory_log endpoint and is the sole surviving write
// path after the bridge's voice role was removed.

import type {
  AgentEndEvent,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { storeMemory } from "./brain_db.ts";

// bridge.py:/api/voice/memory_log truncates user→500 chars, assistant→1000
// before storing. Keep parity so the LLM that was tuned against this
// content shape sees no behaviour change.
const USER_MAX_CHARS = 500;
const ASSISTANT_MAX_CHARS = 1000;

/** Codepoint-aware truncation (matches Python's str[:N]). */
function truncCodepoints(s: string, max: number): string {
  const cp = Array.from(s);
  return cp.length > max ? cp.slice(0, max).join("") : s;
}

/**
 * Extract user + assistant text from an agent_end messages array.
 *
 * AgentMessage[] is the full conversation transcript for the prompt.
 * For per-turn logging we want the LAST user message (the prompt that
 * just completed) and the concatenation of ALL assistant text content
 * after it. Skip thinking, tool calls, images, tool results — those
 * aren't part of what Dotty "said".
 *
 * Returns empty strings if no relevant content is present; the caller
 * decides whether to write or skip.
 */
export function extractTurnText(messages: readonly any[]): {
  user: string;
  assistant: string;
} {
  let lastUserIdx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "user") {
      lastUserIdx = i;
      break;
    }
  }
  if (lastUserIdx === -1) return { user: "", assistant: "" };

  const userMsg = messages[lastUserIdx];
  const user = stringifyUserContent(userMsg.content).trim();

  const assistantParts: string[] = [];
  for (let i = lastUserIdx + 1; i < messages.length; i++) {
    const m = messages[i];
    if (m?.role !== "assistant") continue;
    assistantParts.push(stringifyAssistantContent(m.content));
  }
  const assistant = assistantParts.join("").trim();

  return { user, assistant };
}

function stringifyUserContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const c of content) {
    if (c && typeof c === "object" && (c as any).type === "text") {
      parts.push((c as any).text ?? "");
    }
  }
  return parts.join("");
}

function stringifyAssistantContent(content: unknown): string {
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const c of content) {
    // Only text. Skip "thinking" (per spike telemetry: 19 thinking deltas
    // per turn would dominate the log and isn't what Dotty said) and
    // "toolCall" (tool results are separate messages with their own role).
    if (c && typeof c === "object" && (c as any).type === "text") {
      parts.push((c as any).text ?? "");
    }
  }
  return parts.join("");
}

/**
 * Build the conversation-row content string. Matches bridge.py:
 *   content = f"user: {user[:500]} | assistant: {assistant[:1000]}"
 * with both halves trimmed first (the handler calls .strip() before slicing).
 */
export function formatTurnLog(user: string, assistant: string): string {
  const u = truncCodepoints(user.trim(), USER_MAX_CHARS);
  const a = truncCodepoints(assistant.trim(), ASSISTANT_MAX_CHARS);
  return `user: ${u} | assistant: ${a}`;
}

/**
 * agent_end handler — installed via pi.on("agent_end", logTurnEnd) in
 * index.ts. Fire-and-forget: writes the row in the background and
 * swallows errors so a memory write failure can never break the agent
 * loop. Bridge.py uses the same pattern (asyncio.to_thread + _spawn).
 */
export async function logTurnEnd(
  event: AgentEndEvent,
  _ctx: ExtensionContext,
): Promise<void> {
  try {
    const { user, assistant } = extractTurnText(event.messages);
    if (!user && !assistant) return; // matches bridge.py: skip empty turns
    const content = formatTurnLog(user, assistant);
    // session_id is intentionally null here — pi's session model is
    // per-process, not per-voice-turn, so plumbing it through doesn't
    // add value yet. bridge.py also accepts null for the conversation
    // namespace today.
    storeMemory({
      content,
      category: "conversation",
      namespace: "voice",
      importance: 0.3,
      sessionId: null,
    });
  } catch (err) {
    process.stderr.write(`[turn_logger] log failed: ${err}\n`);
  }
}
