---
title: Route Voice LLM Through OpenRouter (Cloud)
description: Send the LLM call to OpenRouter (or any OpenAI-compatible cloud API) while keeping ASR and TTS fully local. The cheapest, lowest-friction "fully self-hosted except the LLM" path — no GPU, no local model weights, no ZeroClaw native binary required.
---

# Route Voice LLM Through OpenRouter (Cloud)

ASR (FunASR) and TTS (Piper) are already local. Sending only the LLM call to
OpenRouter is the simplest path when you don't have a GPU and don't want to
run a local model — the rest of the stack stays on your hardware. Compare with
[run-fully-local.md](./run-fully-local.md) (Ollama, requires NVIDIA GPU) and
[llama-swap-concurrent-models.md](./llama-swap-concurrent-models.md) (two-tier
local voice path).

This recipe is the deployment shape used by the Raspberry Pi 5 plan at
[`docs/plans/2026-05-18-001-feat-pi5-openrouter-deployment-plan.md`](../plans/2026-05-18-001-feat-pi5-openrouter-deployment-plan.md)
— but it works on any single Docker host (Pi 5, NUC, mini-PC, laptop, Synology).

## When to use this

Pick OpenRouter (or any OpenAI-compatible API: OpenAI direct, Anthropic via
their OpenAI shim, etc.) when:

- You don't have an NVIDIA GPU on the Docker host
- You don't want to manage local model weights / serve them via Ollama or llama-swap
- You're OK with the LLM call leaving your LAN (it's the only thing that does — ASR and TTS stay local)
- You want a one-line model swap to experiment with different LLMs without redeploying

Pick a different path when:

- You need fully-local LLM inference (privacy, offline, no cloud spend) → see
  [run-fully-local.md](./run-fully-local.md) or
  [llama-swap-concurrent-models.md](./llama-swap-concurrent-models.md)
- You want the project's default two-tier path (sub-second chitchat + bigger
  model for tool calls) → see [llama-swap-concurrent-models.md](./llama-swap-concurrent-models.md)
- You're routing through ZeroClaw for agent memory / tool calls → see
  [brain.md](../brain.md)

## Prerequisites

- A working Docker host with `compose.all-in-one.yml` runnable (see [quickstart.md](../quickstart.md))
- The standard model weights downloaded: `make fetch-models`
- An OpenRouter API key from <https://openrouter.ai/keys> (or an OpenAI / OpenAI-compatible key — the provider works with any `/v1/chat/completions` endpoint)
- A `.env` file at the repo root with:

  ```text
  OPENROUTER_API_KEY=sk-or-v1-...your-key-here...
  ```

- The zeroclaw bind-mounts that `compose.all-in-one.yml` declares must be satisfiable on the host. If you don't want to install ZeroClaw natively, a no-op stub script is enough — see the prerequisites section in [`compose.openrouter.override.yml`](../../compose.openrouter.override.yml) for the exact two-command stub.

## Steps

### 1. Layer the override compose file on top of `compose.all-in-one.yml`

This repo ships [`compose.openrouter.override.yml`](../../compose.openrouter.override.yml) at the root. It adds the volume mounts and env vars the all-in-one compose doesn't (the `openai_compat` LLM provider, the project-custom `textUtils.py`, the `personas/` dir, the four `xiaozhi-patches` files that deliver the `/xiaozhi/admin/*` routes and perception event relay, and the `DOTTY_VOICE_PROVIDER=tier1slim` / `XIAOZHI_HOST` env vars).

No edits to `compose.all-in-one.yml` itself — it's the upstream-merge surface.

### 2. Configure `data/.config.yaml` to select OpenAICompat

Copy the template and edit:

```bash
cp .config.yaml.template data/.config.yaml
```

Three edits to make:

1. **Replace `<XIAOZHI_HOST>`** on line 5 (the `websocket:` URL) with the host's LAN IP. This URL is served back to the StackChan via the OTA response — leaving the literal placeholder breaks the WebSocket handshake.

2. **Switch the selected LLM** to `OpenAICompat`:

   ```yaml
   selected_module:
     LLM: OpenAICompat
   ```

3. **Fill in the `LLM.OpenAICompat` block** (around line 133 of the template):

   ```yaml
   LLM:
     OpenAICompat:
       url: https://openrouter.ai/api/v1
       api_key: ${OPENROUTER_API_KEY}    # interpolated from container env
       model: openrouter/free            # see "Choosing a model" below
       persona_file: personas/default.md
       max_tokens: 256
       temperature: 0.7
       timeout: 60
   ```

   The `${OPENROUTER_API_KEY}` interpolation reads the env var the override compose injects into the container. Don't paste the literal key into this file — it's in an active git working tree and `.gitignore` is the only thing keeping it out of commits.

Replace `<ROBOT_NAME>` in the top-level `prompt:` block with whatever you call the robot (default: `Dotty`).

### 3. Bring up the stack

```bash
docker compose --env-file .env \
  -f compose.all-in-one.yml \
  -f compose.openrouter.override.yml \
  up -d
```

Watch logs for the OpenAICompat provider initializing:

