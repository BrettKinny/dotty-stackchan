#!/usr/bin/env bash
# Record Dotty with the workstation webcam/mic while playing repeatable
# spoken prompts through the workstation speakers.

set -euo pipefail

VIDEO_DEVICE="${DOTTY_AV_VIDEO_DEVICE:-/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_7B90DC9F-video-index0}"
AUDIO_DEVICE="${DOTTY_AV_AUDIO_DEVICE:-hw:C920,0}"
AUDIO_BACKEND="${DOTTY_AV_AUDIO_BACKEND:-pulse}"
AUDIO_SOURCE="${DOTTY_AV_AUDIO_SOURCE:-alsa_input.usb-046d_HD_Pro_Webcam_C920_7B90DC9F-02.analog-stereo}"
SINK="${DOTTY_AV_SINK:-@DEFAULT_SINK@}"
VIDEO_SIZE="${DOTTY_AV_VIDEO_SIZE:-1280x720}"
VIDEO_FPS="${DOTTY_AV_VIDEO_FPS:-15}"
VOLUME="${DOTTY_AV_VOLUME:-20}"
RESPONSE_SECONDS="${DOTTY_AV_RESPONSE_SECONDS:-20}"
OUT_DIR="${DOTTY_AV_OUT_DIR:-uat-sessions/$(date +%F)/av}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MAX_SECONDS="${DOTTY_AV_MAX_SECONDS:-180}"

cleanup() {
    if [[ -n "${capture_pid:-}" ]]; then
        kill -INT "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    [[ -z "${speech:-}" ]] || rm -f -- "$speech"
    [[ -z "${tone:-}" ]] || rm -f -- "$tone"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lock_capture() {
    # Shared by all invocations, regardless of output directory.
    exec 9>"${XDG_RUNTIME_DIR:-/tmp}/dotty-av-${UID}.lock"
    flock -n 9 || { echo 'ERROR: another Dotty capture owns the camera' >&2; exit 1; }
}

bounded() {
    [[ "$1" =~ ^[0-9]+$ && "$MAX_SECONDS" =~ ^[0-9]+$ ]] &&
        (( $1 > 0 && $1 <= MAX_SECONDS )) || {
        echo "ERROR: capture must be 1..${MAX_SECONDS} seconds" >&2; exit 2;
    }
}

usage() {
    cat <<'EOF'
Usage:
  scripts/dotty-av-test.sh devices
  scripts/dotty-av-test.sh volume [0-60]
  scripts/dotty-av-test.sh speaker-test
  scripts/dotty-av-test.sh record [seconds] [output.mp4]
  scripts/dotty-av-test.sh run "spoken prompt" [response-seconds] [output.mp4]
  scripts/dotty-av-test.sh verify recording.mp4

Environment overrides:
  DOTTY_AV_VIDEO_DEVICE, DOTTY_AV_AUDIO_DEVICE, DOTTY_AV_SINK
  DOTTY_AV_AUDIO_BACKEND (pulse default, alsa fallback), DOTTY_AV_AUDIO_SOURCE
  DOTTY_AV_VIDEO_SIZE, DOTTY_AV_VIDEO_FPS, DOTTY_AV_VOLUME
  DOTTY_AV_RESPONSE_SECONDS, DOTTY_AV_OUT_DIR

The run command needs espeak-ng for local/offline TTS. No recording is
uploaded. Volume is capped at 60% unless DOTTY_AV_ALLOW_HIGH_VOLUME=1.
EOF
}

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: required command not found: $1" >&2
        exit 1
    }
}

set_volume() {
    local percent="$1"
    [[ "$percent" =~ ^[0-9]+$ ]] || {
        echo "ERROR: volume must be a whole-number percentage" >&2
        exit 2
    }
    if (( percent > 60 )) && [[ "${DOTTY_AV_ALLOW_HIGH_VOLUME:-0}" != 1 ]]; then
        echo "ERROR: refusing volume above 60%; set DOTTY_AV_ALLOW_HIGH_VOLUME=1 to override" >&2
        exit 2
    fi
    (( percent <= 150 )) || {
        echo "ERROR: volume must be 150% or lower" >&2
        exit 2
    }
    pactl set-sink-mute "$SINK" 0
    pactl set-sink-volume "$SINK" "${percent}%"
    pactl get-sink-volume "$SINK"
}

default_output() {
    local label="$1"
    printf '%s/%s-%s.mp4\n' "$OUT_DIR" "$(date +%Y%m%d-%H%M%S)" "$label"
}

capture_args() {
    printf '%s\n' \
        -thread_queue_size 512 -f v4l2 -input_format mjpeg -video_size "$VIDEO_SIZE" -framerate "$VIDEO_FPS" -i "$VIDEO_DEVICE"
    case "$AUDIO_BACKEND" in
        alsa) printf '%s\n' -thread_queue_size 512 -f alsa -ac 2 -ar 32000 -i "$AUDIO_DEVICE" ;;
        pulse) printf '%s\n' -thread_queue_size 512 -f pulse -ac 2 -ar 32000 -i "$AUDIO_SOURCE" ;;
        *) echo "ERROR: unknown audio backend $AUDIO_BACKEND" >&2; exit 2 ;;
    esac
    printf '%s\n' -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -b:a 128k
}

