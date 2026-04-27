---
title: Add a New Face Emoji
description: Add a new emoji to the expression system so the robot can show it.
---

# Add a New Face Emoji

The robot's face is driven by the leading emoji in each LLM reply.

## 1. Update the allowed emoji lists

**`bridge.py`** -- edit `ALLOWED_EMOJIS`:

```python
ALLOWED_EMOJIS = ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴", "🥳")
```

**`custom-providers/openai_compat/openai_compat.py`** -- add the same
emoji (use the `\U` escape form, e.g. `"\U0001f973"  # 🥳`).

## 2. Update the suffix prompt (rule 2)

In `bridge.py`, add the emoji to rule 2 in `_BASE_SUFFIX`:

```
2. First character MUST be one of: 😊 😆 😢 😮 🤔 😠 😐 😍 😴 🥳
```

Also update the `prompt:` block in `.config.yaml` if it lists the set.

## 3. Check firmware support

The firmware must map the emoji to a face animation. If it does not
recognize the emoji, the face stays on the previous expression (no crash).
See [protocols.md](../protocols.md) and the upstream
[emotion docs](https://xiaozhi.dev/en/docs/development/emotion/).

## 4. Restart

```bash
systemctl restart zeroclaw-bridge          # ZeroClaw host
docker compose restart xiaozhi-server      # Docker host (if config changed)
```

Current set: 😊 smile, 😆 laugh, 😢 sad, 😮 surprise, 🤔 thinking,
😠 angry, 😐 neutral, 😍 love, 😴 sleepy.
