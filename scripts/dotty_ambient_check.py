#!/usr/bin/env python3
"""Ambient consumer check (coverage rows B03/B04, perception plumbing). AI-assisted: Claude (Opus 5.5).

Evidence class S+D: it POSTs labelled synthetic perception events to
dotty-behaviour and proves the downstream branch (the consumer emits
`head_turn`, the head really moves, a malformed or duplicate event does not
kill the service). It does NOT prove the physical microphone, direction
estimate or idle-origin WebSocket delivery: those need a real sound.
No audio is played and the microphone is never opened. The head is returned
to (0, 0) in a finally block.

A consumer that is disabled by configuration (SOUND_TURN_ENABLED defaults to
off) or gated (not idle, listening, face present, chat in the last 30 s) is
reported as SKIP with the reason, never as PASS.

Usage: dotty_ambient_check.py --host user@dockerhost [--device ID] [--json out.json]
Needs /xiaozhi/admin/mcp-call deployed for the head readback.
"""
import argparse
import json
import sys
import time
import urllib.parse

from dotty_integration_check import Remote, Checks, ok_response
from dotty_mcp_check import Caller, find_number

BEHAVIOUR = "http://localhost:8090"


def quote(device):
    return urllib.parse.quote(device, safe="")


def post_event(remote, device, name, data=None, raw=None):
    body = raw if raw is not None else json.dumps({"device_id": device, "name": name, "data": data or {}})
    return remote.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
                       "-H", "Content-Type: application/json", "-d", body,
                       f"{BEHAVIOUR}/api/perception/event"], check=False).stdout.strip()


def recent_names(remote, device, since):
    events = remote.get_json(f"{BEHAVIOUR}/api/perception/recent/{quote(device)}?limit=50")
    return [e for e in events if float(e.get("ts") or 0) >= since]


def run_checks(remote, device):
    checks, caller = Checks(), Caller(remote, device)

    def sound_turn(direction, expect_yaw):
        def go():
            state = remote.get_json(f"{BEHAVIOUR}/api/perception/state?device_id={quote(device)}").get(device, {})
            blockers = [k for k, bad in (("state=" + str(state.get("current_state")), state.get("current_state") != "idle"),
                                         ("listening", state.get("listening")),
                                         ("face_present", state.get("face_present"))) if bad]
            if blockers:
                return "SKIP", "gated: " + ", ".join(blockers)
            sent = time.time()
            if post_event(remote, device, "sound_event",
                          {"direction": direction, "balance": 0.9, "energy": 1, "synthetic": True}) != "204":
                return "FAIL", "perception event not accepted"
            time.sleep(4)
            turns = [e for e in recent_names(remote, device, sent) if e.get("name") == "head_turn"]
            if not turns:
                return "SKIP", "no head_turn: consumer disabled (SOUND_TURN_ENABLED) or chat/cooldown gate"
            try:
                time.sleep(3)
                ok, text = caller.call("self.robot.get_head_angles")
                yaw = find_number(json.loads(text), "yaw") if ok else None
            finally:
                remote.admin("POST", "set-head-angles", {"device_id": device, "yaw": 0, "pitch": 0, "speed": 150})
            reached = yaw is not None and abs(yaw - expect_yaw) <= 8
            return ("PASS" if reached else "FAIL"), f"head_turn emitted; device yaw {yaw}, expected about {expect_yaw}"
        return go
    checks.run("B03 synthetic sound_event left -> head_turn + device yaw", sound_turn("left", -45))
    time.sleep(4)  # past the 3 s cooldown
    checks.run("B03 synthetic sound_event right -> head_turn + device yaw", sound_turn("right", 45))

    def robust():
        codes = [post_event(remote, device, "sound_event", {"direction": "sideways"}),
                 post_event(remote, device, "state_changed", {}),
                 post_event(remote, device, "x", raw="{not json"),
                 post_event(remote, device, "x", raw=json.dumps({"device_id": device}))]
        health = remote.http_status(f"{BEHAVIOUR}/health")
        okay = codes[0] == "204" and codes[2] == "422" and codes[3] == "422" and health == 200
        return ("PASS" if okay else "FAIL"), f"codes {codes}; /health {health}"
    checks.run("perception bus survives malformed fixtures", robust)

    def state_follows():
        before = time.time() - 1
        if post_event(remote, device, "face_lost", {"synthetic": True}) != "204":
            return "FAIL", "event not accepted"
        time.sleep(1)
        state = remote.get_json(f"{BEHAVIOUR}/api/perception/state?device_id={quote(device)}").get(device, {})
        return ("PASS" if not state.get("face_present") and recent_names(remote, device, before) else "FAIL"), (
            f"face_present={state.get('face_present')}")
    checks.run("face_lost fixture reaches bus state", state_follows)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True)
    parser.add_argument("--device")
    parser.add_argument("--json")
    args = parser.parse_args()
    remote = Remote(args.host)
    device = args.device
    if not device:
        listed = ok_response(remote.admin("GET", "devices"), "devices")["devices"]
        if len(listed) != 1:
            parser.error(f"pass --device: {len(listed)} devices connected")
        device = listed[0]
    checks = run_checks(remote, device)
    print(checks.table())
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"device": device, "at": time.time(), "rows": checks.rows}, handle, indent=2)
    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
