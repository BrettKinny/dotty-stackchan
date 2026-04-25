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

## Fallback Behavior

If the LLM forgets the emoji prefix, `bridge.py` prepends `😐` (neutral)
via `_ensure_emoji_prefix()`. If the emoji is not in `EMOJI_MAP`, the
firmware receives no emotion frame and keeps its current expression.

## How to Add a New Emoji

See [docs/cookbook/add-emoji.md](cookbook/add-emoji.md).

## Where the Code Lives

| Component | File | What it does |
|-----------|------|-------------|
| Emoji enforcement | `bridge.py` | `ALLOWED_EMOJIS` tuple, `_ensure_emoji_prefix()` |
| Emoji → emotion | `custom-providers/textUtils.py` | `EMOJI_MAP` dict, `get_emotion()` |
| Emotion → face | StackChan firmware | Avatar renderer, expression assets |

## Upstream Emojis Not Used by Dotty

The upstream `EMOJI_MAP` includes additional emojis that Dotty doesn't
use in its `ALLOWED_EMOJIS`: 😂 😭 😲 😱 😌 😜 🙄 😶 🙂 😳 😉 😎 🤤 😘 😏.
These would work if the LLM produced them (the firmware would show the
face), but the bridge's emoji enforcement constrains responses to the
9 emojis in the active mapping above.
