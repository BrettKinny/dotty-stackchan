---
title: Choose Your LLM Backend
description: Side-by-side comparison of LLM backend options for Dotty — PiVoiceLLM (default), OpenAICompat, and llama-swap.
---

# Choose Your LLM Backend

Three LLM backend options, from simplest to most capable. All plug into
the same xiaozhi-server pipeline — you switch by changing `selected_module.LLM`
and the matching block under `LLM:` in `.config.yaml`.

## Comparison

| | OpenAI-compatible API | llama-swap (local, multi-model) | PiVoiceLLM (pi agent — default) |
|---|---|---|---|
| **Provider key** | `OpenAICompat` | `OpenAICompat` | `PiVoiceLLM` |
| **Runs where** | Cloud (OpenRouter, OpenAI, …) or any OpenAI-compatible server on your LAN (LM Studio, Ollama) | Local GPU host (Docker, llama.cpp) | dotty-pi container on the Docker host |
| **Latency** | 300-800 ms (network-bound) | 200-600 ms (GPU-bound; `qwen3.5:4b` warm <500 ms) | 500-1500 ms (pi agent turn overhead) |
| **Cost** | Pay-per-token | Free (electricity + hardware) | Free (electricity + hardware) |
| **Privacy** | Tokens sent to cloud provider — or stay on the LAN if the endpoint is local | Fully local, nothing leaves LAN | Fully local |
| **Setup complexity** | Low — API key + model name | Medium — GPU, Docker, GGUF download | Medium — dotty-pi container + llama-swap |
| **Memory / tools** | None | None | Yes — memory_lookup, recall_person, remember, remember_person, think_hard, take_photo, play_song |
| **Hot-swappable** | Restart container | Restart container | Restart container |
| **Best for** | Quick start, best-in-class models | Privacy + concurrent multi-model serving | **Default — snappy voice with full tool support** |

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

### LM Studio (or any inference box on another machine)

A common setup: xiaozhi-server runs on a headless Linux Docker host, and the
GPU lives in a separate desktop — often a Windows PC running
[LM Studio](https://lmstudio.ai/). LM Studio's local server speaks the same
Chat Completions wire format, so `OpenAICompat` talks to it unchanged.

```yaml
selected_module:
  LLM: OpenAICompat

LLM:
  OpenAICompat:
    type: openai_compat
    url: http://192.168.1.50:1234/v1     # the GPU box's LAN IP, not localhost
    api_key: lm-studio                    # any non-empty string — LM Studio ignores it
    model: qwen2.5-7b-instruct            # must match LM Studio's model id
    persona_file: personas/default.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

Three things account for most "it won't connect" reports:

- **Use the GPU box's LAN IP, never `localhost` / `127.0.0.1`.** This config is
  read from inside a container on a different machine, so loopback resolves to
  the xiaozhi-server container itself. (Same reason `VISION_BRIDGE_URL` in the
  compose files wants a LAN IP.)
- **Enable "Serve on Local Network"** in LM Studio's Developer/Server tab. The
  default binding is localhost-only, so nothing outside that PC can reach it.
- **Open the port in the host firewall.** On Windows, inbound TCP 1234 is
  blocked until you allow it — this is the single most common failure.

Verify from the Docker host *before* touching `.config.yaml`:

```bash
curl http://192.168.1.50:1234/v1/models
```

That confirms reachability and prints the exact model `id` string to put in the
`model:` field — it must match what the server reports, not the display name in
the LM Studio UI. `make doctor` does not validate this value, so a typo here
surfaces at runtime as "My brain returned an error" from the robot.

The same pattern applies to Ollama (`http://<GPU_BOX_IP>:11434/v1`, and set
`OLLAMA_HOST=0.0.0.0` so it listens beyond loopback) or any other
OpenAI-compatible server on a second machine.

#### You do not need a GPU on the Docker host for this

The CUDA block in `docker-compose.yml` is for **WhisperLocal ASR only** — it
has nothing to do with the LLM. `make setup` detects whether the NVIDIA Docker
runtime is present and, if it isn't, drops that block and selects FunASR on CPU
instead. A CPU-only Docker host paired with a remote LM Studio box is a fully
supported configuration.

`compose.local.override.yml` (which does require an NVIDIA GPU and the NVIDIA
Container Toolkit on the Docker host) is a *different* option — it runs Ollama
in a container on the Docker host itself. If your models live on another
machine, ignore that file entirely.

#### Dropping the dotty-pi dependency

