---
title: Run Fully Local
description: Run the entire stack with zero cloud dependencies using Ollama.
---

# Run Fully Local

ASR and TTS are already local. Adding Ollama closes the last cloud
dependency (the LLM call).

This is the **simple single-binary path** — pick Ollama when you only
need one model resident at a time and want the easiest setup. If you
need to run multiple models concurrently (e.g. the Tier1Slim two-tier
default where `qwen3.5:4b` and `qwen3.6:27b-think` share VRAM), use
[llama-swap](./llama-swap-concurrent-models.md) instead — it solves the
multi-model serving problem Ollama doesn't.

## When to use which compose file

- `compose.all-in-one.yml` — single-host bundle that runs xiaozhi-server,
  the bridge, and (with the override) Ollama on the same machine. Good for
  laptops, single-host home servers, and demos.
- `compose.local.override.yml` — applied on top of either compose file to
  add the Ollama container with NVIDIA GPU passthrough.

## Prerequisites

- NVIDIA GPU (8B model needs ~5 GB VRAM, 30B needs ~18 GB).
- NVIDIA Container Toolkit installed on the Docker host.

## Steps

1. Start the stack with the local override:

```bash
docker compose -f compose.all-in-one.yml -f compose.local.override.yml up -d
```

2. Pull a model: `docker exec ollama ollama pull qwen3:8b`

3. Update `.config.yaml`:

```yaml
selected_module:
  LLM: OpenAICompat
LLM:
  OpenAICompat:
    url: http://ollama:11434/v1    # container-to-container DNS
    api_key: unused                # Ollama ignores this
    model: qwen3:8b
    persona_file: personas/default.md
```

4. Restart: `docker compose restart xiaozhi-server`

Now ASR (FunASR), LLM (Ollama), and TTS (Piper) are all local.
No API keys or internet required after model download.

See [llm-backends.md](../llm-backends.md) for a full comparison of options.
