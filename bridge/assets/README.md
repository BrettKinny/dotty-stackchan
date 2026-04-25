# Bridge fixed-audio assets

Drop pre-rendered audio clips here that the bridge can push directly
to a device without going through the LLM + TTS pipeline. These are
"fixed" assets — kid-mode safety sandwiches do not apply, because the
content is curated (not LLM-generated), so anything you put here
becomes effectively permanent voice content for the robot.

## Required files

- `purr.opus` — short cat-purr loop played when the head is petted.
  Target spec: 1-2 second loop, **24 kHz mono Opus**, low rumble,
  fades in/out so a half-played clip doesn't click. A free CC0 cat
  purr from Freesound.org works; or synthesise with sox:

  ```bash
  # Rough purr: 80 Hz square wave with tremolo, low-passed.
  sox -n -r 24000 -c 1 purr.opus \
      synth 1.5 square 80 \
      tremolo 22 50 \
      lowpass 200 \
      fade t 0.15 0 0.2 \
      gain -12
  ```

  The bridge points at this file via `PURR_AUDIO_PATH` (default
  `bridge/assets/purr.opus`). If the file is missing the purr
  consumer logs a warning and no-ops — it never crashes the
  perception bus.

## Why "bypass kid-mode sandwich"

Kid-mode (`DOTTY_KID_MODE=true`) wraps every LLM-generated reply in a
content-filter sandwich: blocked-words regex, age-appropriate suffix,
jailbreak resistance. Fixed audio assets never touch the LLM — they
are pre-curated bytes — so there's nothing to filter. The trade-off
is that **whatever you drop in this directory plays verbatim**, so
review each file the same way you'd review a hardcoded string.
