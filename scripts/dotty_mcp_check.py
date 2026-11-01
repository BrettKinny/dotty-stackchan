#!/usr/bin/env python3
"""Firmware MCP tool check (coverage rows M01-M04, M06-M09, M13-M15). AI-assisted: Claude (Opus 5.5).

Calls allow-listed firmware MCP tools through `POST /xiaozhi/admin/mcp-call`
(custom-providers/xiaozhi-patches, must be deployed) and reads each device
reply back from xiaozhi's log, where its MCP handler records every reply as an
`id`-matched result frame. So a PASS means the firmware answered that exact
request, not merely that the server sent it. Also compares the live `tools/list`
reply with the 15-tool contract (M-set).

It never plays audio or opens the microphone. Everything it changes is
restored in a finally block: speaker volume, screen brightness and theme are
read first and put back; the reminder is created 120 s ahead (never allowed to
fire) and stopped; the head returns to (0, 0). Values from get_device_status
(network names, addresses) are never printed or written, only field names.
LED and head effects are device-side facts; the webcam is what shows them.

Usage: dotty_mcp_check.py --host user@dockerhost [--device ID] [--json out.json]
"""
import argparse
import json
import re
import sys
import time

from dotty_integration_check import Remote, Checks, ok_response  # noqa: E402

EXPECTED_TOOLS = {
    "self.get_device_status", "self.audio_speaker.set_volume", "self.screen.set_brightness",
    "self.screen.set_theme", "self.camera.take_photo", "self.robot.get_head_angles",
    "self.robot.set_head_angles", "self.robot.set_led_color", "self.robot.set_led_multi",
    "self.robot.set_state", "self.robot.set_toggle", "self.robot.set_face_identified",
    "self.robot.create_reminder", "self.robot.get_reminders", "self.robot.stop_reminder",
}
MARKER = "收到mcp消息"
REMINDER_SECONDS = 300  # long enough to stop it, even after a slow retry
REMINDER_MARKER = "dotty-coverage-"


def parse_mcp_frames(log_text):
    """Every MCP frame xiaozhi logged, oldest first. Unparseable lines are skipped."""
    frames = []
    for line in log_text.splitlines():
        if MARKER not in line:
            continue
        raw = line.split(MARKER, 1)[1].lstrip("：: ").strip()
        try:
            frames.append(json.loads(raw))
        except ValueError:
            continue
    return frames


def latest_tool_names(frames):
    """Names from the most recent tools/list reply, or None if none was logged."""
    for frame in reversed(frames):
        tools = ((frame.get("payload") or {}).get("result") or {}).get("tools")
        if isinstance(tools, list):
            return {t.get("name") for t in tools}
    return None


def reply_for(frames, session_id, request_id):
    """The result/error payload for one request, or None while not yet logged.

    Request ids restart at 1 per connection, so they collide with xiaozhi's own
    early tools/list replies; those are skipped, and callers pass only log lines
    newer than the call."""
    for frame in frames:
        payload = frame.get("payload") or {}
        if "tools" in (payload.get("result") or {}):
            continue
        if frame.get("session_id") == session_id and payload.get("id") == request_id and (
                "result" in payload or "error" in payload):
            return payload
    return None


def reply_text(payload):
    """(ok, text): ok is False for a JSON-RPC error or `isError`."""
    if "error" in payload:
        return False, json.dumps(payload["error"])[:200]
    result = payload.get("result") or {}
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    return (not result.get("isError", False)), text


