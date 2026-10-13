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
# Optional diagnostic: same capture process also writes diagnostic.capture.wav.
DOTTY_AV_LOSSLESS_AUDIO=1 scripts/dotty-av-test.sh record 10 /tmp/diagnostic.mp4
```

Device selection can be overridden with `DOTTY_AV_VIDEO_DEVICE`,
`DOTTY_AV_AUDIO_DEVICE`, and `DOTTY_AV_SINK`. The defaults match the C920 and
the current default PipeWire speaker sink.

`DOTTY_AV_LOSSLESS_AUDIO=1` adds a 32 kHz stereo `pcm_f32le` WAV named from
the unique MP4 destination (`raw.mp4` → `raw.capture.wav`). Both outputs map the
same audio input in one FFmpeg process and have the same bounded duration;
existing MP4s or sidecars are refused. The default remains MP4-only. With the
Pulse backend, this opt-in also requests float input instead of Pulse's default
16-bit input format, so above-full-scale samples can survive into the WAV. The
ALSA fallback retains its existing input format and converts that captured
signal to float for storage. The input-codec selection follows
[FFmpeg's Pulse demuxer](https://github.com/FFmpeg/FFmpeg/blob/master/libavdevice/pulse_audio_dec.c).

This is a capture-quality diagnostic, not proof of analogue microphone
clipping. AAC decoding can create overshoots; the same-input PCM provides an
independent comparison before AAC encoding, but does not undo clipping or gain
earlier in the audio stack. MP4 and WAV start timestamps need not align exactly:
the WAV starts at its first audio sample, and the recording-launch timestamp
precedes device startup. Do not use matching nominal durations as proof of
sample-accurate timing.

The overnight evaluator records native-channel prompt and response window
metrics separately under `native_audio_windows`, including the sidecar when
present. Prompt metrics include pre-roll and use the approximate playback-end
anchor; response metrics start after the existing tail margin. These windows
do not downmix or normalize and do not automatically pass an interaction.
For follow-up or state-command cases that must start with an open microphone,
set `require_warm_listening: true` on the case. An absent/stale listening signal
blocks playback; it does not change the robot's state. The default retains the
existing `ready_for_followup` prerequisite, except for explicit cold-wake tests.

## Unattended runs

By default a warm case never changes the robot to satisfy its prerequisite: a
stale or closed-microphone status blocks the case, and a blocked case is not
retried. After one case the listening status is already older than the 30 s
limit, so an unattended session needs an explicit opener:

```bash
scripts/dotty_overnight.py init --session uat-sessions/$(date +%F)/overnight \
  --host user@docker-host --mic-opener admin_say
```

With `mic_opener: admin_say` the runner asks `/xiaozhi/admin/say` to make the
robot say "Ready."; the firmware then opens the microphone. The runner waits
until the microphone has been open 4–14 s before playing the prompt, and
records `mic_opened_by` in the result. Playing the wake word through speakers
proved placement-sensitive on the bench and is not used for this.

Use pre-rendered natural-voice prompts (`prompt_wavs` in the session
`config.json`, bound to the exact text and a SHA-256). The harness default
`espeak-ng` voice was mis-transcribed by Whisper even from the reference
microphone; Piper prompts were recognised 17 times out of 18.

Evidence rules worth knowing when reading results:

- `playback` passes on the reference-microphone transcript **or** on the
  robot's own recognition of the prompt (`playback_evidence` says which).
- If the reference microphone mishears a reply whose service TTS text does
  contain the expected words, the case is `INCONCLUSIVE`
  (`response_transcript_disagrees_with_tts`), not a failure. Service text never
  passes a case by itself.
- Any other recognised speech inside the capture makes the case
  `INCONCLUSIVE` (`extra_speech_in_capture`): keep the room quiet.
- Inconclusive attempts reset a case's pass streak but do not count toward
  quarantine or the three-failure pause.

*Unattended-run additions were AI-assisted by Claude (Opus 5.5) from a
supervised bench session on 2026-10-05.*

*Lossless capture and window-metrics additions were AI-assisted by OpenAI Codex
(GPT-6); their automated checks use synthetic audio, not a physical calibration.*
