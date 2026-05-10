"""Tier 1 slim LLM provider for Dotty's voice path.

Talks directly to llama-swap (or any OpenAI-compatible endpoint) with a small
~500-token system prompt and a five-tool catalogue. When the model emits
`tool_calls`, this provider:

  1. Yields a brief filler phrase to TTS so the user hears something within
     ~500 ms ("hmm, let me think…").
  2. POSTs each tool call to bridge.py's /api/voice/escalate endpoint, which
     dispatches to ZeroClaw memory, the 27B (delegate.thinker), or firmware
     MCP tools, and returns the result.
  3. Makes a second streaming chat call with the tool results in context and
     streams the final answer to TTS.

If no tool_calls are emitted, the first response.content is yielded directly
(single-call fast path, ~500 ms warm).

The bridge URL comes from env var BRIDGE_URL (default http://192.168.1.54:8080).
"""

import json
import os
import re

import requests

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.utils.textUtils import (
    ALLOWED_EMOJIS,
    FALLBACK_EMOJI,
    build_turn_suffix,
)

TAG = __name__
logger = setup_logging()

KID_MODE = os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")
_TURN_SUFFIX = build_turn_suffix(KID_MODE)

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://192.168.1.54:8080").rstrip("/")
BRIDGE_TIMEOUT_SHORT = float(os.environ.get("BRIDGE_TIMEOUT_SHORT", "5"))   # memory_lookup
BRIDGE_TIMEOUT_LONG = float(os.environ.get("BRIDGE_TIMEOUT_LONG", "30"))    # think_hard

# Strip [REMEMBER: ...] markers from final user-facing text. Captured group 1
# is the fact, which we ship to bridge.py asynchronously.
_REMEMBER_RE = re.compile(r"\s*\[REMEMBER:\s*([^\]]+)\s*\]\s*", re.IGNORECASE)


# Tool catalogue — kept short for 4B reliability. Descriptions are terse to
# minimize prompt-token cost and to give clear gating to the model.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_lookup",
            "description": (
                "Recall something the user told you in a past conversation. "
                "Use ONLY when user asks 'do you remember…', 'what did I tell you about…' "
                "or refers to a past topic by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Short keywords describing what to recall (e.g. 'birthday', 'favourite colour').",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think_hard",
            "description": (
                "Delegate a hard question to a more capable model. Use for math with 3+ digit "
                "numbers, multi-step planning, or any question you would otherwise have to guess at."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The exact question to delegate."}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_song",
            "description": "Play a song by name through Dotty's speaker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Song name, e.g. 'twinkle twinkle little star'."}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_led",
            "description": "Set the colour of one of Dotty's LED rings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "side": {
                        "type": "string",
                        "enum": ["left", "right"],
                        "description": "Which ring to change.",
                    },
                    "color": {
                        "type": "string",
                        "description": "Colour name: 'red', 'green', 'blue', 'warm', 'off'.",
                    },
                },
                "required": ["side", "color"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_photo",
            "description": "Look through Dotty's camera and return a short description of what's visible.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Per-tool filler phrases. Spoken via the TTS pipeline while the tool runs.
# `None` means silent (fire-and-forget actions land instantly so a filler
# would be misleading).
TOOL_FILLERS = {
    "memory_lookup": "🤔 Hmm, let me think.",
    "think_hard": "🤔 Let me work that out.",
    "take_photo": "😮 Let me have a look.",
    "play_song": None,
    "set_led": None,
}


def _load_persona(path):
    """Read a persona markdown file and return its contents as a string."""
    if not path:
        return ""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved):
        resolved = os.path.join(os.getcwd(), resolved)
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.bind(tag=TAG).warning(f"Persona file not found: {resolved}")
        return ""
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Failed to read persona file {resolved}: {exc}")
        return ""


def _strip_remember(text):
    """Remove [REMEMBER: ...] markers from text. Returns (clean_text, [facts])."""
    facts = [m.group(1).strip() for m in _REMEMBER_RE.finditer(text or "")]
    clean = _REMEMBER_RE.sub(" ", text or "").strip()
    return clean, facts


