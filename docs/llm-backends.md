---
title: Choose Your LLM Backend
description: Side-by-side comparison of LLM backend options for Dotty.
---

# Choose Your LLM Backend

Four LLM backend options, from simplest to most capable. All plug into
the same xiaozhi-server pipeline — you switch by changing `selected_module.LLM`
and the matching block under `LLM:` in `.config.yaml`.

## Comparison

| | OpenAI-compatible API | llama-swap (local, multi-model) | Tier1Slim (two-tier voice) | ZeroClaw (single-tier agent) |
|---|---|---|---|---|
| **Provider key** | `OpenAICompat` | `OpenAICompat` | `Tier1Slim` | `ZeroClawLLM` |
| **Runs where** | Cloud (OpenRouter, OpenAI, etc.) | Local GPU host (Docker, llama.cpp) | Inner loop on llama-swap; escalations through the bridge | ZeroClaw host or Docker host |
| **Latency** | 300-800 ms (network-bound) | 200-600 ms (GPU-bound; `qwen3.5:4b` warm <500 ms) | <500 ms plain chat; +bridge round-trip on tool calls | 500-1500 ms (full agent overhead on every turn) |
| **Cost** | Pay-per-token | Free (electricity + hardware) | Free for inner loop; pay-per-token in smart mode | Free (electricity + hardware) |
| **Privacy** | Tokens sent to cloud provider | Fully local, nothing leaves LAN | Fully local for plain turns; cloud only when smart_mode is on | Fully local (if local LLM backend) |
| **Setup complexity** | Low — API key + model name | Medium — GPU, Docker, GGUF download | Medium — llama-swap + Tier1Slim block; bridge for escalations | High — ZeroClaw install, bridge, systemd |
| **Memory / tools** | None | None | `memory_lookup` / `think_hard` / `take_photo` / `play_song` via escalation | Yes — persistent memory, 70+ tools, MCP |
| **Hot-swappable** | Restart container | Restart container | **Yes** — `set_runtime()` mutates the live provider; smart-mode flip is instant | No — daemon restart on model swap |
| **Best for** | Quick start, best-in-class models | Privacy + concurrent multi-model serving | Default for snappy voice; agent features only when needed | Always-on agentic features, deep tool use |

## 1. OpenAI-compatible API

The `OpenAICompat` provider works with any endpoint that speaks the OpenAI
`/v1/chat/completions` format: OpenAI, OpenRouter, LM Studio, vLLM, etc.

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: OpenAICompat

LLM:
  OpenAICompat:
    type: openai_compat
    url: https://openrouter.ai/api/v1      # or https://api.openai.com/v1
    api_key: sk-or-v1-xxxxxxxxxxxxxxxxxxxx
    model: qwen/qwen3-30b-a3b
    persona_file: personas/default.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

### Notes

- Swap `url` / `api_key` / `model` for any OpenAI-compatible service.
- `persona_file` is loaded as the system prompt.
- No memory between sessions — each request is stateless.

## 2. llama-swap (local, multi-model)

`OpenAICompat` provider pointed at a local llama-swap instance. llama-swap fronts upstream llama.cpp and routes per-model requests to per-alias `llama-server` children, with declarative co-residency (the `voice` matrix set keeps `qwen3.5:4b` and `qwen3.6:27b-think` both warm) and on-demand swap to other sets (e.g. `coding` for `qwen3.6:27b@96K`). Recommended local backend when you want to run more than one model at a time without paying repeated cold-load costs.

### Prerequisites

- NVIDIA GPU (dual RTX 3060 12 GB tested; single 3090 works too).
- NVIDIA Container Toolkit on the GPU host.
- GGUF model files downloaded into `/mnt/user/appdata/llama-models/` (or your equivalent path).

### Start

```bash
# Container: ghcr.io/mostlygeek/llama-swap:cuda
# Config:    /mnt/user/appdata/llama-swap/config.yaml
docker start llama-swap
curl http://<LLAMA_SWAP_HOST>:8080/health
```

See [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md) for the matrix-set config that pairs `qwen3.5:4b` (voice inner loop) with `qwen3.6:27b-think` (`think_hard` target).

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: OpenAICompat

LLM:
  OpenAICompat:
    type: openai_compat
    url: http://<LLAMA_SWAP_HOST>:8080/v1
    api_key: any-string                     # llama-swap ignores
    model: qwen3.5:4b
    persona_file: personas/dotty_voice.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

### Notes

- Larger models (27B Q4) need ~12 GB VRAM single-card or ~10/10 layer-split across two cards.
- Cold load on Q4_K_M 27B is ~20 s with upstream llama.cpp (was 70 s on Ollama; 2.15× generation speedup too).
- No memory between sessions — stateless like the cloud option.
- If you don't need concurrent multi-model serving, Ollama is the simpler single-binary alternative.

