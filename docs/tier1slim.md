---
title: Tier1Slim — Two-Tier Voice LLM
description: How the Tier1Slim provider runs a small/fast model for inner-loop chat and escalates tool calls to ZeroClaw via the bridge.
---

# Tier1Slim — Two-Tier Voice LLM

Tier1Slim is one of two LLM providers Dotty can use for the voice path; the other is `ZeroClawLLM` (full agent runtime, single tier). Tier1Slim splits the work in two:

- **Inner loop** — every plain conversational turn goes directly to a small, fast model (default: `qwen3.5:4b` against a local llama-swap endpoint), no bridge round-trip. Warm latency is well under 1 s.
- **Escalation** — when the small model emits a structured `tool_call`, Tier1Slim POSTs the call to the bridge's `/api/voice/escalate` endpoint, which dispatches to ZeroClaw memory, the 27 B thinker, or a firmware MCP tool, then streams the final answer back through TTS.

The provider is selected with `selected_module.LLM: Tier1Slim` in `.config.yaml`. Source: `custom-providers/tier1_slim/tier1_slim.py`.

## When to use it

| You want | Use |
|---|---|
| Snappy chitchat ("what colour is the sky?") under 1 s | **Tier1Slim** |
| Every voice turn to go through a full agent loop (memory, multi-step reasoning, tool chains) | `ZeroClawLLM` |
| Voice path that can hot-swap between local and cloud backends with no daemon restart | **Tier1Slim** |

Bridge code reads `DOTTY_VOICE_PROVIDER` to know which path is live. `"zeroclaw"` (default) means smart-mode flips rewrite ZeroClaw's TOML and restart the daemon; `"tier1slim"` means smart-mode flips call `/xiaozhi/admin/set-tier1slim-model` to hot-swap the live provider in xiaozhi-server.

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
   llama-swap (default)                  POST /api/voice/escalate
   qwen3.5:4b @ :8080/v1                 ──→ bridge.py
   ~500 ms warm                                      │
                                  ┌─────────────────┼─────────────────┬─────────────┐
                                  ▼                 ▼                 ▼             ▼
                          memory_lookup       think_hard          play_song    take_photo
                          (ZeroClaw FTS)      (qwen3.6:27b-think) (firmware)   (VLM via bridge)
```

Smart-mode flips the inner-loop target between local and cloud:

| `smart_mode` | Model | URL | Notes |
|---|---|---|---|
| OFF (default) | `TIER1SLIM_LOCAL_MODEL` (`qwen3.5:4b`) | `TIER1SLIM_LOCAL_URL` (llama-swap, `http://192.168.1.67:8080/v1` by default) | Free, fast, fully local. |
| ON | `SMART_MODEL` (`anthropic/claude-sonnet-4-6`) | `TIER1SLIM_CLOUD_URL` (`https://openrouter.ai/api/v1` by default) | Costs money. Requires `TIER1SLIM_CLOUD_API_KEY` (or `OPENROUTER_API_KEY`) to be set. |

The flip is in-process and instant — the next turn lands on the new backend with no docker restart. The Tier1Slim instance is mutated by `set_runtime(model, url, api_key)` in `tier1_slim.py` (driven from the bridge by `_apply_tier1slim_runtime` → `/xiaozhi/admin/set-tier1slim-model`).

## The four escalation tools

The catalogue is intentionally small to stay reliable on a 4 B model. Defined in `tier1_slim.py:TOOLS`.

| Tool | Purpose | Bridge dispatch | Filler phrase |
|---|---|---|---|
| `memory_lookup` | Recall a fact from a past conversation. Use when the user says "do you remember…" or refers to a past topic by name. | ZeroClaw memory (FTS). Short timeout (`BRIDGE_TIMEOUT_SHORT`, default 5 s). | none (lands fast) |
| `think_hard` | Delegate a hard question (multi-step planning, 3+ digit arithmetic). | `qwen3.6:27b-think` via llama-swap. Long timeout (`BRIDGE_TIMEOUT_LONG`, default 30 s). | none |
| `play_song` | Play a song through the speaker. | Bridge → xiaozhi `/xiaozhi/admin/play-asset`. | none (fire-and-forget) |
| `take_photo` | Look through Dotty's camera and describe what's visible. | Bridge → VLM (`VLM_MODEL`, default `google/gemini-2.0-flash-001`). | "😮 Let me have a look." |