class LLMProvider(LLMProviderBase):
    """Slim Tier 1 voice provider with structured tool-use escalation."""

    def __init__(self, config):
        self.base_url = (config.get("url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("Tier1Slim requires 'url' (e.g. http://192.168.1.67:8080/v1)")
        self.api_key = config.get("api_key") or "tier1"
        self.model = config.get("model") or ""
        if not self.model:
            raise ValueError("Tier1Slim requires 'model'")
        self.max_tokens = int(config.get("max_tokens", 256))
        self.temperature = float(config.get("temperature", 0.7))
        self.timeout = float(config.get("timeout", 60))

        persona_path = config.get("persona_file") or ""
        self._persona = _load_persona(persona_path)
        if not self._persona:
            self._persona = config.get("system_prompt") or ""

    # ------------------------------------------------------------------
    # message + payload assembly
    # ------------------------------------------------------------------

    def _build_messages(self, dialogue):
        """Assemble the message list for llama-swap.

        qwen3.5:4b's chat template enforces exactly ONE system message at the
        beginning. xiaozhi-server normally injects the top-level `prompt:`
        block as a system message at the head of `dialogue` — that one is
        sized for the ZeroClawLLM agentic path and conflicts with our slim
        persona. So when we have a persona file loaded we use ONLY that as
        the system message and drop dialogue's system messages. When no
        persona file is loaded we concat dialogue's system messages into one.
        """
        messages = []
        dialogue_systems = [m.get("content", "") for m in dialogue if m.get("role") == "system"]
        if self._persona:
            messages.append({"role": "system", "content": self._persona})
        elif dialogue_systems:
            merged = "\n\n".join(s for s in dialogue_systems if s)
            messages.append({"role": "system", "content": merged})

        last_user_idx = None
        for i, msg in enumerate(dialogue):
            if msg.get("role") == "user":
                last_user_idx = i

        for i, msg in enumerate(dialogue):
            role = msg.get("role", "user")
            if role == "system":
                continue  # already handled above (single merged system message)
            content = msg.get("content", "")
            if i == last_user_idx:
                content = content + _TURN_SUFFIX
            messages.append({"role": role, "content": content})
        return messages

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _completions_url(self):
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # ------------------------------------------------------------------
    # call layer (non-streaming for tool detection, streaming for final)
    # ------------------------------------------------------------------

    def _first_call(self, messages):
        """Non-streaming call with tools=auto. Returns the raw assistant message
        dict {role, content, tool_calls?} or None on failure."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": False,
        }
        try:
            r = requests.post(
                self._completions_url(),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                logger.bind(tag=TAG).error(
                    f"Tier1 first-call HTTP {r.status_code}: {r.text[:500]}"
                )
                r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]
        except Exception as exc:
            logger.bind(tag=TAG).exception(f"Tier1 first-call failed: {exc}")
            return None

    def _stream_final(self, messages):
        """Streaming call with tool results already in messages. No tools/tool_choice
        — we want a plain answer."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        try:
            resp = requests.post(
                self._completions_url(),
                json=payload,
                headers={**self._headers(), "Accept": "text/event-stream"},
                timeout=self.timeout,
                stream=True,
            )
            resp.raise_for_status()
        except Exception:
            logger.bind(tag=TAG).exception("Tier1 stream-final failed")
            yield f"{FALLBACK_EMOJI} I had trouble with that — say it differently?"
            return

        full_text = []
        emoji_checked = False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                data_str = line[6:] if line.startswith("data: ") else line
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content") or ""
                if not content:
                    continue
                full_text.append(content)

                if not emoji_checked:
                    so_far = "".join(full_text).lstrip()
                    if so_far:
                        emoji_checked = True
                        if not any(so_far.startswith(e) for e in ALLOWED_EMOJIS):
                            yield f"{FALLBACK_EMOJI} "
                yield content
        except requests.exceptions.ChunkedEncodingError:
            logger.bind(tag=TAG).warning("Tier1 stream interrupted")
        except Exception:
            logger.bind(tag=TAG).exception("Tier1 stream error")

        if not full_text or not "".join(full_text).strip():
            yield f"{FALLBACK_EMOJI} (no response)"

    # ------------------------------------------------------------------
    # tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(self, name, args, session_id):
        """POST to bridge /api/voice/escalate, return the result string. Tools
        with no useful return value (play_song, set_led) get a generic 'ok'."""
        timeout = BRIDGE_TIMEOUT_LONG if name == "think_hard" else BRIDGE_TIMEOUT_SHORT
        try:
            r = requests.post(
                f"{BRIDGE_URL}/api/voice/escalate",
                json={"tool": name, "args": args, "session_id": session_id},
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
            return str(data.get("result", ""))[:1000]
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning(f"Tier1 escalate timeout: {name}")
            return f"({name} took too long)"
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"Tier1 escalate failed for {name}: {exc}")
            return f"({name} unavailable)"

    def _post_remember(self, fact, session_id):
        """Fire-and-forget POST to bridge /api/voice/remember. Errors logged, never raised."""
        try:
            requests.post(
                f"{BRIDGE_URL}/api/voice/remember",
                json={"fact": fact, "session_id": session_id},
                timeout=2,
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"Tier1 remember POST failed: {exc}")

    def _post_memory_log(self, user_text, assistant_text, session_id):
        """Fire-and-forget POST to bridge /api/voice/memory_log."""
        try:
            requests.post(
                f"{BRIDGE_URL}/api/voice/memory_log",
                json={"user": user_text, "assistant": assistant_text, "session_id": session_id},
                timeout=2,
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"Tier1 memory_log POST failed: {exc}")

    # ------------------------------------------------------------------
    # public interface
    # ------------------------------------------------------------------

    def response(self, session_id, dialogue, **kwargs):
        messages = self._build_messages(dialogue)

        # Pull the most recent user turn for the memory log.
        last_user = ""
        for msg in reversed(dialogue):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break

        first = self._first_call(messages)
        if first is None:
            yield f"{FALLBACK_EMOJI} My brain is offline — try again in a moment."
            return

        tool_calls = first.get("tool_calls") or []

        # Fast path: no tool calls. Yield the model's content (with [REMEMBER:] handling).
        if not tool_calls:
            content = first.get("content") or ""
            clean, facts = _strip_remember(content)
            for fact in facts:
                self._post_remember(fact, session_id)
            for chunk in self._yield_text(clean):
                yield chunk
            self._post_memory_log(last_user, clean, session_id)
            return

        # Tool path. Yield filler ASAP so TTS has something to say.
        filler_text = None
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            f = TOOL_FILLERS.get(name)
            if f:
                filler_text = f
                break
        if filler_text:
            yield filler_text + " "

        # Dispatch each tool call. Fire-and-forget side-effect tools dispatch
        # async via bridge; data tools (memory_lookup, think_hard, take_photo)
        # block here until the result arrives.
        tool_messages = []
        for call in tool_calls:
            call_id = call.get("id", "")
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
                logger.bind(tag=TAG).warning(f"Tier1 malformed tool args: {fn.get('arguments')!r}")
            result = self._dispatch_tool(name, args, session_id)
            tool_messages.append(
                {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}
            )

        # Second call with tool results — stream the final answer to TTS.
        # Strip the assistant's tool_calls turn down to the API-required shape.
        assistant_turn = {
            "role": "assistant",
            "content": first.get("content") or "",
            "tool_calls": tool_calls,
        }
        followup_messages = messages + [assistant_turn] + tool_messages

        final_chunks = []
        for chunk in self._stream_final(followup_messages):
            final_chunks.append(chunk)
            yield chunk

        full_assistant = "".join(final_chunks)
        clean, facts = _strip_remember(full_assistant)
        for fact in facts:
            self._post_remember(fact, session_id)
        # Memory log — full final reply (after filler, after tool results).
        self._post_memory_log(last_user, clean, session_id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _yield_text(self, text):
        """Yield text as TTS chunks, ensuring an emoji prefix and stripping
        [REMEMBER:] markers (already stripped by caller, but defensive)."""
        text = text or ""
        if not any(text.lstrip().startswith(e) for e in ALLOWED_EMOJIS):
            yield FALLBACK_EMOJI + " "
        # Single-shot yield is fine for non-streaming path; xiaozhi will
        # sentence-chunk on its end via _SENTENCE_BOUNDARY.
        yield text
