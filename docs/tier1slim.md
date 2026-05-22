---
title: Tier1Slim — Two-Tier Voice LLM (alternate)
description: How the Tier1Slim alternate provider runs a small/fast model for inner-loop chat and escalates tool calls via the bridge escalation endpoint (non-functional post-cutover).
---

# Tier1Slim — Two-Tier Voice LLM (alternate)

Tier1Slim is an **alternate** voice LLM backend. The default is `PiVoiceLLM` (see [llm-backends.md](./llm-backends.md)). Tier1Slim splits the work in two:

- **Inner loop** — every plain conversational turn goes directly to a small, fast model (default: `qwen3.5:4b` against a local llama-swap endpoint), no bridge round-trip. Warm latency is well under 1 s.
- **Escalation** — when the small model emits a structured `tool_call`, Tier1Slim POSTs the call to `POST /api/voice/escalate` on bridge.py.

**Post-cutover status (2026-05-19, issue #36):** `POST /api/voice/escalate` was served by the ZeroClaw bridge voice path, which was retired in #36. The escalation endpoint is non-functional in the current stack. Tier1Slim is therefore a **chitchat-only rollback path** — plain conversational turns work, but tool calls (`memory_lookup`, `think_hard`, `take_photo`, `play_song`) do not reach a live backend. Use `PiVoiceLLM` for full tool support.

The provider is selected with `selected_module.LLM: Tier1Slim` in `.config.yaml`. Source: `custom-providers/tier1_slim/tier1_slim.py`.

## When to use it

| You want | Use |
|---|---|
| Snappy plain chitchat ("what colour is the sky?") under 1 s, no tool calls needed | **Tier1Slim** (chitchat-only rollback) |
| Every voice turn to go through a full agent loop (memory, multi-step reasoning, tool chains) | `PiVoiceLLM` (default) |
| Voice path that can hot-swap between local and cloud backends with no daemon restart | **Tier1Slim** (inner loop only; smart-mode flip still works) |

Note: tool escalation (`memory_lookup`, `think_hard`, `take_photo`, `play_song`) via `POST /api/voice/escalate` is non-functional post-cutover. If the small model emits a `tool_call`, the escalation POST will fail. Tier1Slim is best used as a lightweight chitchat fallback when the dotty-pi agent is unavailable.

## Models and routing

```
                selected_module.LLM = Tier1Slim
                            │
                            ▼
            Tier1Slim (custom-providers/tier1_slim/)
                            │
        ┌───────────────────┴────────────────────┐
        │                                        │
No tool_calls emitted                  tool_calls emitted
        │                                        │
        ▼                                        ▼
llama-swap (default)          POST /api/voice/escalate → bridge.py
qwen3.5:4b @ :8080/v1         (non-functional post-cutover; endpoint
~500 ms warm                   was served by the retired ZeroClaw
                               voice path)
```

Smart-mode flips the inner-loop target between local and cloud:

| `smart_mode` | Model | URL | Notes |
|---|---|---|---|
| OFF (default) | `TIER1SLIM_LOCAL_MODEL` (`qwen3.5:4b`) | `TIER1SLIM_LOCAL_URL` (llama-swap, `http://localhost:8080/v1` by default) | Free, fast, fully local. |
| ON | `SMART_MODEL` (`anthropic/claude-sonnet-4-6`) | `TIER1SLIM_CLOUD_URL` (`https://openrouter.ai/api/v1` by default) | Costs money. Requires `TIER1SLIM_CLOUD_API_KEY` (or `OPENROUTER_API_KEY`) to be set. |

The flip is in-process and instant — the next turn lands on the new backend with no docker restart. The Tier1Slim instance is mutated by `set_runtime(model, url, api_key)` in `tier1_slim.py` (driven from the bridge by `_apply_tier1slim_runtime` → `/xiaozhi/admin/set-tier1slim-model`).

## The four escalation tools

These tools are defined in `tier1_slim.py:TOOLS` and sent to `POST /api/voice/escalate`. **They are non-functional in the current stack** — the escalation endpoint was served by the retired ZeroClaw voice path. Documented here for reference; use `PiVoiceLLM` for equivalent functionality.

| Tool | Purpose | Escalation target (pre-cutover) | Filler phrase |
|---|---|---|---|
| `memory_lookup` | Recall a fact from a past conversation. Use when the user says "do you remember…" or refers to a past topic by name. | bridge `/api/voice/escalate` (short timeout, 5 s) | none (lands fast) |
| `think_hard` | Delegate a hard question (multi-step planning, 3+ digit arithmetic). | bridge `/api/voice/escalate` → `qwen3.6:27b-think` via llama-swap (long timeout, 30 s) | none |
| `play_song` | Play a song through the speaker. | bridge → xiaozhi `/xiaozhi/admin/play-asset` (fire-and-forget) | none |
| `take_photo` | Look through Dotty's camera and describe what's visible. | bridge → VLM (`VLM_MODEL`, default `google/gemini-2.0-flash-001`) | "😮 Let me have a look." |

Per-tool filler phrases (`tier1_slim.py:TOOL_FILLERS`) give TTS something to say while a slow tool runs. `None` means silent.

## Wire format

### Inner-loop call (model → llama-swap)

Plain OpenAI-compatible chat completion with `tools=auto`. The slim 4 B model decides whether to answer directly or emit a `tool_calls` array.

### Escalation call (Tier1Slim → bridge)

Defined in the source for reference; non-functional in the current stack.

```http
POST {BRIDGE_URL}/api/voice/escalate
Content-Type: application/json

{
  "tool": "<tool_name>",
  "args": {"query": "..."} | {"question": "..."} | {"name": "..."} | {},
  "session_id": "<xiaozhi session id>"
}
```

Response:

```json
{"result": "<short string, truncated to 1000 chars>"}
```

### Memory side channel

Two fire-and-forget POSTs are defined alongside escalation (also non-functional post-cutover):

- `POST /api/voice/remember` — `{"fact": "...", "session_id": "..."}`. Triggered when the model emits a `[REMEMBER: ...]` marker inside the final reply. The marker is stripped before TTS.
- `POST /api/voice/memory_log` — `{"user": "...", "assistant": "...", "session_id": "..."}`. Logs the turn for future `memory_lookup` calls. Posted at end-of-turn.

Both have 2 s timeouts and never raise — failures log and continue.

## Configuration

Provider block in `.config.yaml`:

```yaml
selected_module:
  LLM: Tier1Slim

LLM:
  Tier1Slim:
    type: tier1_slim
    url: <LLAMA_SWAP_URL>          # e.g. http://192.168.1.67:8080/v1
    api_key: <LLAMA_SWAP_KEY>      # any string; llama-swap doesn't enforce
    model: qwen3.5:4b
    max_tokens: 256
    temperature: 0.7
    timeout: 60
    persona_file: personas/dotty_voice.md
```

Environment variables (read by bridge.py for smart-mode flips):

| Variable | Default | Purpose |
|---|---|---|
| `DOTTY_VOICE_PROVIDER` | `tier1slim` | Set to `tier1slim` to enable the Tier1Slim hot-swap path. |
| `TIER1SLIM_LOCAL_URL` | `http://localhost:8080/v1` | Inner-loop endpoint when smart_mode is OFF. |
| `TIER1SLIM_LOCAL_MODEL` | `qwen3.5:4b` | Model name on the local endpoint. |
| `TIER1SLIM_LOCAL_API_KEY` | `dotty-voice` | Sent as `Authorization: Bearer …`. llama-swap ignores. |
| `TIER1SLIM_CLOUD_URL` | `https://openrouter.ai/api/v1` | Endpoint when smart_mode is ON. |
| `TIER1SLIM_CLOUD_API_KEY` | _(unset; falls back to `OPENROUTER_API_KEY`)_ | Required for OFF→ON smart-mode flip. |
| `SMART_MODEL` | `anthropic/claude-sonnet-4-6` | Model name when smart_mode is ON. |
| `BRIDGE_URL` | `http://localhost:8080` | Where Tier1Slim posts escalations. |
| `BRIDGE_TIMEOUT_SHORT` | `5` (s) | Timeout for `memory_lookup` etc. |
| `BRIDGE_TIMEOUT_LONG` | `30` (s) | Timeout for `think_hard`. |

## Persona handling

Tier1Slim uses a single small system prompt (`personas/dotty_voice.md` by default) and discards xiaozhi-server's top-level `prompt:` block. The 4 B chat template only honours one system message, and xiaozhi's default prompt is sized for full agentic paths — concatenating both starves the small model's attention. If no `persona_file` is set, Tier1Slim falls back to merging the dialogue's system messages.

The emoji + English rules are appended per turn via `build_turn_suffix(KID_MODE)` (`custom-providers/textUtils.py`). Same set as elsewhere: 😊😆😢😮🤔😠😐😍😴. Fallback prefix is 😐.

## See also

- [llm-backends.md](./llm-backends.md) — choosing between PiVoiceLLM (default), Tier1Slim, and OpenAICompat.
- [voice-pipeline.md](./voice-pipeline.md) — where Tier1Slim sits in the ASR → LLM → TTS chain.
- [brain.md](./brain.md) — the dotty-pi agent (the active brain) and its tool set.
- [protocols.md](./protocols.md) — `/api/voice/escalate` wire format and its post-cutover status.
- [modes.md](./modes.md) — how smart_mode swaps the inner-loop backend.
- [cutover-behaviour.md](./cutover-behaviour.md) — historical runbook for the ZeroClaw→PiVoiceLLM cutover.
- [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md) — running `qwen3.5:4b` + `qwen3.6:27b-think` concurrently on one GPU pair.
