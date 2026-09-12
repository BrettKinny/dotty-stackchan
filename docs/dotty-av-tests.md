---
title: Dotty speaker-to-robot A/V tests
description: Record Dotty with the C920 while sending repeatable TTS prompts through workstation speakers.
---

# Dotty speaker-to-robot A/V tests

> **AI-assistance note:** this test guide and its companion script were drafted
> by OpenAI Codex. A human should review the prompts, volume, framing, and test
> results before relying on them.

This setup uses the Logitech C920 for 720p video and 32 kHz stereo audio, and
the workstation's default PipeWire sink for prompt playback. It is intended for
repeatable black-box tests: the workstation speaks, Dotty hears and responds,
and one MP4 captures both the visible behaviour and room audio. Recordings stay
local under `uat-sessions/<date>/av/`.

## One-time prerequisite

Install the offline TTS command used to render prompts:

```bash
sudo pacman -S --needed espeak-ng
```

FFmpeg, PipeWire, `v4l2-ctl`, and ALSA must also be available.

## Calibrate safely

```bash
scripts/dotty-av-test.sh devices
scripts/dotty-av-test.sh volume
scripts/dotty-av-test.sh speaker-test
scripts/dotty-av-test.sh record 5
```

The volume command is interactive and recommends 15–30%. The harness refuses
more than 60% by default. The speaker test is a quiet half-second tone; use it
before spoken tests and adjust the Volt 4's physical monitor knob as needed.
Place the speakers near the C920 but pointed toward Dotty. Keep Dotty's own
speaker closer to the camera microphone so its answer remains clear.

## Test sequence

Start with a short baseline and run each command separately. Wake-word success
depends strongly on speaker placement and room echo.

```bash
# Wake word plus a deterministic identity question.
scripts/dotty-av-test.sh run "Hi E S P. What is your name?" 20

# ASR clarity: five distinct words and a constrained reply.
scripts/dotty-av-test.sh run \
  "Hi E S P. Repeat these five words: purple, robot, seven, window, Brisbane." 20

# Emotion-prefix/display path.
scripts/dotty-av-test.sh run \
  "Hi E S P. Tell me one short thing you love about being a robot." 20

# Multi-turn recovery; run after Dotty has returned to idle.
scripts/dotty-av-test.sh run \
  "Hi E S P. What is two hundred and forty seven plus eighty six?" 25
```

For each recording, check:

- the wake phrase opens listening mode;
- Dotty transcribes the important words correctly;
- speech begins without a long unexplained stall;
- the response starts with an appropriate animated face;
- Dotty returns to idle without rebooting;
- `verify` reports video and audio streams plus a measurable audio peak.

Use a custom destination when a stable test ID matters:

```bash
scripts/dotty-av-test.sh run \
  "Hi E S P. What is your name?" 20 uat-sessions/$(date +%F)/av/AV01-name.mp4
```

If automatic wake-word playback proves unreliable, tap Dotty to open the mic,
then run the same prompt without “Hi E S P.”. Use headphones only for inspecting
recordings; they cannot drive Dotty's microphone during a live test.

## Useful overrides

```bash
DOTTY_AV_VOLUME=25 scripts/dotty-av-test.sh speaker-test
DOTTY_AV_RESPONSE_SECONDS=30 scripts/dotty-av-test.sh run "Hi E S P. Tell me a joke."
DOTTY_AV_OUT_DIR=/tmp/dotty-tests scripts/dotty-av-test.sh record 10
```

Device selection can be overridden with `DOTTY_AV_VIDEO_DEVICE`,
`DOTTY_AV_AUDIO_DEVICE`, and `DOTTY_AV_SINK`. The defaults match the C920 and
the current default PipeWire speaker sink.