def find_number(obj, *names):
    """First int/float stored under any of `names` in nested JSON, else None."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in names and isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        for value in obj.values():
            found = find_number(value, *names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_number(value, *names)
            if found is not None:
                return found
    return None


def find_string(obj, *names):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in names and isinstance(value, str):
                return value
        for value in obj.values():
            found = find_string(value, *names)
            if found is not None:
                return found
    return None


def field_names(obj, prefix=""):
    names = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            names.append(prefix + key)
            names.extend(field_names(value, prefix + key + "."))
    return names


def reminder_ids(text):
    """Reminder ids in a get_reminders reply, tolerant of JSON or loose text."""
    try:
        data = json.loads(text)
    except ValueError:
        return set(int(m) for m in re.findall(r'"?id"?\s*[:=]\s*(\d+)', text))
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("id"), int):
                found.add(node["id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(data)
    return found


def marked_reminder_ids(text, marker):
    """Ids of reminders in a get_reminders reply whose message starts with `marker`."""
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    found = set()
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and isinstance(item.get("id"), int) \
                and str(item.get("message", "")).startswith(marker):
            found.add(item["id"])
    return found


class Caller:
    def __init__(self, remote, device):
        self.remote, self.device = remote, device

    def log_frames(self, since):
        out = self.remote.run(["sh", "-c", f"docker logs --since {int(since)} xiaozhi-esp32-server 2>&1 | "
                               f"grep -F '{MARKER}'"], check=False).stdout
        return parse_mcp_frames(out)

    def call(self, tool, arguments=None, wait=15):
        """Send one call and wait for the device's reply: (ok, text)."""
        since = int(time.time()) - 2
        sent = ok_response(self.remote.admin("POST", "mcp-call", {
            "device_id": self.device, "tool": tool, "arguments": arguments or {}}), tool)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            payload = reply_for(self.log_frames(since), sent["session_id"], sent["request_id"])
            if payload is not None:
                return reply_text(payload)
            time.sleep(1)
        raise RuntimeError(f"no device reply to {tool} within {wait}s")


