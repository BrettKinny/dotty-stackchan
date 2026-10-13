// Thin llama-swap client — just the chat-completions POST shape used by
// the voice tools. Lives next to brain_db.ts because llama-swap is the
// other "infrastructure dep" the extension talks to from inside the
// dotty-pi container (both on localhost via host networking).
//
// We don't generalise — voice tools touch a tiny slice of the OpenAI
// API and bridge.py never grew an abstraction either. Each consuming
// tool builds its own request body via a pure helper so the test rig
// can diff against bridge.py's exact shape.

const DEFAULT_URL =
  process.env.VOICE_THINKER_URL ?? "http://localhost:8080/v1/chat/completions";
// 90 s, not 30: the reasoner is not kept resident, and a cold load alone is
// 30-50 s. At 30 s the first think_hard after any idle period was cancelled
// mid-load (llama-swap logged "502 … context canceled" at ~31 s).
export const DEFAULT_TIMEOUT_SEC = Number(process.env.VOICE_THINKER_TIMEOUT ?? "90");

export interface ChatCompletionRequest {
  model: string;
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
  max_tokens: number;
  temperature: number;
  stream: boolean;
  chat_template_kwargs?: Record<string, unknown>;
}

export class TimeoutError extends Error {
  readonly isTimeout = true as const;
}

export interface PostOptions {
  url?: string;
  timeoutSec?: number;
}

/**
 * POST a chat-completion request and return the assistant content. The
 * caller is responsible for shaping {@link ChatCompletionRequest} —
 * keeping the body construction in each tool's pure helper means the
 * oracle tests can diff request bodies without going through this fn.
 *
 * Throws {@link TimeoutError} on AbortSignal timeout; throws Error
 * subclasses on non-2xx / parse failures / network errors.
 */
export async function postChatCompletion(
  body: ChatCompletionRequest,
  opts: PostOptions = {},
): Promise<string> {
  const url = opts.url ?? DEFAULT_URL;
  const timeoutMs = (opts.timeoutSec ?? DEFAULT_TIMEOUT_SEC) * 1000;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    if (!resp.ok) {
      throw new Error(`llama-swap HTTP ${resp.status}`);
    }
    const data = (await resp.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
    };
    return data.choices?.[0]?.message?.content ?? "";
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new TimeoutError(`llama-swap timeout after ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
