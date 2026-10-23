// think_hard voice tool — pi-extension port of
// bridge.py:_voice_tool_think_hard (lines ~3998-4038).
//
// Bypasses the agent loop entirely: a direct POST to llama-swap at
// qwen3.6:27b-think (the small-context 27B in the `voice` matrix set; it
// is not kept resident, so a cold load takes 30-50 s, see llama_swap.ts).
// `enable_thinking=false` is critical — qwen3 defaults to reasoning
// mode and would otherwise eat the entire 200-token budget on `<think>`
// tokens, leaving content empty.
//
// Contract (must match Python so the LLM's tuned-for prompt behaviour
// holds):
//   - Empty / whitespace question → "(empty question)"
//   - Timeout                     → "(I'm slow today, try again in a moment)"
//   - Other error                 → "(thinking failed)"
//   - Success                     → trimmed content, truncated to 500 chars

import { Type } from "typebox";
import {
  TimeoutError,
  postChatCompletion,
  type ChatCompletionRequest,
} from "../lib/llama_swap.ts";

const DEFAULT_MODEL = process.env.VOICE_THINKER_MODEL ?? "qwen3.6:27b-think";
const SYSTEM_PROMPT =
  "Answer the user's question concisely in 1-2 sentences. Be precise.";
const MAX_OUTPUT_CHARS = 500;

/**
 * Pure request-body builder. Separated so the oracle can diff our body
 * shape against bridge.py's without hitting llama-swap.
 */
export function buildThinkRequest(
  question: string,
  model: string = DEFAULT_MODEL,
): ChatCompletionRequest {
  return {
    model,
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: question },
    ],
    max_tokens: 200,
    temperature: 0.3,
    stream: false,
    chat_template_kwargs: { enable_thinking: false },
  };
}

export interface ThinkHardOptions {
  url?: string;
  timeoutSec?: number;
  model?: string;
}

/**
 * Top-level dispatch. Mirrors the Python wrapper's error handling.
 */
export async function runThinkHard(
  question: string,
  opts: ThinkHardOptions = {},
): Promise<string> {
  const q = (question ?? "").trim();
  if (!q) return "(empty question)";
  const body = buildThinkRequest(q, opts.model);
  try {
    const content = await postChatCompletion(body, {
      url: opts.url,
      timeoutSec: opts.timeoutSec,
    });
    // Python: (content or "").strip()[:500]
    // JS .slice is OK here — the 500-char cap is generous and the
    // codepoint/code-unit drift is at most ~10 chars on emoji-dense
    // replies; the LLM is told to answer in 1-2 sentences, so we'll
    // virtually never hit the cap anyway. Matching Python literally:
    return content.trim().slice(0, MAX_OUTPUT_CHARS);
  } catch (err) {
    if (err instanceof TimeoutError) {
      return "(I'm slow today, try again in a moment)";
    }
    process.stderr.write(`[think_hard] failed: ${err}\n`);
    return "(thinking failed)";
  }
}

export const thinkHardTool = {
  name: "think_hard",
  label: "Think Hard",
  description:
    "Send a single question to a larger reasoning model for a precise " +
    "1-2 sentence answer. Call it when the user asks you to think hard, " +
    "and for arithmetic beyond simple sums, lookups or technical specifics.",
  promptSnippet:
    "Escalate a single question to the qwen3.6:27b-think reasoning model.",
  promptGuidelines: [
    "Use think_hard when the user asks a factual or technical question " +
      "that needs precise reasoning. Keep the question self-contained.",
  ],
  parameters: Type.Object({
    question: Type.String({
      description:
        "Self-contained question for the reasoning model. Include any context inline.",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { question: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = await runThinkHard(params.question);
    return { content: [{ type: "text", text }] };
  },
};