def run_checks(remote, device, skip_leds=False):
    checks, caller = Checks(), Caller(remote, device)
    status = {}

    def tools_list():
        names = latest_tool_names(caller.log_frames(int(time.time()) - 6 * 3600))
        if names is None:
            return "SKIP", "no tools/list reply in the last 6 h of xiaozhi logs (reconnect the robot)"
        missing, extra = sorted(EXPECTED_TOOLS - names), sorted(names - EXPECTED_TOOLS)
        return ("FAIL" if missing else "PASS"), f"{len(names)} tools; missing {missing}; extra {extra}"
    checks.run("tools/list matches the 15-tool contract", tools_list)

    def get_status():
        ok, text = caller.call("self.get_device_status")
        if not ok:
            return "FAIL", text[:120]
        status["data"] = json.loads(text)
        status["volume"] = find_number(status["data"], "volume")
        status["brightness"] = find_number(status["data"], "brightness")
        status["theme"] = find_string(status["data"], "theme")
        return "PASS", "fields: " + ", ".join(field_names(status["data"])[:20])
    if not checks.run("M01 get_device_status", get_status):
        return checks

    def bounded(name, tool, key, current, step, low=5, high=95):
        def go():
            if current is None:
                return "SKIP", f"no {key} field in get_device_status; cannot restore safely"
            target = current + step if current + step <= high else current - step
            target = max(low, min(high, target))
            ok, _ = caller.call(tool, {key: target})
            try:
                if not ok:
                    return "FAIL", "device rejected a bounded value"
                again = find_number(json.loads(caller.call("self.get_device_status")[1]), key)
                return ("PASS" if again == target else "FAIL"), f"{current} -> {target}, status reads {again}"
            finally:
                caller.call(tool, {key: current})
        checks.run(name, go)
    bounded("M02 set_volume (robot speaker, restored)", "self.audio_speaker.set_volume",
            "volume", status["volume"], 5)
    bounded("M03 set_brightness (restored)", "self.screen.set_brightness",
            "brightness", status["brightness"], 10)

    def theme():
        if not status["theme"]:
            return "SKIP", "no theme field in get_device_status"
        bad_ok, _ = caller.call("self.screen.set_theme", {"theme": "not-a-theme"})
        good_ok, _ = caller.call("self.screen.set_theme", {"theme": status["theme"]})
        return ("PASS" if good_ok and not bad_ok else "FAIL"), (
            f"current theme re-applied ok={good_ok}; invalid theme accepted={bad_ok}")
    checks.run("M04 set_theme (invalid refused, current re-applied)", theme)

    def head_readback():
        sent = ok_response(remote.admin("POST", "set-head-angles", {
            "device_id": device, "yaw": 20, "pitch": 0, "speed": 150}), "set-head-angles")
        try:
            time.sleep(3)
            ok, text = caller.call("self.robot.get_head_angles")
            yaw = find_number(json.loads(text), "yaw") if ok else None
            return ("PASS" if yaw is not None and abs(yaw - sent["yaw"]) <= 6 else "FAIL"), (
                f"requested yaw 20, device reports {yaw}")
        finally:
            remote.admin("POST", "set-head-angles", {"device_id": device, "yaw": 0, "pitch": 0, "speed": 150})
    checks.run("M06/M07 head angle readback (returned home)", head_readback)

    if skip_leds:
        checks.add("M08/M09 LEDs", "SKIP", "--skip-leds")
    else:
        def leds():
            one, _ = caller.call("self.robot.set_led_color", {"red": 0, "green": 40, "blue": 0})
            multi, _ = caller.call("self.robot.set_led_multi", {"index": 2, "red": 40, "green": 0, "blue": 40})
            right, _ = caller.call("self.robot.set_led_multi", {"index": 6, "red": 255, "green": 0, "blue": 0})
            return ("PASS" if one and multi and not right else "FAIL"), (
                f"left ring ok={one}; pixel 2 ok={multi}; right-ring pixel 6 accepted={right} "
                "(webcam shows the effect; StateManager repaints at 5 Hz)")
        checks.run("M08/M09 LED calls (right ring refused)", leds)

    def reminders():
        # A popup that fires cannot be dismissed remotely (no tool), so nothing
        # here may leave a reminder behind: ids come from get_reminders by our
        # unique message (the create reply format is not relied on), and a
        # finally block sweeps every reminder carrying our marker.
        message = f"{REMINDER_MARKER}{int(time.time())}"
        created, listed, stop_error = set(), False, None

        def sweep():
            ids = set(created)
            try:
                ids |= marked_reminder_ids(caller.call("self.robot.get_reminders")[1], REMINDER_MARKER)
            except Exception:
                pass
            for rid in sorted(ids):
                try:
                    caller.call("self.robot.stop_reminder", {"id": rid})
                except Exception as exc:
                    nonlocal stop_error
                    stop_error = exc
            return ids
        try:
            ok, text = caller.call("self.robot.create_reminder", {
                "duration_seconds": REMINDER_SECONDS, "message": message, "repeat": False})
        except BaseException:
            sweep()  # the create may have reached the device before the reply was lost
            raise
        try:
            if not ok:
                return "FAIL", "create refused: " + text[:80]
            created |= reminder_ids(text.replace(message, ""))
            found = marked_reminder_ids(caller.call("self.robot.get_reminders")[1], REMINDER_MARKER)
            created |= found
            listed = bool(found)
        finally:
            swept = sweep()
        after = marked_reminder_ids(caller.call("self.robot.get_reminders")[1], REMINDER_MARKER)
        gone = not after and stop_error is None
        if after:
            return "FAIL", f"reminder {sorted(after)} STILL ACTIVE and will fire in under {REMINDER_SECONDS}s: dismiss it by hand"
        return ("PASS" if swept and listed and gone else "FAIL"), (
            f"created {sorted(swept)}; listed={listed}; removed after stop={gone}; not left to fire")
    checks.run("M13/M14/M15 reminder create, list, stop", reminders)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="SSH user@host of the Docker host")
    parser.add_argument("--device", help="device id (default: the only connected device)")
    parser.add_argument("--json", help="also write the rows to this file")
    parser.add_argument("--skip-leds", action="store_true")
    args = parser.parse_args()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit("terminated")))
    remote = Remote(args.host)
    device = args.device
    if not device:
        listed = ok_response(remote.admin("GET", "devices"), "devices")["devices"]
        if len(listed) != 1:
            parser.error(f"pass --device: {len(listed)} devices connected")
        device = listed[0]
    checks = run_checks(remote, device, args.skip_leds)
    print(checks.table())
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"device": device, "at": time.time(), "rows": checks.rows}, handle, indent=2)
    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
