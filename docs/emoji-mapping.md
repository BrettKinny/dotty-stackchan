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

## Enforcement on the live PiVoiceLLM path

`build_turn_suffix()` requests one of the nine allowed emojis in every reply.
`enforce_face_stream()` in `custom-providers/textUtils.py` (used by
`PiVoiceLLM` and `OpenAICompat`) then enforces the wire contract: exactly one
supported face at the head of the first non-empty chunk, which is the chunk
xiaozhi reads the emotion from. An allowed prefix is preserved, a near-miss
emoji is mapped onto the supported set via `FACE_ALIASES` (`😲` → `😮`,
`🧐` → `🤔`, `😂` → `😆`), and anything else gets neutral `😐`. Emoji later in
the reply are stripped. Persona files
and xiaozhi's top-level `.config.yaml` prompt are not forwarded by PiVoiceLLM;
Pi runs with `--no-context-files`.

If the model omits the prefix, the neutral fallback is used, unless the reply opens with an unmistakable word ("Wow" → `😮`, "Hmm" → `🤔`, "Haha" → `😆`). A newly allowed
emoji must be added consistently to `ALLOWED_EMOJIS`, `EMOJI_MAP`, and the
firmware mapping or it will not select the intended face.

## How to Add a New Emoji

See [docs/cookbook/add-emoji.md](cookbook/add-emoji.md).

## Where the Code Lives

| Component | File | What it does |
|-----------|------|-------------|
| Emoji + reply rules | `custom-providers/textUtils.py` | `build_turn_suffix()` (in pi's system prompt on the live `PiVoiceLLM` path) |
| Leading-emoji enforcement | `custom-providers/textUtils.py` | `enforce_face_stream()`, `FACE_ALIASES` |
| Emoji → emotion | `custom-providers/textUtils.py` | `EMOJI_MAP` dict, `get_emotion()` |
| Emotion → face | StackChan firmware | Avatar renderer, expression assets |

## Upstream Emojis Not Used by Dotty

The upstream `EMOJI_MAP` includes additional emojis that Dotty doesn't
use in its 9-emoji set: 😂 😭 😲 😱 😌 😜 🙄 😶 🙂 😳 😉 😎 🤤 😘 😏.
PiVoiceLLM's system-prompt rules constrain responses to the nine emojis above,
and `enforce_face_stream()` maps any other leading emoji onto that set
(`FACE_ALIASES`) or, when no face fits, replaces it with `😐`.