## 3. Tier1Slim (two-tier voice — current default)

The default in the shipped `.config.yaml`. A small, fast model (`qwen3.5:4b` against llama-swap) handles every plain conversational turn without involving the bridge. When the model emits a structured `tool_call`, the provider escalates to `POST /api/voice/escalate` and the bridge dispatches the tool (ZeroClaw memory for `memory_lookup`, `qwen3.6:27b-think` for `think_hard`, the VLM for `take_photo`, or `/xiaozhi/admin/play-asset` for `play_song`).

Smart-mode flips repoint the inner loop at a cloud model (default `anthropic/claude-sonnet-4-6`) via in-process `set_runtime()` — no docker restart and no daemon restart.

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: Tier1Slim

LLM:
  Tier1Slim:
    type: tier1_slim
    url: <LLAMA_SWAP_URL>                   # e.g. http://192.168.1.67:8080/v1
    api_key: <LLAMA_SWAP_KEY>               # any string; llama-swap ignores
    model: qwen3.5:4b
    persona_file: personas/dotty_voice.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

Plus environment variables (consumed by the bridge for smart-mode dispatch):

```
DOTTY_VOICE_PROVIDER=tier1slim
TIER1SLIM_CLOUD_API_KEY=sk-or-...           # required for OFF→ON smart-mode flip
```

Full reference: [tier1slim.md](./tier1slim.md).

### Notes

- The inner loop bypasses the bridge entirely on plain turns, so `bridge.py` going down doesn't break chitchat (only tool calls fail).
- `set_runtime()` lets the bridge hot-swap the live provider — used for smart-mode flips and would also support per-time-of-day model selection in future.
- Persona uses `personas/dotty_voice.md`; the top-level `prompt:` block is deliberately ignored because the 4 B chat template only honours one system message.

## 4. ZeroClaw (always-on single-tier agent)

The `ZeroClawLLM` provider routes through the FastAPI bridge on the ZeroClaw host into a long-running ZeroClaw agent process. ZeroClaw handles its own LLM calls (to OpenRouter, Ollama, or any supported provider), persistent memory, tool execution, and MCP integration. Every voice turn round-trips through ZeroClaw — heavier than Tier1Slim, but you get the full agent loop on every turn whether you need it or not.

### Prerequisites

- ZeroClaw installed on the ZeroClaw host (or another host): `cargo install zeroclaw`.
- `bridge.py` running as a systemd service (`zeroclaw-bridge.service`).
- Persona configured in `~/.zeroclaw/workspace/` (`SOUL.md`, `IDENTITY.md`, etc.).

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: ZeroClawLLM

LLM:
  ZeroClawLLM:
    type: zeroclaw
    url: http://<ZEROCLAW_HOST>:8080/api/message/stream
    channel: dotty
    timeout: 90
    system_prompt: |
      You are <ROBOT_NAME>, a desktop robot (StackChan body). Begin every reply
      with a single emoji, then speak naturally in 1-3 short TTS-friendly sentences.
```

### Notes

- Higher latency because ZeroClaw may invoke tools or consult memory before
  replying. The `timeout: 90` accommodates this.
- The bridge enforces an English + emoji sandwich around every turn to prevent
  Qwen3's Chinese-leak tendency (see [brain.md](./brain.md)).
- Persistent memory (SQLite-backed) means the robot remembers across sessions.
- Supports 70+ built-in tools plus any MCP servers you connect.
- Set `DOTTY_VOICE_PROVIDER=zeroclaw` (the default) so smart-mode flips know to rewrite ZeroClaw's `config.toml` rather than Tier1Slim's runtime.

## Switching backends

1. Edit `.config.yaml` — change `selected_module.LLM` and the relevant `LLM:` block.
2. If you're switching the smart-mode dispatch path, also set `DOTTY_VOICE_PROVIDER` (`tier1slim` or `zeroclaw`) in the bridge's systemd unit env block.
3. Restart xiaozhi-server: `docker compose restart xiaozhi-server`.
4. Test with a voice command or `curl` to the bridge endpoint.

All four `LLM:` blocks can coexist in the config; only the one named in `selected_module.LLM` is active.

## See also

- [tier1slim.md](./tier1slim.md) — the default voice path in detail.
- [brain.md](./brain.md) — model matrix and ZeroClaw architecture.
- [voice-pipeline.md](./voice-pipeline.md) — ASR, TTS, and VAD modules.
- [architecture.md](./architecture.md) — how the LLM slot fits into the full pipeline.
- [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md) — running multiple resident models on one GPU.

Last verified: 2026-05-17.
