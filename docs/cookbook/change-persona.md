---
title: Change Persona
description: Swap Dotty's personality by editing the persona prompt or pointing the LLM provider at a different persona file.
---

# Change Persona

Dotty's personality comes from a persona file loaded as the LLM system prompt. **Where** that file lives depends on which LLM provider is active.

Three personas ship in `personas/`:

| File | Style | Used by |
|---|---|---|
| `default.md` | Cheerful, curious desktop robot. The general-purpose persona used by `ZeroClawLLM` and other generic providers. | `ZeroClawLLM`, `OpenAICompat` |
| `dotty_voice.md` | Voice-tuned variant of `default.md` — same character but pruned for short replies, with the 4-tool catalogue and `[REMEMBER: ...]` markers baked in. | `Tier1Slim` |
| `smart.md` | More capable, allowed longer answers — for when `smart_mode` is on and the cloud model is doing the heavy lifting. | optional override for either provider |

## Which file controls the persona?

Check `selected_module.LLM` in `.config.yaml`, then read the matching block:

| Provider | Persona source |
|---|---|
| `Tier1Slim` (current default) | `LLM.Tier1Slim.persona_file` in `.config.yaml`. Defaults to `personas/dotty_voice.md`. |
| `ZeroClawLLM` | ZeroClaw's own workspace files: `~/.zeroclaw/workspace/{SOUL,IDENTITY,MEMORY,AGENTS}.md` on the ZeroClaw host. **The `persona_file` key is ignored** — ZeroClaw doesn't read it. |
| `OpenAICompat` (and similar generic providers) | `LLM.OpenAICompat.persona_file` in `.config.yaml`. |

Setting `persona_file` for a `ZeroClawLLM` selection is a silent no-op — edit the workspace files instead, or switch the provider first.

## Switch to a different shipped persona (Tier1Slim or generic providers)

1. Edit `.config.yaml`:

   ```yaml
   LLM:
     Tier1Slim:
       persona_file: personas/smart.md   # was personas/dotty_voice.md
   ```

2. Restart: `docker compose restart xiaozhi-server`.

## Edit ZeroClaw's persona

1. SSH to the ZeroClaw host.
2. Edit the relevant file under `~/.zeroclaw/workspace/`:
   - `SOUL.md` — voice, values, and role in the household.
   - `IDENTITY.md` — name, backstory, family context.
   - `USER.md` — per-user preferences (optional).
   - `MEMORY.md` — long-term facts; the agent also writes to this.
3. No restart needed — ZeroClaw hot-reads these on each turn.

## Create your own persona

1. Copy an existing file: `cp personas/dotty_voice.md personas/pirate.md`.
2. Edit the new file. **Keep the emoji instruction line** — the firmware needs it to animate the face. See [emoji-mapping.md](../emoji-mapping.md) for the allowlist (😊😆😢😮🤔😠😐😍😴).
3. Point the active provider's `persona_file` at the new file in `.config.yaml`, then restart.

## Quick inline edit (no file swap)

Edit the top-level `prompt:` block in `.config.yaml` directly. This is the xiaozhi-server system prompt; it gets injected alongside the persona file for most providers. Note: `Tier1Slim` deliberately **discards** the top-level prompt when a `persona_file` is set (the 4 B chat template only honours one system message). For `Tier1Slim`, edit the persona file instead.

## Notes

- Always keep the emoji-leader rule in any persona — removing it breaks face animations and `_ensure_emoji_prefix` in the bridge will paper over it with 😐.
- See [tier1slim.md](../tier1slim.md) for why Tier1Slim treats persona files differently from other providers.
- See [protocols.md](../protocols.md) for the emoji → face frame mapping.
