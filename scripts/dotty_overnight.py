#!/usr/bin/env python3
"""Bounded overnight physical Dotty tests. AI-assisted: OpenAI Codex (GPT-6).

This runner does not edit, deploy, restart or flash product code. The supervising
agent owns controlled repair experiments. All evidence and transcription stay local.
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from dotty_av_media import verify

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("xiaozhi-esp32-server", "dotty-behaviour", "dotty-bridge", "dotty-pi")
TZ = ZoneInfo("Australia/Brisbane")
STOP = False


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def journal(session, event):
    with (session / "events.jsonl").open("a") as f:
        f.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def command(args, timeout=30, **kwargs):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True,
                          timeout=timeout, check=True, **kwargs).stdout


def ssh(host, argv, timeout=30, **kwargs):
    return command(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
                    shlex.join([str(a) for a in argv])], timeout=timeout, **kwargs)


def admin(host, route, body=None):
    if route not in {"devices", "songs", "abort", "set-state", "set-toggle",
                     "set-head-angles", "take-photo", "play-asset", "say", "inject-text"}:
        raise ValueError("unsupported admin route")
    # Token stays in the container: never in argv, logs, evidence or shell expansion.
    code = '''import json,os,sys,urllib.request
route, body = json.loads(sys.argv[1])
headers={"Content-Type":"application/json", "X-Admin-Token":os.environ.get("DOTTY_ADMIN_TOKEN", "")}
req=urllib.request.Request("http://127.0.0.1:8003/xiaozhi/admin/"+route,
 data=None if body is None else json.dumps(body).encode(), headers=headers)
with urllib.request.urlopen(req, timeout=10) as r: print(r.read().decode())
'''
    return json.loads(ssh(host, ["docker", "exec", "-i", "xiaozhi-esp32-server",
                                "python", "-", json.dumps([route, body])], input=code))


def snapshot(host):
    result = {"at": now(), "errors": {}}
    for name, url in {
        "bridge": "http://localhost:8081/health",
        "behaviour": "http://localhost:8090/health",
        "perception": "http://localhost:8090/api/perception/state",
    }.items():
        try:
            result[name] = json.loads(ssh(host, ["curl", "-fsS", "--max-time", "5", url]))
        except Exception as exc:
            result["errors"][name] = type(exc).__name__
    try:
        result["devices"] = admin(host, "devices")["devices"]
        raw = ssh(host, ["docker", "inspect", "--format",
                         '{{json .Name}} {{json .State.Running}} {{json .RestartCount}} {{json .Image}}',
                         *SERVICES])
        result["containers"] = raw.strip().splitlines()
        result["services_running"] = len(result["containers"]) == 4 and all(
            ' true ' in line for line in result["containers"])
    except Exception as exc:
        result["errors"]["containers"] = type(exc).__name__
    return result


def normalize(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def matches(text, groups):
    normalized = normalize(text)
    # Token boundaries matter: "no" must not pass merely because "know" occurs.
    return all(any(re.search(r"(?<!\w)" + re.escape(normalize(option)) + r"(?!\w)",
                             normalized) for option in group if normalize(option))
               for group in groups)


def evaluate(case, transcript, log_text, playback_end, after, device):
    # Only response-window words count. Prompt and response transcribed separately
    # to prevent Whisper concatenating both speakers into a single segment.
    segments = transcript.get("segments", [])
    response = " ".join(s["text"].strip() for s in segments
                        if s.get("end", 0) > s.get("start", 0)
                        and s.get("no_speech_prob", 1) < .8
                        and s.get("avg_logprob", -10) > -1.2)
    asr = re.findall(r"结果: (.*)", log_text)
    state = after.get("perception", {}).get(device, {})
    tts = "SentenceType.FIRST" in log_text or "发送第一段语音:" in log_text
    tts_edges = re.findall(r"SentenceType\.(FIRST|LAST)\b|(发送第一段语音:)", log_text)
    tts_completed = bool(tts_edges) and tts_edges[-1][0] == "LAST"
    # One recognition must contain the request; unrelated log lines cannot pool
    # their words into evidence that Dotty understood this prompt.
    asr_ok = any(matches(item, case.get("asr", [])) for item in asr)
    response_ok = bool(response) and matches(response, case.get("reply", []))
    word_count = len(re.findall(r"\b\w+(?:['’]\w+)*\b", response))
    word_limit = case.get("max_response_words")
    if word_limit is not None and (type(word_limit) is not int or word_limit < 1):
        raise ValueError("max_response_words must be a positive integer")
    within_limit = word_limit is None or word_count <= word_limit
    expected_tool = case.get("expected_tool")
    if expected_tool is not None and (not isinstance(expected_tool, str) or not expected_tool.strip()):
        raise ValueError("expected_tool must name one tool")
    # This concrete PiClient marker records model tool-call construction. It
    # cannot prove execution or a successful result, and a name mentioned in
    # an ASR transcript or assistant answer must not satisfy it.
    tool_calls = re.findall(r"PiClient: tool call name=([a-z_][a-z0-9_]*) id=([^\s]+)", log_text)
    observed_tools = [name for name, ident in tool_calls if ident != "unknown"]
    tool_observed = expected_tool in observed_tools if expected_tool else None
    recovered = (not after.get("errors") and after.get("services_running", False)
                 and not state.get("sensor_stale", True)
                 and state.get("current_state") in case.get("resting_states", ["idle"])
                 and state.get("listening") is False)
    if case.get("recovery_policy") == "ready_for_followup":
        # The deployed voice path intentionally leaves automatic listening open.
        # Count readiness only with an ended TTS turn and a fresh live state.
        recovered = (not after.get("errors") and after.get("services_running", False)
                     and not state.get("sensor_stale", True)
                     and tts_completed
                     and ((state.get("current_state") == "idle" and state.get("listening") is False)
                          or (state.get("current_state") == "talk" and state.get("listening") is True)))
    failure = None
    if not asr:
        failure = "no_asr_or_no_wake"
    elif not asr_ok:
        failure = "asr_mismatch"
    elif not tts:
        failure = "no_tts"
    elif not response:
        failure = "no_audible_response"
    elif not response_ok:
        failure = "response_mismatch"
    elif not within_limit:
        failure = "response_too_long"
    elif not recovered:
        failure = "not_ready_for_followup" if case.get("recovery_policy") == "ready_for_followup" else "no_idle_recovery"
    interaction = "FAIL" if failure else "PASS"
    if failure is None and expected_tool and not tool_observed:
        # Absence of a marker may mean the deployed provider lacks this logger;
        # do not turn uncertain instrumentation into a product failure or pass.
        interaction = "INCONCLUSIVE"
    return {"asr": asr, "asr_match": asr_ok, "response_transcript": response,
            "response_match": response_ok, "tts_evidence": tts, "tts_completed": tts_completed,
            "response_word_count": word_count, "max_response_words": word_limit,
            "response_length": "PASS" if within_limit else "FAIL",
            "semantic_quality": "INCONCLUSIVE",
            "semantic_note": "Keyword/length checks do not establish creative quality or complete semantic correctness.",
            "observed_tool_calls": observed_tools,
            "expected_tool_call": "PASS" if tool_observed else "INCONCLUSIVE",
            "tool_execution": "INCONCLUSIVE",
            "tool_note": "Expected call unverified; logger availability and execution/result require review."
                         if expected_tool and not tool_observed else "Call markers alone do not prove tool execution/result.",
            "expression": "INCONCLUSIVE",
            "recovery": "PASS" if recovered else "FAIL", "failure": failure,
            "interaction": interaction,
            "visual": "INCONCLUSIVE", "verdict": "FAIL" if failure else "INCONCLUSIVE",
            "note": "Visual assertion requires frame review; automated acoustic checks are provisional."}


def space_ok(path):
    usage = shutil.disk_usage(path)
    return usage.free >= max(10 * 1024**3, usage.total * .1)


def guarded_process(argv, logfile, session, timeout, env=None, deadline=None):
    if deadline is not None and time.time() >= deadline:
        raise TimeoutError("session deadline reached before process launch")
    with logfile.open("w") as log:
        p = subprocess.Popen([str(a) for a in argv], stdout=log, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        started = time.monotonic()
        heartbeat = started
        try:
            while p.poll() is None:
                if STOP or (session / "STOP").exists():
                    raise InterruptedError("operator stopped session")
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"process exceeded {timeout}s")
                if deadline is not None and time.time() >= deadline:
                    raise TimeoutError("session deadline reached")
                if not space_ok(session):
                    raise RuntimeError("disk pressure")
                if time.monotonic() - heartbeat >= 5:
                    status = load(session / "status.json", {})
                    write_json(session / "status.json", {**status, "heartbeat": now(),
                               "stage": logfile.stem, "pid": os.getpid()})
                    heartbeat = time.monotonic()
                time.sleep(.25)
            if p.returncode:
                raise RuntimeError(f"process exit {p.returncode}; see {logfile.name}")
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()


def read_log_window(host, start, end, directory):
    errors = []
    for service in SERVICES:
        # Bounded history. Separate stdout/stderr merged by docker logs on remote.
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
                                 shlex.join(["docker", "logs", "--timestamps", "--since", start,
                                             "--until", end, "--tail", "3000", service])],
                                capture_output=True, text=True, timeout=20)
        (directory / f"{service}.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            errors.append(service)
    if errors:
        raise RuntimeError("log_collection_failed:" + ",".join(errors))


def playback_window(directory, media_seconds):
    def stamp(name):
        return datetime.fromisoformat((directory / name).read_text().strip().replace("Z", "+00:00"))
    launched = stamp("raw.recording-start.txt")
    started = stamp("raw.playback-start.txt")
    ended = stamp("raw.playback-end.txt")
    start = (started - launched).total_seconds()
    end = (ended - launched).total_seconds()
    if not 0 <= start < end < end + .4 < media_seconds:
        raise ValueError("invalid_or_truncated_playback_window")
    # Launch precedes the first camera sample, so this response cut is late,
    # never early. A small tail margin excludes speaker reverberation.
    return start, end + .4


def host_clock(host):
    started = time.time()
    remote = datetime.fromisoformat(ssh(host, ["date", "-u", "+%FT%T.%NZ"]).strip().replace("Z", "+00:00")).timestamp()
    ended = time.time()
    return {"offset_seconds": remote - (started + ended) / 2,
            "uncertainty_seconds": (ended - started) / 2}


def host_timestamp(local_iso, clock, margin=0):
    stamp = datetime.fromisoformat(local_iso).timestamp() + clock["offset_seconds"] + margin
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def run_case(session, config, case):
    ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + case["id"] + "-" + uuid.uuid4().hex[:6]
    directory = session / "cases" / ident
    directory.mkdir(parents=True)
    result = {"id": ident, "case_id": case["id"], "case": case, "started": now(),
              "commit": command(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(),
              "capture": "INCONCLUSIVE", "playback": "INCONCLUSIVE",
              "interaction": "INCONCLUSIVE", "recovery": "INCONCLUSIVE",
              "visual": "INCONCLUSIVE", "verdict": "INCONCLUSIVE"}
    write_json(directory / "result.json", result)
    write_json(session / "active.json", {"case": ident, "case_id": case["id"], "started": result["started"]})
    journal(session, {"event": "case_started", "id": ident})
    try:
        before = snapshot(config["host"])
        write_json(directory / "before.json", before)
        if before["errors"] or not before.get("services_running") or config["device"] not in before.get("devices", []):
            raise RuntimeError("infrastructure_down_or_device_disconnected")
        sink = command(["pactl", "get-default-sink"]).strip()
        if sink != config["sink"]:
            raise RuntimeError("speaker_sink_changed")
        volume = command(["pactl", "get-sink-volume", sink])
        channels = re.findall(r"(\d+)%", volume)
        if not channels or any(int(p) != config["volume"] for p in channels):
            raise RuntimeError("speaker_volume_changed")
        if command(["pactl", "get-sink-mute", sink]).strip() != "Mute: no":
            raise RuntimeError("speaker_muted_or_unknown")
        env = dict(os.environ, DOTTY_AV_VOLUME=str(config["volume"]),
                   DOTTY_AV_ALLOW_HIGH_VOLUME="1", DOTTY_AV_SINK=sink,
                   DOTTY_AV_MAX_SECONDS="180")
        media = directory / "raw.mp4"
        result["host_clock"] = host_clock(config["host"])
        result["capture_started"] = now()
        guarded_process([ROOT / "scripts/dotty-av-test.sh", "run", case["prompt"],
                         case.get("seconds", 50), media], directory / "capture.log", session, 185, env,
                        datetime.fromisoformat(config["stop_prompts_at"]).timestamp())
        result["capture_ended"] = now()
        # Snapshot before transcription: evaluator runtime must not give a stuck
        # robot extra minutes to recover unnoticed.
        after = snapshot(config["host"])
        write_json(directory / "after.json", after)
        clock = result["host_clock"]
        read_log_window(config["host"],
                        host_timestamp(result["capture_started"], clock, -clock["uncertainty_seconds"]),
                        host_timestamp(result["capture_ended"], clock, clock["uncertainty_seconds"]), directory)
        result["media"] = verify(media, expected=case.get("seconds", 50))
        result["capture"] = "PASS"
        result["playback_process"] = "PASS"
        # Compute actual playback offset from persisted timestamps and recording
        # duration. Crop conservatively after playback, never use prompt audio as answer.
        prompt_offset, offset = playback_window(directory, result["media"]["audio_seconds"])
        result["response_offset"] = offset
        result["timing_note"] = "Recording-launch timestamp precedes first sample; response crop is conservatively late. Early reply/latency may be lost."
        for label, options in (("response", ["-ss", str(offset)]),
                               ("prompt-heard", ["-ss", "0", "-t", str(offset - .4)])):
            wav = directory / f"{label}.wav"
            command(["ffmpeg", "-nostdin", "-n", "-v", "error", *options, "-i", media,
                     "-vn", "-ar", "16000", "-ac", "1", wav])
            guarded_process([config["python"], ROOT / "scripts/dotty_av_media.py", "transcribe", wav,
                            "--model", config["model"], "--output", directory / f"{label}.json"],
                            directory / f"{label}-transcribe.log", session, 120,
                            deadline=datetime.fromisoformat(config["finish_at"]).timestamp())
        heard = " ".join(s["text"] for s in load(directory / "prompt-heard.json")["segments"])
        result["prompt_heard"] = heard
        result["playback"] = "PASS" if heard.strip() and matches(heard, case.get("asr", [])) else "INCONCLUSIVE"
        result.update(evaluate(case, load(directory / "response.json"),
                               (directory / "xiaozhi-esp32-server.log").read_text(),
                               offset, after, config["device"]))
        if result["playback"] != "PASS" and result["interaction"] == "PASS":
            result.update(interaction="INCONCLUSIVE", failure="prompt_playback_unverified")
        for label, seconds in (("prompt", 3), ("response", offset + 2),
                               ("final", max(0, result["media"]["video_seconds"] - 2))):
            command(["ffmpeg", "-nostdin", "-n", "-v", "error", "-ss", str(seconds),
                     "-i", media, "-frames:v", "1", directory / f"frame-{label}.jpg"])
    except Exception as exc:
        result.update(verdict="FAIL", failure=str(exc), exception=type(exc).__name__)
    finally:
        result["ended"] = now()
        write_json(directory / "result.json", result)
        # Caller clears active only after checkpoint fsync. A crash between these
        # writes otherwise replays a potentially side-effectful successful case.
        journal(session, {"event": "case_finished", "id": ident, "verdict": result["verdict"],
                          "failure": result.get("failure")})
    return result


def report(session):
    results = [load(p) for p in sorted((session / "cases").glob("*/result.json"))]
    rows = []
    for r in results:
        rel = "cases/" + r["id"]
        rows.append(f'<tr><td>{html.escape(r["case_id"])}</td><td>{r["verdict"]}</td>'
                    f'<td>{html.escape(r.get("failure") or r.get("response_transcript", ""))}</td>'
                    f'<td><a href="{rel}/raw.mp4">Video</a> · <a href="{rel}/result.json">Evidence</a></td></tr>')
    (session / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>Dotty overnight review</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:2em auto}td,th{padding:.7em;border-bottom:1px solid #ccc}</style>'
        '<h1>Dotty overnight review</h1><p>AI-assisted: OpenAI Codex (GPT-6). '
        'Automated acoustic verdicts are provisional. Missing visual/physical coverage is not a pass.</p>'
        '<p><a href="coverage.json">Coverage</a> · <a href="status.json">Status</a></p>'
        '<table><tr><th>Case</th><th>Overall</th><th>Finding</th><th>Review</th></tr>' + ''.join(rows) + '</table>')
    write_json(session / "summary.json", {"at": now(), "attempts": len(results),
        "interaction_passes": sum(r.get("interaction") == "PASS" for r in results),
        "failures": sum(r["verdict"] == "FAIL" for r in results),
        "inconclusive": sum(r["verdict"] == "INCONCLUSIVE" for r in results)})


def preflight(session, config):
    data = {"at": now(), "disk_ok": space_ok(session), "snapshot": snapshot(config["host"])}
    for binary in ("ffmpeg", "ffprobe", "espeak-ng", "pw-play", "pactl", "flock"):
        data[binary] = shutil.which(binary)
    data["model_exists"] = (Path(config["model"]) / "model.bin").is_file()
    data["camera_exists"] = Path("/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_7B90DC9F-video-index0").exists()
    write_json(session / "preflight.json", data)
    return data


def lock(session):
    f = (session / "runner.lock").open("w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


@contextlib.contextmanager
def device_lock(config):
    # All sessions targeting this physical robot serialize, including different
    # output directories. The shell separately locks the shared camera.
    key = hashlib.sha256(config["device"].encode()).hexdigest()[:20]
    path = Path("/tmp") / f"dotty-overnight-device-{key}.lock"
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def restore_checkpoint(session):
    checkpoint = load(session / "checkpoint.json", {})
    for key, value in {"completed": [], "quarantined": [], "consecutive_failures": 0,
                       "soak": 0, "counts": {}}.items():
        checkpoint.setdefault(key, value)
    active = load(session / "active.json", {})
    if active:
        if checkpoint.get("last_attempt") == active["case"]:
            write_json(session / "active.json", {})
            return checkpoint
        path = session / "cases" / active["case"] / "result.json"
        result = load(path, {})
        case_id = active.get("case_id") or result.get("case_id")
        if not case_id:
            raise RuntimeError("interrupted case identity unknown; manual evidence review required")
        if case_id not in checkpoint["quarantined"]:
            checkpoint["quarantined"].append(case_id)
        if result:
            result.update(verdict="INCONCLUSIVE", failure="interrupted_case_requires_review",
                          ended=now())
            write_json(path, result)
        journal(session, {"event": "interrupted_case_requires_review", **active})
        # Persist quarantine before clearing active: a second crash cannot lose it.
        write_json(session / "checkpoint.json", checkpoint)
        write_json(session / "active.json", {})
    return checkpoint


def record_attempt(checkpoint, case, result):
    if result.get("id"):
        checkpoint["last_attempt"] = result["id"]
    counts = checkpoint["counts"].setdefault(case["id"], {"attempts": 0, "streak": 0, "failures": 0})
    counts["attempts"] += 1
    acoustic_pass = all(result.get(k) == "PASS" for k in ("capture", "playback", "interaction", "recovery"))
    counts["streak"] = counts["streak"] + 1 if acoustic_pass else 0
    if not acoustic_pass:
        counts["failures"] += 1
    checkpoint["consecutive_failures"] = 0 if acoustic_pass else checkpoint["consecutive_failures"] + 1
    # Acoustic completion remains provisional while the visual verdict is open.
    if counts["streak"] >= case.get("required_passes", 3) and case["id"] not in checkpoint["completed"]:
        checkpoint["completed"].append(case["id"])
    if counts["failures"] >= case.get("max_failures", 3) and case["id"] not in checkpoint["quarantined"]:
        checkpoint["quarantined"].append(case["id"])
    if result.get("exception") in {"InterruptedError", "TimeoutError"} and case["id"] not in checkpoint["quarantined"]:
        checkpoint["quarantined"].append(case["id"])


def run(session, config, catalogue, selected=None, once=False):
    with lock(session), device_lock(config):
        checkpoint = restore_checkpoint(session)
        deadline = datetime.fromisoformat(config["stop_prompts_at"]).timestamp()
        rng = random.Random(config["seed"] + checkpoint["soak"])
        bank = [c for c in catalogue if c.get("enabled", True) and (not selected or c["id"] in selected)]
        if not bank:
            raise ValueError("no selected executable cases")
        previous = time.monotonic()
        try:
            while not STOP and not (session / "STOP").exists() and time.time() < deadline:
                write_json(session / "status.json", {"state": "running", "heartbeat": now(), "pid": os.getpid(),
                    "checkpoint": checkpoint, "stop_prompts_at": config["stop_prompts_at"]})
                if (session / "PAUSE").exists():
                    time.sleep(1)
                    continue
                if checkpoint["consecutive_failures"] >= 3:
                    write_json(session / "PAUSE", {"reason": "three consecutive failures require agent diagnosis"})
                    write_json(session / "checkpoint.json", checkpoint)
                    continue
                eligible = [c for c in bank if c["id"] not in checkpoint["quarantined"]]
                pending = [c for c in eligible if c["id"] not in checkpoint["completed"]]
                if pending:
                    case = pending[0]
                else:
                    soak_bank = [c for c in eligible if c.get("soak_safe", False)]
                    if not soak_bank:
                        journal(session, {"event": "no_eligible_soak_cases"})
                        break
                    due = previous + config.get("interval_seconds", 600)
                    if time.monotonic() < due:
                        time.sleep(min(1, due - time.monotonic()))
                        continue
                    case = soak_bank[0] if checkpoint["soak"] % 6 == 0 else rng.choice(soak_bank)
                    checkpoint["soak"] += 1
                # Reserve enough time for capture and both transcriptions before cutoff.
                if deadline - time.time() < 360:
                    break
                if not space_ok(session):
                    raise RuntimeError("disk pressure")
                result = run_case(session, config, case)
                previous = time.monotonic()
                record_attempt(checkpoint, case, result)
                write_json(session / "checkpoint.json", checkpoint)
                write_json(session / "active.json", {})
                report(session)
                print(json.dumps({"case": case["id"], "verdict": result["verdict"], "failure": result.get("failure")}), flush=True)
                if once:
                    break
                # Quiet gap between coverage cases; interruptible, no prompt overlap.
                for _ in range(20):
                    if STOP or (session / "STOP").exists():
                        break
                    time.sleep(1)
        finally:
            write_json(session / "status.json", {"state": "stopped", "at": now(), "checkpoint": checkpoint})
            report(session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "preflight", "run", "resume", "status", "stop", "report", "admin"])
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--host", default="root@tower")
    parser.add_argument("--device")
    parser.add_argument("--cases", help="comma-separated case IDs")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--route")
    parser.add_argument("--body", help="JSON object; tokens are resolved remotely")
    args = parser.parse_args()
    session = args.session.resolve()
    session.mkdir(parents=True, exist_ok=True)
    if args.command == "init":
        if (session / "config.json").exists():
            raise ValueError("session already initialized")
        morning = datetime.now(TZ).replace(hour=8, minute=0, second=0, microsecond=0)
        if morning <= datetime.now(TZ):
            morning += timedelta(days=1)
        devices = admin(args.host, "devices")["devices"]
        device = args.device or (devices[0] if len(devices) == 1 else None)
        if not device:
            raise ValueError("select exactly one live device")
        config = {"host": args.host, "device": device,
            "sink": command(["pactl", "get-default-sink"]).strip(), "volume": 100,
            "python": str(session / "evaluator-venv/bin/python"),
            "model": str(session / "models/whisper-small.en-ct2"),
            "seed": 20260912, "interval_seconds": 600,
            "stop_prompts_at": (morning - timedelta(minutes=30)).isoformat(),
            "finish_at": morning.isoformat(), "created": now(), "framing": "BLOCKED"}
        write_json(session / "config.json", config)
        for folder in ("clips/ready-for-review", "clips/needs-review", "clips/failures", "experiments", "logs"):
            (session / folder).mkdir(parents=True, exist_ok=True)
        write_json(session / "coverage.json", load(ROOT / "scripts/dotty-av-cases.json"))
        print(json.dumps(config, indent=2))
        return
    config = load(session / "config.json")
    if args.command == "admin":
        print(json.dumps(admin(config["host"], args.route, json.loads(args.body) if args.body else None)))
    elif args.command == "preflight":
        print(json.dumps(preflight(session, config), indent=2))
    elif args.command in {"run", "resume"}:
        if args.command == "resume":
            # Explicit invocation is the agent's decision to resume after review.
            with lock(session):
                for name in ("STOP", "PAUSE"):
                    (session / name).unlink(missing_ok=True)
                checkpoint = restore_checkpoint(session)
                checkpoint["consecutive_failures"] = 0
                write_json(session / "checkpoint.json", checkpoint)
        run(session, config, load(ROOT / "scripts/dotty-av-cases.json")["cases"],
            args.cases.split(",") if args.cases else None, args.once)
    elif args.command == "stop":
        write_json(session / "STOP", {"at": now()})
    elif args.command == "status":
        print(json.dumps(load(session / "status.json", {}), indent=2))
    else:
        report(session)


if __name__ == "__main__":
    def stop_signal(signum, frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    main()