```bash
docker compose logs xiaozhi-server | grep -i openai
```

You should see the provider register with the OpenRouter base URL.

### 4. Smoke-test from the host

```bash
curl -s http://localhost:8003/xiaozhi/ota/
# expect: OTA banner string with the WebSocket URL

curl -s http://localhost:8080/health | jq .
# expect: {"status":"ok","service":"zeroclaw-bridge","acp_running": <bool>}
# acp_running may be false (stub ZeroClaw never responds) — that's expected
# and not a problem; the LLM path bypasses the bridge entirely.
```

### 5. Point the StackChan at this host's OTA URL and converse

OTA URL: `http://<this-host's-LAN-IP>:8003/xiaozhi/ota/` — set this in the firmware's `sdkconfig.defaults` before building (see [SETUP.md](../../SETUP.md) for the full firmware build + flash flow), or via the device's M5 BURNER tool after flash.

## Choosing a model

`openrouter/free` is OpenRouter's auto-router for the free-model pool. **$0 per request**, 200k token context, smart feature-filtering (the router picks free models that support whatever your request needs — tool calls, structured outputs, vision). Trade-offs:

- **~200 requests/day + ~20 requests/minute rate limits.** Fine for prototyping and casual use; tight for active daily use with a kid burning turns in a 1-hour play session (plus perception events firing additional turns for face-detected greetings and sound-direction responses).
- **Random model selection** — different free models follow the emoji-prefix protocol with different reliability. The bridge's `_ensure_emoji_prefix` fallback covers it (defaults to 😐), but expect less expressive face animation on some routes.
- **Variable safety adherence** — for a kid-facing system this matters. The pool composition changes without notice, and uncensored / abliterated community models can show up. The bridge's kid-mode turn-suffix is a defense layer but not a guarantee. Consider this when picking a default; pin to a specific known-safe model if reliability matters more than experiment-friendliness.

**Upgrade paths** (each is a one-line `model:` change in `data/.config.yaml` + `docker compose restart xiaozhi-server` — no rebuild):

| Model ID | Notes |
|---|---|
| `anthropic/claude-haiku-4.5` | Recommended for daily-driven kid-facing systems. Fast (~500ms-1s LLM latency), best emoji-prefix adherence of the cheap models, safety-trained. ~$1/M input, ~$5/M output. |
| `openai/gpt-4o-mini` | Cheapest of the major brand models. Reliable but less consistent on the emoji protocol — bridge fallback covers it but you'll see more 🟡 neutral faces. |
| `anthropic/claude-sonnet-4-6` | Highest quality if cost isn't a constraint. ~5× more than Haiku. Overkill for a desktop robot in most cases. |
| `meta-llama/llama-3.1-70b-instruct` | Solid open-weight pick on OpenRouter at ~$0.30/M. Good emoji adherence. |

## Switching to a different OpenAI-compatible provider

The provider name is `openai_compat` — it works with anything that speaks `/v1/chat/completions`. To point at OpenAI directly instead of OpenRouter:

```yaml
LLM:
  OpenAICompat:
    url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}
    model: gpt-4o-mini
```

Or Anthropic's OpenAI-compatible shim, Together, Groq, Fireworks, vLLM, or a self-hosted Ollama (`http://ollama:11434/v1` inside the docker network — see [run-fully-local.md](./run-fully-local.md)). Same compose override; only the URL + model change.

## What this override does NOT enable

- **Voice tools** (`memory_lookup`, `take_photo`, `think_hard`, `play_song`). The `openai_compat` provider doesn't define tools in the Chat Completions request, so the LLM can never emit tool calls; the bridge's tool handlers stay dormant. Adding them later means either extending `openai_compat.py` with tool definitions OR migrating to Tier1Slim with a real llama-swap endpoint.
- **Smart-mode model flip.** Smart mode switches between two configured models via `/xiaozhi/admin/set-tier1slim-model`, which only works under the Tier1Slim provider. Under OpenAICompat, smart-mode requests are no-ops. Acceptable for V1; revisit if you want the smart-mode toggle to actually swap models.
- **ZeroClaw agent memory.** ZeroClaw is bypassed entirely; the bridge's `_voice_tool_*` ZeroClaw paths are unreachable from this LLM provider. The bridge's own `brain.db` SQLite memory is still active for perception state and identified-face tracking — that's local, not ZeroClaw.

See [llm-backends.md](../llm-backends.md) for the full comparison matrix.

## See also

- [`compose.openrouter.override.yml`](../../compose.openrouter.override.yml) — the override compose file this recipe layers on
- [`docs/plans/2026-05-18-001-feat-pi5-openrouter-deployment-plan.md`](../plans/2026-05-18-001-feat-pi5-openrouter-deployment-plan.md) — the Pi 5 deployment plan that uses this recipe
- [`SETUP.md`](../../SETUP.md) — firmware build + flash + WiFi onboarding
- [`docs/llm-backends.md`](../llm-backends.md) — comparison of all LLM backend choices
- [`docs/voice-pipeline.md`](../voice-pipeline.md) — what the pipeline does around the LLM call
