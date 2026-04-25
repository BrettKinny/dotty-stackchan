# Songs

Pre-rendered song WAV files played during dance/singing mode.

## Format requirements

All files in this directory MUST be:
- **Sample rate**: 24000 Hz
- **Channels**: Mono
- **Bit depth**: 16-bit signed PCM
- **Container**: WAV

This matches the device's downlink sample rate. `audio_to_data()` in xiaozhi-server reads the file and Opus-encodes it for the WebSocket stream.

## Naming convention

`<dance_name>.wav` — must match the key in `DANCE_REGISTRY` (in `dances.py`). When the dance is triggered, the matching audio file (if present) is queued into `tts_audio_queue` and plays alongside the choreography.

If no matching file exists, the dance falls back to silent choreography with an LLM-generated spoken intro.

## Generating songs

### Phase 1: Piper pitch-shift (quick prototype)
```
python scripts/render_singing_piper.py
```
Produces `songs/macarena.wav` using the existing Piper voice model at varying pitch/speed per phrase.

### Phase 2: DiffSinger (higher quality)
1. Author the song in OpenUtau on the workstation
2. Render to WAV using a DiffSinger voice bank
3. Run `python scripts/postprocess_song.py <input.wav> <output.wav>` to normalize to the format above

## Mounting

The `songs/` directory is mounted read-only into the xiaozhi-server container at `/opt/xiaozhi-esp32-server/config/assets/songs/` (see `docker-compose.yml`). Reference paths from `DANCE_REGISTRY` use the in-container path: `config/assets/songs/<name>.wav`.
