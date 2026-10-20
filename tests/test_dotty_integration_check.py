"""Integration-check restore and secrecy tests. AI-assisted: Claude (Opus 5.5)."""
import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("dotty_integration_check", SCRIPTS / "dotty_integration_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class FakeRemote:
    """Scripted Docker host: records admin calls, echoes state changes."""

    def __init__(self, state="idle", kid="true", smart="false", fail_route=None, asr_log=None):
        self.asr_log = asr_log if asr_log is not None else [
            "INFO-Loading faster-whisper model: id=models/whisper device=cuda compute_type=float16",
            "INFO-faster-whisper warm-up complete in 0.412s"]
        self.calls = []
        self.state = state
        self.policy = {"kid-mode": kid, "smart-mode": smart}
        self.events = []
        self.clock = 1000.0
        self.fail_route = fail_route

    def run(self, argv, input=None, check=True, timeout=None):
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if argv[0] == "date":
            self.clock += 1
            r.stdout = str(self.clock)
        elif argv[:2] == ["docker", "inspect"]:
            r.stdout = "\n".join(f"/{s} true 0" for s in check_module().SERVICES)
        elif argv[:3] == ["docker", "exec", "dotty-bridge"]:
            r.stdout = self.policy[argv[-1].rsplit("/", 1)[1]]
        elif argv[:3] == ["docker", "exec", "dotty-pi"]:
            r.stdout = "\n".join(t + ".ts" for t in check_module().TOOLS)
        elif argv[0] == "sh" and "faster-whisper" in argv[2]:
            r.stdout = "\n".join(self.asr_log)
        return r

    def get_json(self, url):
        if "perception/state" in url:
            return {"robot": {"current_state": self.state, "listening": False,
                              "sensor_stale": False, "sensor_age_s": 1}}
        if "perception/recent" in url:
            return list(reversed(self.events))
        if "vision/cache" in url:
            return {"robot": {"wall_ts": self.clock + 5, "description": "a private room"}}
        return {"status": "ok"}

    def http_status(self, url):
        return 200

    def admin(self, method, route, body=None, with_token=True):
        self.calls.append((route, body))
        if route == self.fail_route:
            return {"status": 503, "body": {"error": "fixture"}, "token_configured": True}
        if not with_token:
            return {"status": 401, "body": {"error": "unauthorized"}, "token_configured": True}
        if route == "devices":
            return {"status": 200, "body": {"devices": ["robot"]}, "token_configured": True}
        if route == "songs":
            return {"status": 200, "body": {"files": ["tetris.wav"]}, "token_configured": True}
        if (body or {}).get("name") == "nonsense" or (body or {}).get("state") == "nonsense":
            return {"status": 400, "body": {"error": "bad"}, "token_configured": True}
        if route == "set-state":
            self.state = body["state"]
            self.clock += 1
            self.events.append({"ts": self.clock, "name": "state_changed", "data": {"state": body["state"]}})
        return {"status": 200, "body": {"ok": True}, "token_configured": True}


def check_module():
    return check


def run(remote, monkeypatch, **kwargs):
    monkeypatch.setattr(check.time, "sleep", lambda seconds: None)
    return check.check(remote, "robot", **kwargs)


def test_all_checks_pass_and_settings_are_restored(monkeypatch):
    remote = FakeRemote()
    result = run(remote, monkeypatch)
    assert not result.failed, result.table()
    toggles = [b for r, b in remote.calls if r == "set-toggle" and b["name"] != "nonsense"]
    assert toggles == [
        {"device_id": "robot", "name": "smart_mode", "enabled": True},
        {"device_id": "robot", "name": "smart_mode", "enabled": False},
        {"device_id": "robot", "name": "kid_mode", "enabled": False},
        {"device_id": "robot", "name": "kid_mode", "enabled": True},
    ]
    heads = [b for r, b in remote.calls if r == "set-head-angles"]
    assert heads[-1]["yaw"] == 0 and heads[-1]["pitch"] == 0
    assert all(abs(b["yaw"]) <= 10 and abs(b["pitch"]) <= 10 for b in heads)
    assert remote.state == "idle"
    assert "private room" not in result.table()


def test_sticky_state_is_restored_and_motion_skipped(monkeypatch):
    remote = FakeRemote(state="sleep")
    result = run(remote, monkeypatch)
    assert remote.state == "sleep"
    assert not any(r == "set-head-angles" for r, _ in remote.calls)
    assert any(row["check"] == "set-head-angles" and row["status"] == "SKIP" for row in result.rows)


def test_toggle_restore_runs_even_when_flip_fails(monkeypatch):
    remote = FakeRemote()
    original = remote.admin
    def admin(method, route, body=None, with_token=True):
        if route == "set-toggle" and body.get("enabled") is True and body["name"] == "smart_mode":
            remote.calls.append((route, body))
            return {"status": 503, "body": {"error": "x"}, "token_configured": True}
        return original(method, route, body, with_token)
    remote.admin = admin
    result = run(remote, monkeypatch)
    assert result.failed
    smart = [b for r, b in remote.calls if r == "set-toggle" and b["name"] == "smart_mode"]
    assert smart[-1]["enabled"] is False


def test_unreadable_policy_never_flips_toggle(monkeypatch):
    remote = FakeRemote(kid="garbage")
    run(remote, monkeypatch)
    assert not any(r == "set-toggle" and b["name"] == "kid_mode" for r, b in remote.calls)


def asr_row(result):
    return next(row for row in result.rows if row["check"] == "WhisperLocal warm-up")


def test_whisper_warmup_passes_on_its_device(monkeypatch):
    row = asr_row(run(FakeRemote(), monkeypatch))
    assert row["status"] == "PASS" and "cuda" in row["detail"]


def test_missing_cuda_library_fails_the_asr_check(monkeypatch):
    remote = FakeRemote(asr_log=[
        "INFO-faster-whisper warm-up complete in 0.4s",  # an older start, superseded
        "INFO-Loading faster-whisper model: id=models/whisper device=cuda compute_type=float16",
        "WARNING-faster-whisper warm-up failed (non-fatal): Library libcublas.so.12 is not found"])
    result = run(remote, monkeypatch)
    assert asr_row(result)["status"] == "FAIL" and "libcublas" in asr_row(result)["detail"]
    assert result.failed


def test_other_asr_skips_the_whisper_check(monkeypatch):
    assert asr_row(run(FakeRemote(asr_log=[]), monkeypatch))["status"] == "SKIP"


def test_token_is_read_inside_container_only():
    assert "DOTTY_ADMIN_TOKEN" in check._ADMIN_CODE
    assert "print(token" not in check._ADMIN_CODE
