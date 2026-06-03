---
title: Brain
description: The pi agent runtime (dotty-pi container), the model matrix, and the admin dashboard service (bridge.py).
---

# Brain — the pi agent + the model matrix

## TL;DR

- The "brain" is the **`dotty-pi` Docker container** running the pi coding agent with the `dotty-pi-ext` extension.
- **`PiVoiceLLM`** (the default xiaozhi LLM provider) translates each voice turn into a pi RPC request via `docker exec -i dotty-pi pi --mode rpc`. TTS-bound text streams back to xiaozhi-server; tool dispatch happens entirely inside the container.
- The `dotty-pi-ext` extension exposes **five voice tools** to the agent loop: `memory_lookup`, `remember`, `think_hard`, `take_photo`, `play_song`.
- **Which LLM runs which turn:** the pi outer loop targets `qwen3.5:4b` (local llama-swap, ~500 ms warm); `think_hard` escalates directly to `qwen3.6:27b-think` (co-resident on llama-swap). **Smart-mode does NOT swap the backend model on the live `PiVoiceLLM` path** — it flips ambient/behaviour only; the inner-loop model-swap is v2 scope and not wired. (Instant in-process model-swap existed only on the now-removed `Tier1Slim` provider.)
- One documented alternate voice provider exists: **`OpenAICompat`** (points straight at any OpenAI-compatible endpoint; stateless, no voice tools). See [llm-backends.md](./llm-backends.md).