Switching away from `PiVoiceLLM` means the `dotty-pi` container is no longer on
the voice path, so xiaozhi-server no longer needs to `docker exec` into it. Once
`selected_module.LLM` is `OpenAICompat`, comment out both of these mounts in your
compose file — the socket mount grants the container effective root on the
Docker host, and there's no reason to keep it once nothing uses it:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
- /usr/bin/docker:/usr/bin/docker:ro
```

The trade-off is the one in the comparison table above: `OpenAICompat` is
stateless, so you lose persistent memory and all seven voice tools.

### Anthropic API directly (without OpenRouter)

Anthropic ships an OpenAI-SDK-compatible shim at `https://api.anthropic.com/v1/`
that maps Chat Completions calls onto the underlying Messages API. The
`OpenAICompat` provider works against it out of the box:

```yaml
selected_module:
  LLM: OpenAICompat

LLM:
  OpenAICompat:
    type: openai_compat
    url: https://api.anthropic.com/v1
    api_key: sk-ant-api03-xxxxxxxxxxxxxxxxxxxx     # Anthropic console key
    model: claude-haiku-4-5                         # or claude-sonnet-4-6, etc.
    persona_file: personas/default.md
    max_tokens: 256
    temperature: 0.7
    timeout: 60
```

Caveats when running Anthropic-only (no OpenRouter):

- **Vision intents** (`take_photo`) rely on the VLM call path. Point the
  bridge's VLM env vars (`VLM_API_KEY`, `VLM_MODEL`, `VLM_URL`) at your
  Anthropic key and endpoint to keep vision working without OpenRouter.
- The compat shim doesn't support every OpenAI option (streaming and tools
  work; `logprobs`, `seed`, etc. don't).

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

## 3. PiVoiceLLM (pi agent — default)

The default in the shipped `.config.yaml`. The `PiVoiceLLM` provider routes each voice turn to the **dotty-pi container** — the pi coding agent running on the same Docker host as xiaozhi-server.

`PiClient` drives the agent by running `docker exec -i dotty-pi pi --mode rpc … --no-context-files` and exchanging JSONL messages over its stdin/stdout. The agent's outer loop uses `qwen3.5:4b` on local llama-swap for fast chitchat and loads the **dotty-pi-ext extension**, which exposes seven voice-focused tools:

| Tool | Purpose |
|---|---|
| `memory_lookup` | Recall a fact from past conversations (FTS on brain.db) |
| `recall_person` | Recall approved facts about a named household member |
| `remember` | Stash a new fact into brain.db |
| `remember_person` | Store a fact about a named household member |
| `think_hard` | Escalate a hard question to `qwen3.6:27b-think` |
| `take_photo` | Describe what Dotty's camera sees via a VLM |
| `play_song` | Play a song through the speaker |

Only TTS-bound text streams back to xiaozhi-server — tool results stay internal to the agent loop.
PiVoiceLLM does not use `persona_file` or forward xiaozhi's configured system dialogue; its live policy (`build_turn_suffix()` plus the voice tool-routing rules) goes into pi's system prompt when the provider spawns pi, and pi is respawned when Kid Mode changes. (`OpenAICompat`, by contrast, still appends `build_turn_suffix()` to the last user message.)

### Prerequisites

- dotty-pi container running on the Docker host.
- llama-swap running and reachable by the dotty-pi container (`qwen3.5:4b` for the outer loop; `qwen3.6:27b-think` for `think_hard`).

### `.config.yaml` snippet

```yaml
selected_module:
  LLM: PiVoiceLLM

LLM:
  PiVoiceLLM:
    type: pi_voice
    container_name: dotty-pi
```

### Notes

- Higher latency than a raw llama-swap call because the pi agent loop adds overhead — tool-using turns are slower than plain chitchat.
- Persistent memory (`brain.db`, FTS5) means the robot remembers across sessions.
- All four server-side services (xiaozhi-server, dotty-pi, dotty-behaviour, bridge.py) run as Docker containers on the same host — no separate "brain host" required.

## Switching backends

1. Edit `.config.yaml` — change `selected_module.LLM` and the relevant `LLM:` block.
2. Restart xiaozhi-server: `docker compose restart xiaozhi-server`.
3. Test with a voice command or a `curl` to the health endpoint.

All `LLM:` blocks can coexist in the config; only the one named in `selected_module.LLM` is active.

## See also

- [brain.md](./brain.md) — model matrix and dotty-pi agent architecture.
- [voice-pipeline.md](./voice-pipeline.md) — ASR, TTS, and VAD modules.
- [architecture.md](./architecture.md) — how the LLM slot fits into the full pipeline.
- [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md) — running multiple resident models on one GPU.

Last verified: 2026-05-22.
