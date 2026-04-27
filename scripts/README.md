# Scripts

Developer tooling for Dotty's singing mode. None of this runs in the live audio path — these scripts produce the WAV files that `_handle_dance()` injects into the TTS queue.

## render_singing_piper.py — Phase 1: Quick prototype

Synthesizes the Macarena vocal track using the existing Piper TTS model with per-phrase pitch + speed manipulation. Produces "charming pitched speech," not real singing — fast to iterate on, no new dependencies.

Run inside the xiaozhi-server container:
```
ssh root@<XIAOZHI_HOST> "docker cp /mnt/user/appdata/xiaozhi-server/scripts/render_singing_piper.py xiaozhi-esp32-server:/opt/xiaozhi-esp32-server/render_singing_piper.py && \
  docker exec xiaozhi-esp32-server python /opt/xiaozhi-esp32-server/render_singing_piper.py"
```

The output goes to `/opt/xiaozhi-esp32-server/config/assets/songs/macarena.wav` inside the container — copy it out to the host songs/ directory so it persists across restarts:
```
ssh root@<XIAOZHI_HOST> "docker cp xiaozhi-esp32-server:/opt/xiaozhi-esp32-server/config/assets/songs/macarena.wav /mnt/user/appdata/xiaozhi-server/songs/macarena.wav"
```

Edit the `PHRASES` list in the script to tweak lyrics, pitch, or timing.

## postprocess_song.py — Phase 2: DiffSinger normalizer

Takes a high-quality singing render from DiffSinger / OpenUtau / NNSVS / VISinger2 and normalizes it to the 24 kHz mono int16 WAV format the device expects. Does the dirty work of resampling, downmixing, and beat-aligned trimming.

```
python scripts/postprocess_song.py input.wav songs/macarena.wav --duration-ms 27936
```

The `27936` ms target matches the choreography duration (`BEAT_MS=582 * 48 beats`). Drop the flag to skip duration fitting.

### DiffSinger authoring workflow (Phase 2)

1. **Install OpenUtau** on the workstation — Linux AppImage from https://www.openutau.com/ or build from source.

2. **Install a DiffSinger English voice bank** (search HuggingFace for `DiffSinger English` — community-trained banks for Miku, Tiger, Cori, etc.). Drop the voicebank folder into OpenUtau's `Singers/` directory.

3. **Author the Macarena project**:
   - Open OpenUtau, set BPM to **103** (matches `BEAT_MS=582`).
   - Import a public-domain Macarena MIDI (BitMidi, MidiWorld) — main melody track only.
   - Switch to the DiffSinger voice bank for the track.
   - Type lyrics syllable-by-syllable on the piano roll. Use `+` for sustains.
   - Export → "Render to file" → WAV (44.1 kHz or 48 kHz mono, doesn't matter — postprocess will normalize).

4. **Normalize and deploy**:
   ```
   python scripts/postprocess_song.py macarena_diffsinger.wav songs/macarena.wav --duration-ms 27936
   scp songs/macarena.wav root@<XIAOZHI_HOST>:/mnt/user/appdata/xiaozhi-server/songs/
   ```

5. **Test**: trigger "do the macarena" with the device. The mounted file is read directly — no container restart needed (the next dance trigger picks it up fresh).

### Optional: backing track

Open a Macarena instrumental MIDI in any DAW (Reaper, LMMS) and render to WAV. Mix 70% vocal / 30% backing in your DAW or Audacity, then run that mixdown through `postprocess_song.py`. Avoid using copyrighted instrumental recordings — render from MIDI to keep it safe.
