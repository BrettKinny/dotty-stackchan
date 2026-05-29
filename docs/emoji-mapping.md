---
title: Emoji → Expression Mapping
description: How emoji characters in LLM responses map to face animations on the StackChan.
---

# Emoji → Expression Mapping

Every LLM response starts with an emoji. The xiaozhi-server parses this
emoji and sends an emotion frame to the StackChan firmware, which renders
the corresponding face animation.

## Active Mapping

| Emoji | Emotion ID | Face Animation | Source |
|-------|-----------|----------------|--------|
| 😊 | `happy` | Smiling face | Dotty patch |
| 😆 | `laughing` | Laughing face | Upstream |
| 😢 | `sad` | Sad face | Dotty patch |
| 😮 | `surprised` | Surprised face | Dotty patch |
| 🤔 | `thinking` | Thinking face | Upstream |
| 😠 | `angry` | Angry face | Upstream |
| 😐 | `neutral` | Neutral face | Dotty patch |
| 😍 | `loving` | Love face | Upstream |
| 😴 | `sleepy` | Sleepy face | Upstream |

"Dotty patch" means the emoji was added to the upstream `EMOJI_MAP` in
`custom-providers/textUtils.py`. "Upstream" means it exists in the base
xiaozhi-server code.

## Enforcement (no code fallback on the live path)

On the live `PiVoiceLLM` path there is **no programmatic emoji fallback**.
The old `bridge.py::_ensure_emoji_prefix()` belonged to the retired ZeroClaw
voice path and is gone. The emoji prefix is enforced entirely by prompt
layers: (1) the pi agent persona prompt (`personas/dotty_voice.md`, loaded by
the `dotty-pi` container), and (2) the top-level `prompt:` block in
`data/.config.yaml` injected by xiaozhi-server. The shared
`custom-providers/textUtils.py` (`build_turn_suffix`, `EMOJI_MAP`,
`get_emotion`) carries the per-turn suffix and the emoji → emotion lookup.

If the LLM omits the emoji prefix anyway, nothing prepends one — the firmware
receives no emotion frame and keeps its current expression. If the emoji is
not in `EMOJI_MAP`, the same applies.

## How to Add a New Emoji

See [docs/cookbook/add-emoji.md](cookbook/add-emoji.md).

## Where the Code Lives

| Component | File | What it does |
|-----------|------|-------------|
| Per-turn emoji + rules suffix | `custom-providers/textUtils.py` | `build_turn_suffix()` (appended on the live `PiVoiceLLM` path) |
| Emoji → emotion | `custom-providers/textUtils.py` | `EMOJI_MAP` dict, `get_emotion()` |
| Persona emoji rule | `personas/dotty_voice.md` | loaded by the `dotty-pi` agent |
| xiaozhi system prompt | `data/.config.yaml` | top-level `prompt:` block |
| Emotion → face | StackChan firmware | Avatar renderer, expression assets |

## Upstream Emojis Not Used by Dotty

The upstream `EMOJI_MAP` includes additional emojis that Dotty doesn't
use in its 9-emoji set: 😂 😭 😲 😱 😌 😜 🙄 😶 🙂 😳 😉 😎 🤤 😘 😏.
These would work if the LLM produced them (the firmware would show the
face), but the persona prompt and the `.config.yaml` `prompt:` block
constrain responses to the 9 emojis in the active mapping above.
