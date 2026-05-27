# /bench-video — Close the Video Loop in Bench Testing

Run this command when you want to test a recent change on the physical Dotty robot and record evidence.

**Argument (optional):** a short description of what to test. If omitted, Claude will infer it from recent git changes.

---

## Workflow

### Step 1 — Identify what to test

Look at recent changes to determine the most relevant thing to exercise:

```bash
git log --oneline -10
git diff HEAD~3..HEAD --stat
```

Read the diff of anything that touched:
- `custom-providers/` — LLM/TTS/ASR behaviour changes
- `firmware/` — LED, state, perception, audio changes  
- `dotty-behaviour/` — consumer or perception bus changes
- `dotty-pi-ext/` — voice tool changes

If the user supplied `$ARGUMENTS`, use that as the test description verbatim. Otherwise, synthesise a one-sentence description from the diff.

### Step 2 — Write a clear test card

Before starting the script, print a **test card** for the user so they know exactly what to do and what to look for:

```
TEST: <one-line description>

TRIGGER:
  • Exactly what to say / do to start the test
  • (e.g. "Say 'Hey Dotty, what time is it?'"  or  "Walk in front of the camera")

EXPECT TO SEE:
  • LED ring behaviour (which pixels, which colour, sequence)
  • Face animation (which emoji should appear)
  • What Dotty should say (verbatim or paraphrase)
  • Head movement (if any)

EXPECT IN LOGS:
  • Key log lines / patterns to watch for
  • Error lines that would indicate failure

PASS CRITERIA:
  • The single observable outcome that means "this worked"
```

### Step 3 — Run the capture + analysis script

```bash
bash scripts/bench-video.sh "<test description>"
```

The script will:
1. SSH-preflight to the Docker host
2. Tail `xiaozhi-esp32-server`, `dotty-pi`, `dotty-behaviour`, `dotty-bridge` logs from the moment the test starts
3. Prompt the user to record the test on their phone and upload to YouTube Shorts
4. Accept the YouTube URL
5. Stop log capture
6. Call `gemini --model gemini-2.5-pro` with the video URL and captured logs
7. Print Gemini's structured visual + log analysis

Wait for the script to complete and print the Gemini report.

### Step 4 — Synthesise results

After the Gemini output, add your own synthesis:

**Combined verdict:** Summarise in one sentence — did the test pass or fail, and what is the primary evidence?

**Correlations confirmed:** Which log events matched the visual? (Shows the instrumentation is trustworthy.)

**Discrepancies to investigate:** Anything Gemini saw that isn't in the logs, or vice versa.

**Action items:** If the test failed or had issues, list concrete code changes to try next. If it passed, suggest what the next test should be.

---

## Environment requirements

- `DOTTY_HOST=user@host` must be set (or in `.env`) — the Docker host running all containers.
- `gemini` must be on PATH: `npm install -g @google/gemini-cli`
- SSH key auth to the Docker host (no passphrase prompt).

If these aren't met the script will tell you exactly what's missing.

---

## Notes

- YouTube Shorts unlisted links work fine — Gemini can access them.
- The video does not need to be public; unlisted is preferred for bench recordings.
- If `gemini` returns an error about model availability, try `GEMINI_MODEL=gemini-2.0-flash bash scripts/bench-video.sh`.
- Logs are kept alive until you press Enter so you can inspect them after the Gemini analysis.
- LED ring contract is documented in `docs/modes.md` — reference it when writing the test card.