verify() {
    local file="$1"
    [[ -s "$file" ]] || { echo "ERROR: missing or empty recording: $file" >&2; exit 1; }
    python "$SCRIPT_DIR/dotty_av_media.py" verify "$file"
    echo "Streams:"
    ffprobe -v error \
        -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,duration,nb_frames \
        -of default=noprint_wrappers=1 "$file"
    echo "Audio level:"
    ffmpeg -hide_banner -i "$file" -map 0:a:0 -af volumedetect -f null - 2>&1 \
        | sed -n '/mean_volume:/p;/max_volume:/p'
}

record() {
    local seconds="$1" output="$2"
    [[ "$seconds" =~ ^[0-9]+$ ]] && (( seconds > 0 )) || {
        echo "ERROR: duration must be a positive whole number" >&2
        exit 2
    }
    [[ ! -e "$output" ]] || { echo "ERROR: output already exists: $output" >&2; exit 1; }
    bounded "$seconds"
    lock_capture
    mkdir -p "$(dirname "$output")"
    mapfile -t args < <(capture_args)
    ffmpeg -nostdin -n -hide_banner -loglevel warning "${args[@]}" -t "$seconds" "$output" &
    capture_pid=$!
    wait "$capture_pid"
    capture_pid=''
    verify "$output"
}

cmd="${1:-}"
case "$cmd" in
devices)
    need v4l2-ctl
    need arecord
    need pactl
    v4l2-ctl --list-devices
    arecord -l
    pactl list short sinks
    pactl list short sources
    ;;
volume)
    need pactl
    requested="${2:-}"
    if [[ -z "$requested" ]]; then
        pactl get-sink-volume "$SINK"
        read -r -p "Speaker volume percent (recommended 15-30): " requested
    fi
    set_volume "$requested"
    ;;
speaker-test)
    need ffmpeg
    need pw-play
    need pactl
    set_volume "$VOLUME"
    tone="$(mktemp --suffix=.wav)"
    ffmpeg -hide_banner -loglevel error -y -f lavfi -i 'sine=frequency=440:duration=0.5' -af 'volume=-18dB' "$tone"
    echo "Playing a quiet half-second calibration tone at ${VOLUME}%..."
    target_sink="$SINK"
    [[ "$target_sink" != '@DEFAULT_SINK@' ]] || target_sink="$(pactl get-default-sink)"
    pw-play --target "$target_sink" "$tone"
    ;;
record)
    need ffmpeg
    need ffprobe
    seconds="${2:-10}"
    output="${3:-$(default_output ambient)}"
    record "$seconds" "$output"
    ;;
run)
    need ffmpeg
    need ffprobe
    need espeak-ng
    need pw-play
    need pactl
    prompt="${2:?provide the text to speak}"
    response_seconds="${3:-$RESPONSE_SECONDS}"
    [[ "$response_seconds" =~ ^[0-9]+$ ]] && (( response_seconds >= 5 )) || {
        echo "ERROR: response duration must be a whole number of at least 5 seconds" >&2
        exit 2
    }
    output="${4:-$(default_output response)}"
    [[ ! -e "$output" ]] || { echo "ERROR: output already exists: $output" >&2; exit 1; }
    lock_capture
    mkdir -p "$(dirname "$output")"
    speech="$(mktemp --suffix=.wav)"
    espeak-ng -v en-au -s 145 -w "$speech" "$prompt"
    speech_seconds="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$speech")"
    total_seconds="$(awk -v speech="$speech_seconds" -v response="$response_seconds" 'BEGIN { printf "%d", speech + response + 3.999 }')"
    bounded "$total_seconds"
    set_volume "$VOLUME"
    target_sink="$SINK"
    [[ "$target_sink" != '@DEFAULT_SINK@' ]] || target_sink="$(pactl get-default-sink)"
    cp -- "$speech" "${output%.mp4}.prompt.wav"
    mapfile -t args < <(capture_args)
    # Launch-time anchor; first encoded sample may lag device initialization.
    date -u +%FT%T.%NZ > "${output%.mp4}.recording-start.txt"
    ffmpeg -nostdin -n -hide_banner -loglevel warning "${args[@]}" -t "$total_seconds" "$output" &
    capture_pid=$!
    echo "Recording. Prompt plays in 2 seconds; Dotty then has ${response_seconds}s to answer."
    sleep 2
    kill -0 "$capture_pid" 2>/dev/null || { echo 'ERROR: capture failed before playback' >&2; exit 1; }
    [[ -s "$output" ]] || { echo 'ERROR: capture produced no file before playback' >&2; exit 1; }
    date -u +%FT%T.%NZ > "${output%.mp4}.playback-start.txt"
    pw-play --target "$target_sink" "$speech"
    date -u +%FT%T.%NZ > "${output%.mp4}.playback-end.txt"
    wait "$capture_pid"
    capture_pid=''
    verify "$output"
    echo "Saved: $output"
    ;;
verify)
    need ffmpeg
    need ffprobe
    verify "${2:?provide a recording path}"
    ;;
*)
    usage
    [[ -z "$cmd" ]] || exit 2
    ;;
esac
