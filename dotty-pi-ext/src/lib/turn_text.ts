// Separates what a person said from the scaffolding the voice provider wraps
// around it. PiVoiceLLM appends tool-routing guidance and the HARD CONSTRAINTS
// rules to every prompt (and, for recall, a block of memory search results).
// None of that is conversation: it must not be written to brain.db, and rows
// written before this existed must not be read back out with it attached.

// Each marker is matched only at the start of a line after a blank/new line,
// exactly as the provider emits it, so a person saying the words is untouched.
const SCAFFOLD_START =
  /\n+(?:MEMORY SEARCH RESULTS \(|VOICE TOOL ROUTING:|---\nHARD CONSTRAINTS for THIS reply)/;

/** Unwrap the legacy ASR envelope `{"content": "..."}` if that is all there is. */
function unwrapAsrEnvelope(text: string): string {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return text;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && typeof parsed.content === "string") {
      return parsed.content;
    }
  } catch {
    // Not JSON — leave the text exactly as spoken.
  }
  return text;
}

/** The user's own words from a prompt as pi received it. */
export function stripTurnBoilerplate(text: string): string {
  const raw = text ?? "";
  const cut = raw.search(SCAFFOLD_START);
  const spoken = cut === -1 ? raw : raw.slice(0, cut);
  return unwrapAsrEnvelope(spoken).trim();
}

const USER_PREFIX = "user: ";
const ASSISTANT_SEPARATOR = " | assistant: ";

/**
 * Clean one stored `user: … | assistant: …` conversation row. Rows written
 * before the logger stripped scaffolding hold up to 500 characters of it
 * (truncated mid-rule) between the user's words and the reply.
 */
export function cleanStoredTurn(content: string): string {
  const raw = content ?? "";
  if (!raw.startsWith(USER_PREFIX)) return raw;
  const split = raw.indexOf(ASSISTANT_SEPARATOR);
  const user = stripTurnBoilerplate(raw.slice(USER_PREFIX.length, split === -1 ? undefined : split));
  return split === -1
    ? `${USER_PREFIX}${user}`
    : `${USER_PREFIX}${user}${ASSISTANT_SEPARATOR}${raw.slice(split + ASSISTANT_SEPARATOR.length).trim()}`;
}
