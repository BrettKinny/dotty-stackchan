# Songs

Audio files played during dance mode. Two supported formats:

## MIDI files (`.mid`) — recommended

Rendered on demand by fluidsynth inside the xiaozhi-server container against the FluidR3 General MIDI soundfont (installed via the `Dockerfile`). The `_encode_midi_to_opus()` helper in `receiveAudioHandle.py` handles tempo override, downmix, resample, and Opus encoding, then caches the result in memory keyed by (path, mtime, rate, tempo, duration).

Example registry entry (in `dances.py`):
```python
"macarena": {
    "audio_file": "config/assets/songs/macarena.mid",
    "audio_tempo_bpm": 103,   # rewrites MIDI tempo events; choreography is locked to BEAT_MS=582
    "duration_ms": BEAT_MS * 48,
    ...
}
```

MIDI files are gitignored — most public MIDI transcriptions are derivative works of copyrighted compositions. Source them yourself (BitMidi, MidiWorld, etc.) and drop into this directory. For Macarena specifically: any sequence at any source tempo works; the runtime helper rewrites it via mido.

## Pre-rendered WAV (`.wav`)

24 kHz mono 16-bit signed PCM. Used for songs that need vocal synthesis (Sinsy, DiffSinger output) or any non-MIDI source. The `_encode_song_to_opus()` helper resamples + Opus-encodes at request time.

Generate via `scripts/render_singing_piper.py` (Piper pitch-shift) or `scripts/render_singing_sinsy.py` (HMM singing voice).

## Naming convention

`<dance_name>.{mid,wav}` matching the key in `DANCE_REGISTRY`. If the file is missing, the dance falls back to silent choreography with an LLM-generated spoken intro.

## Mounting

The `songs/` directory is mounted read-only into the xiaozhi-server container at `/opt/xiaozhi-esp32-server/config/assets/songs/` (see `docker-compose.yml`). Reference paths from `DANCE_REGISTRY` use the in-container path: `config/assets/songs/<name>.<ext>`.
