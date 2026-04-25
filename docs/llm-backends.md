---
title: Choose Your LLM Backend
description: Side-by-side comparison of LLM backend options for Dotty.
---

# Choose Your LLM Backend

Three LLM backend options, from simplest to most capable. All plug into
the same xiaozhi-server pipeline — you switch by changing `selected_module.LLM`
and the matching block under `LLM:` in `.config.yaml`.

## Comparison

| | OpenAI-compatible API | Ollama (local) | ZeroClaw |
|---|---|---|---|
| **Provider key** | `OpenAICompat` | `OpenAICompat` | `ZeroClawLLM` |
| **Runs where** | Cloud (OpenRouter, OpenAI, etc.) | Local GPU on Docker host | RPi or Docker host |
| **Latency** | 300-800 ms (network-bound) | 200-600 ms (GPU-bound) | 500-1500 ms (agent overhead) |
| **Cost** | Pay-per-token | Free (electricity + hardware) | Free (electricity + hardware) |
| **Privacy** | Tokens sent to cloud provider | Fully local, nothing leaves LAN | Fully local (if Ollama backend) |
| **Setup complexity** | Low — API key + model name | Medium — GPU, Nvidia toolkit, model pull | High — ZeroClaw install, bridge, systemd |
| **Memory / tools** | None | None | Yes — persistent memory, 70+ tools, MCP |
| **Best for** | Quick start, best-in-class models | Privacy, offline use, no recurring cost | Agentic features, tool use, long-term memory |

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

## 2. Ollama (local)

Same `OpenAICompat` provider pointed at a local Ollama instance. Use the
included `compose.local.override.yml` to add an Ollama container alongside
xiaozhi-server.

### Prerequisites

- NVIDIA GPU with enough VRAM (RTX 3060 12 GB+ recommended).
- NVIDIA Container Toolkit installed on the Docker host.

### Start the stack

```bash
docker compose -f compose.all-in-one.yml -f compose.local.override.yml up -d
docker exec ollama ollama pull qwen3:8b
```

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: OpenAICompat

LLM:
  OpenAICompat:
    type: openai_compat
    url: http://ollama:11434/v1             # container-to-container DNS
    api_key: unused                         # Ollama ignores this field
    model: qwen3:8b
    persona_file: personas/default.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

### Notes

- `url` uses the Docker service name `ollama` (not `localhost`) because
  xiaozhi-server and Ollama share the `dotty` bridge network.
- Larger models (30B MoE) need ~18 GB VRAM; 8B fits in ~5 GB.
- No memory between sessions — stateless like the cloud option.

## 3. ZeroClaw (advanced)

The `ZeroClawLLM` provider routes through the FastAPI bridge on the RPi
into a long-running ZeroClaw agent process. ZeroClaw handles its own LLM
calls (to OpenRouter, Ollama, or any supported provider), persistent memory,
tool execution, and MCP integration.

### Prerequisites

- ZeroClaw installed on the RPi (or another host): `cargo install zeroclaw`.
- `bridge.py` running as a systemd service (`zeroclaw-bridge.service`).
- Persona configured in `~/.zeroclaw/workspace/` (`SOUL.md`, `IDENTITY.md`, etc.).

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: ZeroClawLLM

LLM:
  ZeroClawLLM:
    type: zeroclaw
    url: http://<RPI_IP>:8080/api/message/stream
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
## Switching backends

1. Edit `.config.yaml` — change `selected_module.LLM` and the relevant `LLM:` block.
2. Restart xiaozhi-server: `docker compose restart xiaozhi-server`.
3. Test with a voice command or curl to the bridge endpoint.

All three `LLM:` blocks can coexist in the config; only the one named in
`selected_module.LLM` is active.

## See also

- [brain.md](./brain.md) — ZeroClaw architecture and the bridge in detail.
- [voice-pipeline.md](./voice-pipeline.md) — ASR, TTS, and VAD modules.
- [architecture.md](./architecture.md) — how the LLM slot fits into the full pipeline.

Last verified: 2026-04-25.
