---
name: dotty-av-test
description: Run local black-box voice tests against the physical Dotty robot by playing a TTS prompt through the workstation speakers while the C920 records video and room audio. Use when testing Dotty's wake word, ASR, spoken response, expressions, or end-to-end voice behavior on this bench.
---

# Dotty A/V Test

Use the repository harness at `scripts/dotty-av-test.sh`; do not recreate its
FFmpeg, ALSA, PipeWire, volume-safety, or verification logic ad hoc. Read
`docs/dotty-av-tests.md` when choosing prompts or running a multi-case session.

## Before playback

1. The user must have explicitly requested recording. Keep captures local
   unless they separately request sharing or upload.
2. Run `scripts/dotty-av-test.sh devices`. Prefer the stable C920 index-0 path
   and its `hw:C920,0` microphone; index 1 is commonly metadata-only.
3. Run `scripts/dotty-av-test.sh volume` and let the user choose or confirm the
   speaker level, then run `speaker-test`. Do not infer that a successful
   playback process means the user heard a comfortable level.
4. Confirm `espeak-ng`, FFmpeg, `pw-play`, `pactl`, `v4l2-ctl`, and ALSA are
   available. On this Omarchy workstation, the missing TTS dependency is
   installed with `sudo pacman -S --needed espeak-ng`; the human may need to run
   that command because sudo requires their password.

The currently observed hardware is a Logitech C920 (1280x720 MJPEG video;
32 kHz, two-channel S16_LE microphone) and a Volt 4 default PipeWire sink.
Rediscover rather than assuming those devices remain connected.

## Run and evaluate

Run one test at a time:

```bash
scripts/dotty-av-test.sh run "Hi E S P. What is your name?" 20
```

The harness creates a timestamped MP4 under `uat-sessions/<date>/av/`, provides
two seconds of pre-roll, plays offline TTS, leaves the configured response
window, then reports stream metadata and audio levels.

Inspect representative frames when visible state or expression matters. Treat
these as separate assertions:

- capture passed: non-zero video and audio streams with expected durations;
- playback passed: the prompt was audibly clear at the intended position;
- interaction passed: Dotty entered listening, understood the prompt, replied,
  animated appropriately, and recovered to idle.

Never report the interaction as passed merely because the MP4 and audio signal
exist. Record silence/non-response as an interaction failure or deferred
diagnostic while preserving a successful capture result.

## Bench observations from 2026-09-12

- A 26-second full run produced valid H.264 1280x720 video and 32 kHz stereo AAC
  audio, with mean level -26.2 dB and peak -1.4 dB.
- The user found 20% Volt output too soft, so future sessions must recalibrate
  with the user; do not silently reuse 20%.
- Dotty did not respond to espeak-ng saying “Hi E S P. What is your name?” at
  that level. Wake-word pronunciation, level, placement, or live Dotty state
  remain unproven and should be diagnosed separately.
- ALSA reported buffer xruns during that run even though both encoded streams
  had complete 26-second durations. Note repeated xruns and inspect the media;
  do not discard a complete recording solely because of that warning.

Do not change device permissions, reload drivers, record indefinitely, or
overwrite an existing capture without specific authorization.

*This skill was drafted by OpenAI Codex from a human-observed bench test.*
