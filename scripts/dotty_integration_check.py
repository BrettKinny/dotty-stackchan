#!/usr/bin/env python3
"""Non-acoustic Dotty integration check over SSH. AI-assisted: Claude (Opus 5.5).

Exercises the xiaozhi admin routes, dotty-behaviour and the bridge dashboard on
the Docker host named by --host (or DOTTY_TEST_HOST), then prints a pass/fail
table. It never plays audio, opens the microphone (no say/inject-text) or
records. Every setting it changes is restored in a finally block:

- head: one small bounded yaw nudge, then back to the home pose (0, 0);
- toggles: each firmware pip flipped, then set back to the guardian policy value
  read from the bridge state file (the policy file itself is never written);
- state: set-state idle (a deliberate sticky state is restored afterwards).

The admin token never leaves the xiaozhi container: requests run inside it with
`docker exec` and read DOTTY_ADMIN_TOKEN from the container environment (the
same value `docker exec xiaozhi-esp32-server printenv DOTTY_ADMIN_TOKEN`
prints). The token is never printed, logged or passed in argv.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.parse

SERVICES = ("xiaozhi-esp32-server", "dotty-behaviour", "dotty-bridge", "dotty-pi")
STATES = ("idle", "talk", "story_time", "security", "sleep", "dance")
STICKY = ("story_time", "security", "sleep", "dance")
STATE_DIR = "/var/lib/dotty-bridge/state"
HEAD_NUDGE = {"yaw": 10, "pitch": 0, "speed": 150}
HEAD_HOME = {"yaw": 0, "pitch": 0, "speed": 150}
TOOLS = {"memory_lookup", "remember", "recall_person", "remember_person",
         "think_hard", "take_photo", "play_song"}

# Runs inside xiaozhi-esp32-server. Token comes from the container environment.
_ADMIN_CODE = '''import json,os,sys,urllib.request,urllib.error
method, route, body, with_token = json.loads(sys.argv[1])
headers={"Content-Type":"application/json"}
token=os.environ.get("DOTTY_ADMIN_TOKEN","").strip()
if with_token and token: headers["X-Admin-Token"]=token
req=urllib.request.Request("http://127.0.0.1:8003/xiaozhi/admin/"+route, method=method,
 data=None if body is None else json.dumps(body).encode(), headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10) as r: status, text = r.status, r.read().decode()
except urllib.error.HTTPError as e: status, text = e.code, e.read().decode()
try: payload=json.loads(text)
except ValueError: payload={"raw": text[:200]}
print(json.dumps({"status": status, "body": payload, "token_configured": bool(token)}))
'''


class Remote:
    def __init__(self, host, timeout=30):
        self.host = host
        self.timeout = timeout

    def run(self, argv, input=None, check=True, timeout=None):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host,
             shlex.join([str(a) for a in argv])],
            input=input, capture_output=True, text=True, timeout=timeout or self.timeout)
        if check and result.returncode:
            raise RuntimeError(f"remote exit {result.returncode}: {result.stderr.strip()[:160]}")
        return result

    def get_json(self, url):
        return json.loads(self.run(["curl", "-fsS", "--max-time", "8", url]).stdout)

    def http_status(self, url):
        out = self.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                        "--max-time", "8", url], check=False).stdout.strip()
        return int(out) if out.isdigit() else 0

    def admin(self, method, route, body=None, with_token=True):
        out = self.run(["docker", "exec", "-i", "xiaozhi-esp32-server", "python", "-",
                        json.dumps([method, route, body, with_token])], input=_ADMIN_CODE).stdout
        return json.loads(out)


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail=""):
        self.rows.append({"check": name, "status": status, "detail": str(detail)})
        return status == "PASS"

    def run(self, name, fn):
        try:
            outcome = fn()
        except Exception as exc:  # each check reports, never aborts the run
            return self.add(name, "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")
        status, detail = outcome if isinstance(outcome, tuple) else (outcome, "")
        return self.add(name, status, detail)

    @property
    def failed(self):
        return any(row["status"] == "FAIL" for row in self.rows)

    def table(self):
        width = max([len(r["check"]) for r in self.rows] + [5])
        lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL"]
        lines += [f"{r['check'].ljust(width)}  {r['status'].ljust(6)}  {r['detail']}" for r in self.rows]
        return "\n".join(lines)


def ok_response(response, name):
    if response["status"] != 200 or not response["body"].get("ok", True):
        raise RuntimeError(f"{name}: HTTP {response['status']} {json.dumps(response['body'])[:120]}")
    return response["body"]


def device_state(remote, device):
    state = remote.get_json("http://localhost:8090/api/perception/state?device_id="
                            + urllib.parse.quote(device, safe=""))
    return state.get(device, {})


def recent_events(remote, device):
    return remote.get_json("http://localhost:8090/api/perception/recent/"
                           + urllib.parse.quote(device, safe="") + "?limit=50")


def host_now(remote):
    return float(remote.run(["date", "+%s.%N"]).stdout.strip())


def set_state_verified(remote, device, target, timeout=12):
    """set-state, then require a fresh firmware state_changed echo and current_state."""
    sent = host_now(remote) - 1
    ok_response(remote.admin("POST", "set-state", {"device_id": device, "state": target}), "set-state")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        echoed = [e for e in recent_events(remote, device)
                  if e.get("name") == "state_changed" and (e.get("data") or {}).get("state") == target
                  and float(e.get("ts") or 0) >= sent]
        if echoed and device_state(remote, device).get("current_state") == target:
            return round(float(echoed[0]["ts"]) - sent, 1)
        time.sleep(1)
    raise RuntimeError(f"no state_changed={target} echo within {timeout}s")


def read_policy(remote, name):
    out = remote.run(["docker", "exec", "dotty-bridge", "cat", f"{STATE_DIR}/{name}"]).stdout.strip().lower()
    if out in ("true", "1", "yes"):
        return True
    if out in ("false", "0", "no"):
        return False
    raise RuntimeError(f"unparseable {name} policy value")


def check(remote, device, skip_photo=False, skip_motion=False):
    checks = Checks()
    context = {}

    def containers():
        raw = remote.run(["docker", "inspect", "--format", "{{.Name}} {{.State.Running}} {{.RestartCount}}",
                          *SERVICES]).stdout.split("\n")
        lines = [line for line in raw if line.strip()]
        down = [line for line in lines if " true " not in line + " "]
        return ("PASS" if len(lines) == 4 and not down else "FAIL"), "; ".join(lines)
    checks.run("containers running", containers)

    for name, url in (("bridge /health", "http://localhost:8081/health"),
                      ("behaviour /health", "http://localhost:8090/health")):
        checks.run(name, lambda url=url: ("PASS", json.dumps(remote.get_json(url))[:100]))
    for name, url in (("dashboard /ui", "http://localhost:8081/ui"),
                      ("behaviour vision cache", "http://localhost:8090/api/vision/cache"),
                      ("behaviour audio cache", "http://localhost:8090/api/audio/cache"),
                      ("behaviour scene synthesis", "http://localhost:8090/api/scene-synthesis/recent")):
        checks.run(name, lambda url=url: ("PASS" if remote.http_status(url) == 200 else "FAIL",
                                          f"HTTP {remote.http_status(url)}"))

    def auth():
        response = remote.admin("GET", "devices", with_token=False)
        if not response["token_configured"]:
            return "FAIL", "DOTTY_ADMIN_TOKEN not set in xiaozhi container: admin routes are open"
        return ("PASS" if response["status"] == 401 else "FAIL"), f"tokenless request -> HTTP {response['status']}"
    checks.run("admin rejects missing token", auth)

    def devices():
        listed = ok_response(remote.admin("GET", "devices"), "devices")["devices"]
        context["devices"] = listed
        if device not in listed:
            return "FAIL", f"{device} not connected; listed {listed}"
        extra = [d for d in listed if d != device]
        return "PASS", "connected" + (f"; extra devices {extra} (cold-wake attribution blocked)" if extra else "")
    if not checks.run("admin devices", devices):
        checks.add("device-dependent checks", "SKIP", "target device not connected")
        return checks

    def perception():
        state = device_state(remote, device)
        context["state"] = state.get("current_state")
        return ("FAIL" if state.get("sensor_stale", True) else "PASS",
                f"state={state.get('current_state')} listening={state.get('listening')} "
                f"age={round(state.get('sensor_age_s') or -1, 1)}s")
    checks.run("perception state", perception)

    def songs():
        files = ok_response(remote.admin("GET", "songs"), "songs").get("files", [])
        return ("PASS" if files else "FAIL"), f"{len(files)} files: {', '.join(files[:8])}"
    checks.run("admin songs", songs)

    def tool_logger():
        found = remote.run(["docker", "exec", "xiaozhi-esp32-server", "grep", "-rqs",
                            "PiClient: tool call name", "/opt/xiaozhi-esp32-server/core/providers/llm/pi_voice/"],
                           check=False).returncode == 0
        return ("PASS" if found else "FAIL"), "deployed PiClient logs tool calls" if found else "marker absent"
    checks.run("pi tool-call logger deployed", tool_logger)

    def asr_warmup():
        # WhisperLocal transcribes 1 s of silence on its device at startup; a
        # missing CUDA library fails there and then on every utterance, while
        # every other check still passes.
        lines = remote.run(["sh", "-c", "docker logs xiaozhi-esp32-server 2>&1 | grep -E "
                            "'Loading faster-whisper model|faster-whisper warm-up|libcublas|libcudnn' | tail -40"],
                           check=False).stdout.splitlines()
        loads = [i for i, line in enumerate(lines) if "Loading faster-whisper model" in line]
        if not loads:
            return "SKIP", "WhisperLocal not loaded in this container's log"
        since = lines[loads[-1]:]
        device = since[0].split("device=")[1].split()[0] if "device=" in since[0] else "?"
        failed = [line for line in since if "warm-up failed" in line or "libcublas" in line or "libcudnn" in line]
        if failed:
            return "FAIL", failed[0].split("-WARNING-")[-1][-160:]
        done = [line for line in since if "warm-up complete" in line]
        return ("PASS", f"warm-up transcribed on {device}") if done else ("FAIL", "no warm-up result after model load")
    checks.run("WhisperLocal warm-up", asr_warmup)

    def pi_tools():
        listed = set(remote.run(["docker", "exec", "dotty-pi", "ls",
                                 "/root/.pi/extensions/dotty-pi-ext/src/tools"]).stdout.split())
        names = {n.removesuffix(".ts") for n in listed if n.endswith(".ts")}
        missing = sorted(TOOLS - names)
        return ("FAIL", f"missing {missing}") if missing else ("PASS", "7 voice tools present")
    checks.run("dotty-pi extension tools", pi_tools)

    state_now = context.get("state")
    if skip_motion:
        checks.add("set-head-angles", "SKIP", "--skip-motion")
    elif state_now not in ("idle", "talk"):
        checks.add("set-head-angles", "SKIP", f"robot in {state_now}; motion not safe/owned")
    else:
        def head():
            try:
                ok_response(remote.admin("POST", "set-head-angles", {"device_id": device, **HEAD_NUDGE}), "nudge")
                time.sleep(2)
            finally:
                ok_response(remote.admin("POST", "set-head-angles", {"device_id": device, **HEAD_HOME}), "home")
            return "PASS", "yaw 10 then home (0,0); no readback route, review video for motion"
        checks.run("set-head-angles (bounded, restored)", head)

    for toggle, policy_file in (("smart_mode", "smart-mode"), ("kid_mode", "kid-mode")):
        def flip(toggle=toggle, policy_file=policy_file):
            policy = read_policy(remote, policy_file)
            try:
                ok_response(remote.admin("POST", "set-toggle", {"device_id": device, "name": toggle,
                                                               "enabled": not policy}), "flip")
                time.sleep(2)
            finally:
                ok_response(remote.admin("POST", "set-toggle", {"device_id": device, "name": toggle,
                                                               "enabled": policy}), "restore")
            return "PASS", f"pip {not policy} then restored to policy {policy}; pip needs visual review"
        checks.run(f"set-toggle {toggle} (restored)", flip)

    def bad_toggle():
        response = remote.admin("POST", "set-toggle", {"device_id": device, "name": "nonsense", "enabled": True})
        return ("PASS" if response["status"] == 400 else "FAIL"), f"HTTP {response['status']}"
    checks.run("set-toggle rejects unknown name", bad_toggle)

    if skip_photo:
        checks.add("take-photo -> vision cache", "SKIP", "--skip-photo")
    else:
        def photo():
            sent = host_now(remote) - 1
            ok_response(remote.admin("POST", "take-photo", {"device_id": device,
                        "question": "Integration check: describe the scene in one sentence."}), "take-photo")
            end = time.monotonic() + 30
            while time.monotonic() < end:
                entry = remote.get_json("http://localhost:8090/api/vision/cache").get(device) or {}
                if float(entry.get("wall_ts") or 0) >= sent and entry.get("description"):
                    # Description content stays on the host: the room may be private.
                    return "PASS", f"fresh description ({len(entry['description'])} chars)"
                time.sleep(2)
            return "FAIL", "no fresh vision cache entry within 30s"
        checks.run("take-photo -> vision cache", photo)

    def state_idle():
        prior = device_state(remote, device).get("current_state")
        try:
            latency = set_state_verified(remote, device, "idle")
        finally:
            if prior in STICKY:
                set_state_verified(remote, device, prior)
        return "PASS", f"state_changed idle echoed after {latency}s" + (
            f"; restored {prior}" if prior in STICKY else "")
    checks.run("set-state idle (verified echo)", state_idle)

    def bad_state():
        response = remote.admin("POST", "set-state", {"device_id": device, "state": "nonsense"})
        return ("PASS" if response["status"] == 400 else "FAIL"), f"HTTP {response['status']}"
    checks.run("set-state rejects unknown state", bad_state)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("DOTTY_TEST_HOST"),
                        help="SSH user@host of the Docker host (or DOTTY_TEST_HOST)")
    parser.add_argument("--device", help="device id (default: the only connected device)")
    parser.add_argument("--json", type=Path, help="also write the result rows to this file")
    parser.add_argument("--skip-photo", action="store_true", help="do not trigger a camera capture")
    parser.add_argument("--skip-motion", action="store_true", help="do not move the head")
    args = parser.parse_args()
    if not args.host or not args.host.strip():
        parser.error("--host or DOTTY_TEST_HOST is required; no deployment host is assumed")
    remote = Remote(args.host)
    device = args.device
    if not device:
        listed = ok_response(remote.admin("GET", "devices"), "devices")["devices"]
        if len(listed) != 1:
            parser.error(f"pass --device: {len(listed)} devices connected")
        device = listed[0]
    checks = check(remote, device, args.skip_photo, args.skip_motion)
    print(checks.table())
    if args.json:
        args.json.write_text(json.dumps({"host": args.host, "device": device, "at": time.time(),
                                         "rows": checks.rows}, indent=2) + "\n")
    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
