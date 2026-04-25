---
title: Change Persona
description: Swap Dotty's personality by editing the persona prompt or switching persona files.
---

# Change Persona

Dotty's personality comes from a persona file loaded as the LLM system prompt.
Three personas ship in `personas/`:

| File | Style |
|---|---|
| `default.md` | Cheerful, curious desktop robot (ships active) |
| `assistant.md` | Straightforward general-purpose assistant |
| `playful.md` | Extra silly, joke-heavy, kid-oriented |

## Switch to a different shipped persona

1. Open `.config.yaml` and change the `persona_file` value:

```yaml
LLM:
  OpenAICompat:
    persona_file: personas/playful.md   # was personas/default.md
```

2. Restart: `docker compose restart xiaozhi-server`

## Create your own persona

1. Copy an existing file: `cp personas/default.md personas/pirate.md`
2. Edit the new file. Keep the emoji instruction line -- the firmware
   needs it to animate the face.
3. Point `.config.yaml` at the new file and restart.

## Quick inline edit (no file swap)

Edit the top-level `prompt:` block in `.config.yaml` directly. This is
the xiaozhi-server system prompt, injected alongside the persona file.

## Notes

- The ZeroClaw backend uses its own persona in `~/.zeroclaw/workspace/`
  (`SOUL.md`, `IDENTITY.md`) instead of `persona_file`.
- Always keep the emoji instruction line -- removing it breaks face
  animations. See [protocols.md](../protocols.md) for the mapping.
