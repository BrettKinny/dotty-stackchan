# Local overnight clip review

AI-assisted implementation and documentation: OpenAI Codex (GPT-6). Human review
is required before publication. This helper never uploads or plays recordings.

Export an interesting completed case using source-video seconds. Retain the
spoken question and complete response when selecting a window:

```bash
python scripts/dotty_av_clips.py export uat-sessions/DATE/SESSION CASE_ID --start 0 --end 35 --title 'Dotty gives a tiny performance review'
python scripts/dotty_av_clips.py index uat-sessions/DATE/SESSION
```

The first command prints the new export directory. The second prints a local HTML
review index; each export also contains its own `index.html`. Every index is a new
snapshot. Original evidence is preserved; existing export names cannot be reused.
Partial failed exports are retained with `export_state: incomplete` in their
manifest, and should be retried under a new name.

Each export contains a fitted portrait 1080×1920 `clean.mp4`, a thumbnail, a
provenance manifest and, when local transcription and its capture offset are
available, a provisional `captions.srt`. Captions describe the response only;
the spoken prompt remains in the original audio. All source pixels are retained
within the portrait frame. The export normalizes original sound; it does not
replace the soundtrack. Captions are a sidecar, not burned into the clean video.

`manifest.json` records the source recording hash, exact source interval, full case
timestamp (safe across midnight), code commit, case verdict and caption alignment.
The review index links back to the uncut source and original result. A passing
automated test never automatically becomes a promotional recommendation.

Exports default to `clips/needs-review/`. Failed cases go to `clips/failures/`,
even when reviewed. Inspect these five independent gates before placing an export
in `clips/ready-for-review/`:

- `visual`: framing, motion and the full interaction are intelligible.
- `privacy`: faces, household information and spoken personal details are cleared.
- `captions`: transcription and timing were checked, or omission was intentional.
- `music`: any music or third-party audio is cleared for the intended use.
- `context`: the trim and suggested title honestly represent the observed result.

For a new export after those checks, repeat `--reviewed GATE` for each gate.
That acknowledges review; the helper does not independently prove these facts.
Ready-for-review still means a human makes the final posting decision. Keep the
session outside public web hosting and version control; recordings stay local.

Dependencies: Python 3.11+, FFmpeg with libx264, and ffprobe. This tool consumes
existing `cases/CASE_ID/{raw.mp4,result.json,response.json}` artifacts and never
runs transcription models itself. `response.json` times are relative to the
response audio; `result.json.response_offset` locates that audio within `raw.mp4`.
