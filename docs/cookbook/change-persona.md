---
title: Change Persona
description: Swap Dotty's personality by editing the persona prompt or pointing the LLM provider at a different persona file.
---

# Change Persona

Dotty's personality comes from a persona file loaded as the LLM system prompt. **Where** that file lives depends on which LLM provider is active.

Three personas ship in `personas/`:

| File | Style | Used by |
|---|---|---|
| `default.md` | Cheerful, curious desktop robot. The general-purpose persona for generic providers. | `OpenAICompat` |
| `dotty_voice.md` | Voice-tuned variant of `default.md` — same character but pruned for short replies, with the tool catalogue and `[REMEMBER: ...]` markers baked in. | `PiVoiceLLM` |
| `smart.md` | More capable, allowed longer answers — for when `smart_mode` is on and the cloud model is doing the heavy lifting. | optional override |

## Which file controls the persona?

Check `selected_module.LLM` in `.config.yaml`, then read the matching block:

| Provider | Persona source |
|---|---|
| `PiVoiceLLM` (current default) | The persona file configured in the pi agent's extension (`dotty-pi-ext`). Defaults to `personas/dotty_voice.md`. |
| `OpenAICompat` (and similar generic providers) | `LLM.OpenAICompat.persona_file` in `.config.yaml`. |

## Switch to a different shipped persona

1. Edit `.config.yaml` (or the pi agent persona config for `PiVoiceLLM`):

   ```yaml
   LLM:
     OpenAICompat:
       persona_file: personas/smart.md   # was personas/default.md
   ```

2. Restart: `docker compose restart xiaozhi-server`.

## Create your own persona

1. Copy an existing file: `cp personas/dotty_voice.md personas/pirate.md`.
2. Edit the new file. **Keep the emoji instruction line** — the firmware needs it to animate the face. See [emoji-mapping.md](../emoji-mapping.md) for the allowlist (😊😆😢😮🤔😠😐😍😴).
3. Point the active provider's `persona_file` at the new file in `.config.yaml`, then restart.

## Quick inline edit (no file swap)

Edit the top-level `prompt:` block in `.config.yaml` directly. This is the xiaozhi-server system prompt; it gets injected alongside the persona file for most providers. On the `PiVoiceLLM` path the pi agent's own persona file (in `dotty-pi-ext`) is the primary source — edit that for substantive personality changes.

## Notes

- Always keep the emoji-leader rule in any persona — removing it breaks face animations. The persona prompt and the xiaozhi-server system prompt are the two enforcement layers.
- See [protocols.md](../protocols.md) for the emoji → face frame mapping.