Per-tool filler phrases (`tier1_slim.py:TOOL_FILLERS`) give TTS something to say while a slow tool runs. `None` means silent — used where the action lands instantly or would make a filler misleading.

## Wire format

### Inner-loop call (model → llama-swap)

Plain OpenAI-compatible chat completion with `tools=auto`. The slim 4 B model decides whether to answer directly or emit a `tool_calls` array.

### Escalation call (Tier1Slim → bridge)

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

`memory_lookup` and `think_hard` block until the result arrives. `play_song` and `take_photo` block too but the bridge returns quickly because the side effect is dispatched downstream.

### Memory side channel

Two fire-and-forget POSTs run alongside escalation:

- `POST /api/voice/remember` — `{"fact": "...", "session_id": "..."}`. Triggered when the model emits a `[REMEMBER: ...]` marker inside the final reply. The marker is stripped before TTS.
- `POST /api/voice/memory_log` — `{"user": "...", "assistant": "...", "session_id": "..."}`. Logs the turn so ZeroClaw can index it for future `memory_lookup` calls. Posted at end-of-turn.

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

Environment variables (read by the bridge for smart-mode flips):

| Variable | Default | Purpose |
|---|---|---|
| `DOTTY_VOICE_PROVIDER` | `zeroclaw` | Set to `tier1slim` to enable the hot-swap path. |
| `TIER1SLIM_LOCAL_URL` | `http://192.168.1.67:8080/v1` | Inner-loop endpoint when smart_mode is OFF. |
| `TIER1SLIM_LOCAL_MODEL` | `qwen3.5:4b` | Model name on the local endpoint. |
| `TIER1SLIM_LOCAL_API_KEY` | `dotty-voice` | Sent as `Authorization: Bearer …`. llama-swap ignores. |
| `TIER1SLIM_CLOUD_URL` | `https://openrouter.ai/api/v1` | Endpoint when smart_mode is ON. |
| `TIER1SLIM_CLOUD_API_KEY` | _(unset; falls back to `OPENROUTER_API_KEY`)_ | Required for OFF→ON smart-mode flip. |
| `SMART_MODEL` | `anthropic/claude-sonnet-4-6` | Model name when smart_mode is ON. |
| `BRIDGE_URL` | `http://192.168.1.54:8080` | Where Tier1Slim posts escalations. |
| `BRIDGE_TIMEOUT_SHORT` | `5` (s) | Timeout for `memory_lookup` etc. |
| `BRIDGE_TIMEOUT_LONG` | `30` (s) | Timeout for `think_hard`. |

## Persona handling

Tier1Slim uses a single small system prompt (`personas/dotty_voice.md` by default) and discards xiaozhi-server's top-level `prompt:` block. The 4 B chat template only honours one system message, and xiaozhi's default prompt is sized for the ZeroClawLLM agentic path — concatenating both starves the small model's attention. If no `persona_file` is set, Tier1Slim falls back to merging the dialogue's system messages.

The emoji + English rules are appended per turn via `build_turn_suffix(KID_MODE)` (`custom-providers/textUtils.py`). Same set as elsewhere: 😊😆😢😮🤔😠😐😍😴. Fallback prefix is 😐.

## See also

- [voice-pipeline.md](./voice-pipeline.md) — where Tier1Slim sits in the ASR → LLM → TTS chain.
- [brain.md](./brain.md) — the ZeroClaw agent that Tier1Slim escalates to.
- [protocols.md](./protocols.md) — `/api/voice/escalate`, `/api/voice/remember`, `/api/voice/memory_log` wire formats.
- [llm-backends.md](./llm-backends.md) — choosing between Tier1Slim, ZeroClawLLM, OpenAICompat.
- [modes.md](./modes.md) — how smart_mode swaps the inner-loop backend.
- [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md) — running `qwen3.5:4b` + `qwen3.6:27b-think` concurrently on one GPU pair so Tier1Slim's escalation tools don't evict the inner-loop model.