> **Cutover note (2026-05-19, issue #36):** The brain previously ran as the ZeroClaw Rust agent on a Raspberry Pi, fronted by a FastAPI bridge (`bridge.py`) under systemd. ZeroClaw and the RPi host are retired. `bridge.py` survives as the admin dashboard service (port 8081, `/ui`) on the Docker host; its voice and perception roles moved to `PiVoiceLLM`/`dotty-pi` and `dotty-behaviour` respectively.

## Model matrix

| Path | Model | Where | When called |
|---|---|---|---|
| PiVoiceLLM outer agent loop | `qwen3.5:4b` | local llama-swap | Every voice turn. ~500 ms warm. |
| pi tool: `think_hard` | `qwen3.6:27b-think` | local llama-swap | Multi-step reasoning; direct POST from dotty-pi-ext, no agent overhead. |
| pi tool: `memory_lookup` | (no LLM call — FTS5) | brain.db inside dotty-pi | `"do you remember…"` queries. |
| pi tool: `take_photo` | `google/gemini-2.0-flash-001` (`VLM_MODEL`) | dotty-behaviour → OpenRouter | Camera describe. |
| pi tool: `play_song` | (no LLM call) | Firmware via `/xiaozhi/admin/play-asset` | Song request. |
| Smart-mode inner loop (`SMART_MODEL`) | `anthropic/claude-sonnet-4-6` | OpenRouter | **Not wired on the live `PiVoiceLLM` path — v2 scope.** Smart-mode flips ambient/behaviour only; it does NOT swap the inner-loop model today. (`SMART_MODEL` is still consumed for dashboard-adjacent calls.) |
| Vision narrative (security/scene synthesis) | `VISION_MODEL` (`google/gemini-2.0-flash-001`) | OpenRouter | dotty-behaviour internal — camera frame description. |
| Audio captioning (security mode) | `AUDIO_CAPTION_MODEL` (`google/gemini-2.5-flash`) | OpenRouter | dotty-behaviour internal — ambient sound description. |

## The pi agent runtime

### dotty-pi container

`dotty-pi` is a pinned `node:25.9-alpine3.23` image with `@earendil-works/pi-coding-agent` installed globally. It idles via `sleep infinity`; voice turns invoke pi on demand via `docker exec -i` from `PiClient` (in `custom-providers/pi_voice/pi_client.py`).

The runtime contract:
1. **xiaozhi-server** calls `PiVoiceLLM.generate()` with the dialogue.
2. **PiClient** runs `docker exec -i dotty-pi pi --mode rpc` — JSONL messages over stdin/stdout.
3. **pi** runs the prompt against llama-swap (`qwen3.5:4b` by default) with the `dotty-pi-ext` extension loaded.
4. Thinking deltas and extension UI requests are filtered by PiClient; only TTS-bound text chunks reach xiaozhi-server.
5. `PiVoiceLLM` holds one long-lived `PiClient`; between turns it issues `new_session` to reset pi's working state without re-spawning the process.

Appdata layout on the Docker host:

```
/mnt/user/appdata/dotty-pi/
├── agent/
│   └── models.json          # provider config (llama-swap endpoints + aliases)
├── sessions/                # pi session state
├── persona/                 # Dotty persona files
├── memory/
│   └── brain.db             # FTS5 full-text store
└── extensions/
    └── dotty-pi-ext/        # voice-tool extension
```

### dotty-pi-ext — the five voice tools

`dotty-pi-ext` is the pi extension that exposes Dotty's voice tools to the agent loop. Installed inside the container at `/root/.pi/extensions/dotty-pi-ext/`.

| Tool | What it does |
|---|---|
| `memory_lookup(query)` | FTS5 search against `brain.db`; returns top-3 snippets, ≤200 chars each. |
| `remember(fact)` | Stores a durable fact (≤300 codepoints) into `brain.db` with `category=core`, `importance=0.7`. |
| `think_hard(question)` | Direct POST to llama-swap `qwen3.6:27b-think` (`enable_thinking=false`, 200-token cap, terse answer). |
| `take_photo()` | GET to `dotty-behaviour /api/voice/take_photo` — returns latest cached vision description if ≤30 s old. |
| `play_song(name)` | Resolves free-form name against `/xiaozhi/admin/songs` catalogue (60 s cache), then POSTs `/xiaozhi/admin/play-asset`. |

In addition, an `agent_end` handler in the extension automatically writes a `category=conversation` row to `brain.db` after every completed user prompt — the agent does not decide to log; every successful turn is recorded.

### Model selection for dotty-pi

The outer pi loop must target `qwen3.5:4b` — **not** `qwen3.6:27b`. llama-swap groups models into matrix sets: `voice` (`qwen3.5:4b` + `qwen3.6:27b-think`) and `coding` (`qwen3.6:27b` alone). Requesting the coding-set model evicts both voice models; reloading either voice model is a 30–50 s cold hit. `think_hard` calls `qwen3.6:27b-think` directly from the extension, which is in the `voice` set and resident alongside `4b`. See [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md).

## The bridge — `bridge.py` (dashboard service)

`bridge.py` was the original HTTP→ZeroClaw translator, running under systemd on the RPi. Post-cutover (#36) it runs as a Docker container on the same Docker host, port 8081. Its **voice path** (`/api/message`, `/api/voice/*`) and **perception relay** (`/api/perception/event`) roles are retired — those functions moved to `PiVoiceLLM`/`dotty-pi` and `dotty-behaviour`. What remains:

- **Admin dashboard** (`/ui`) — the operator web UI for monitoring turns, toggling kid-mode/smart-mode, viewing scene context, and LED state.
- **`/admin/*` endpoints** (localhost-only) — runtime toggles for kid-mode, smart-mode, safety allowlist.

A dashboard port to `dotty-behaviour` is still pending; until then, bridge.py's dashboard panels that relied on the bridge's own perception bus may show stale or empty data.

See [protocols.md](./protocols.md) for the admin endpoint wire formats.

## The LLMs

### Qwen3-30B-A3B-Instruct-2507 (legacy path — retired)

Previously used by the ZeroClawLLM provider via OpenRouter. Not used in the current architecture.

### Qwen3 caveat — Chinese leak and long-context drift

Qwen3 is multilingual by training and occasionally **leaks Chinese mid-response** when context is long or system-prompt adherence is weakened by MoE expert routing. Observed symptom: the model starts a response in English and drops a Chinese character or phrase partway through; `en-*` EdgeTTS voices return silent audio on non-English input, making it sound like a dead mic.

**Mitigation in the current stack:**

1. The pi agent persona (`persona/dotty_voice.md`) has English hard rules.
2. xiaozhi-server's top-level `prompt:` in `data/.config.yaml` is also English-only.
3. `custom-providers/textUtils.py` appends a per-turn English-only suffix (used by PiVoiceLLM).

### qwen3.5:4b (pi outer agent loop)

Local on llama-swap (dual RTX 3060). Fast: ~500 ms warm round-trip including TTS dispatch. Trained for tool calling, which is what lets the five-tool catalogue work reliably at 4 B parameters. See the dotty-pi-ext tool table above.

### qwen3.6:27b-think (think_hard target)

Local on the same llama-swap, separate alias. ~18 tok/s generation, ~30–50 s cold-load. Co-resident with `qwen3.5:4b` under the `voice` matrix set in `llama-swap/config.yaml` so an escalation doesn't evict the inner loop. See [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md).

### Cloud models (smart_mode + visual + audio)

- **Smart-mode inner loop:** `anthropic/claude-sonnet-4-6` (`SMART_MODEL` env var). **Not wired on the live `PiVoiceLLM` path** — the inner-loop model-swap is v2 scope; smart-mode currently flips ambient/behaviour only. The env var is still read for dashboard-adjacent calls.
- **VLM (`take_photo`, security camera frames):** `google/gemini-2.0-flash-001` (`VLM_MODEL`). Served by dotty-behaviour.
- **Audio captioning (security mode):** `google/gemini-2.5-flash` (`AUDIO_CAPTION_MODEL`). Served by dotty-behaviour.

## OpenRouter

Routing: OpenRouter fronts cloud models (`SMART_MODEL`, `VLM_MODEL`, `AUDIO_CAPTION_MODEL`). It handles multiple upstream providers and exposes an OpenAI-compatible API. The bridge reads `OPENROUTER_API_KEY` from its container environment for dashboard-adjacent calls; dotty-behaviour reads its own copy for vision and audio-caption calls.

Observability OpenRouter itself offers (not currently surfaced in this stack):
- Per-request latency + cost dashboards.
- Multi-model A/B routing.
- Per-provider failover for the same model.

## See also

- [voice-pipeline.md](./voice-pipeline.md) — what xiaozhi-server runs.
- [architecture.md](./architecture.md) — full topology and data-flow diagrams.
- [protocols.md](./protocols.md) — pi RPC mode wire format, admin endpoints.
- [llm-backends.md](./llm-backends.md) — choosing between PiVoiceLLM and OpenAICompat.
- [latent-capabilities.md](./latent-capabilities.md) — streaming, session reuse, tool-use, MCP-server mode.
- [references.md](./references.md) — Qwen3, OpenRouter, pi coding agent links.
- [cutover-behaviour.md](./cutover-behaviour.md) — historical runbook for the #36 ZeroClaw → pi-agent cutover.

Last verified: 2026-05-22.
