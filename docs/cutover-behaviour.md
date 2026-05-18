---
title: dotty-behaviour cutover runbook
last_reviewed: 2026-05-19
---

# dotty-behaviour cutover runbook

Steps to flip xiaozhi-server from the RPi-hosted `zeroclaw-bridge` to
the new Unraid-resident `dotty-behaviour` container, then decommission
the RPi.

Prerequisites:

- `feat/dotty-behaviour` branch merged (or being deployed from)
- SSH access to the Unraid host
- xiaozhi-server's `docker-compose.yml` and `data/.config.yaml` in
  this repo (they get edited in step 4)

## 1. Build + ship dotty-behaviour

```bash
BEHAVIOUR_HOST=root@<UNRAID_HOST> bash scripts/deploy-behaviour.sh
```

The script does: tracked-files tar → SSH → `docker build` →
`docker compose up -d --force-recreate` → poll `/health` → md5
round-trip verification.

Verify on Unraid:

```bash
ssh root@<UNRAID_HOST> 'curl -s http://localhost:8090/health'
# → {"status":"ok","service":"dotty-behaviour","version":"0.1.0"}
```

## 2. Migrate state files from the RPi

```bash
# household.yaml — used by greeter + face_recognized handler
scp <ZEROCLAW_USER>@<ZEROCLAW_HOST>:/root/.zeroclaw/household.yaml \
    /tmp/household.yaml
scp /tmp/household.yaml \
    root@<UNRAID_HOST>:/mnt/user/appdata/dotty-behaviour/state/household.yaml

# greeter_state.json — preserves greet log + per-day cap counters
scp <ZEROCLAW_USER>@<ZEROCLAW_HOST>:/root/.zeroclaw/greeter_state.json \
    /tmp/greeter_state.json || true  # may not exist if greeter never fired
scp /tmp/greeter_state.json \
    root@<UNRAID_HOST>:/mnt/user/appdata/dotty-behaviour/state/greeter_state.json \
    || true

# Google Calendar service-account JSON
scp <ZEROCLAW_USER>@<ZEROCLAW_HOST>:/root/.zeroclaw/secrets/google-calendar-sa.json \
    /tmp/cal-sa.json
scp /tmp/cal-sa.json \
    root@<UNRAID_HOST>:/mnt/user/appdata/dotty-behaviour/secrets/google-calendar-sa.json
rm /tmp/cal-sa.json
```

The state dir is bind-mounted into the container; no restart needed
after copying (household.yaml hot-reloads on mtime, greeter_state.json
is read on next greet).

## 3. Migrate daily NDJSON logs (optional)

These are append-only ring files; older days can stay on the RPi as
historical archive if you don't need them on Unraid:

```bash
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> \
    'sudo tar -czf /tmp/zb-logs.tgz -C /root/zeroclaw-bridge logs/'
scp <ZEROCLAW_USER>@<ZEROCLAW_HOST>:/tmp/zb-logs.tgz /tmp/
scp /tmp/zb-logs.tgz root@<UNRAID_HOST>:/tmp/
ssh root@<UNRAID_HOST> \
    'tar -xzf /tmp/zb-logs.tgz -C /mnt/user/appdata/dotty-behaviour/'
```

## 4. Retarget xiaozhi-server at the new URL

Edit `docker-compose.yml` in this repo:

```yaml
environment:
  - VISION_BRIDGE_URL=http://127.0.0.1:8090  # was: http://<ZEROCLAW_HOST>:8080
```

Edit `data/.config.yaml`:

```yaml
plugins:
  vision_explain: http://127.0.0.1:8090/api/vision/explain
```

Drop the obsolete LLM config block in `.config.yaml` that points at
`http://<ZEROCLAW_HOST>:8080/api/message/stream` — PiVoiceLLM doesn't
use it, and the bridge that served it is about to be powered off.

Deploy the xiaozhi-server config change:

```bash
bash scripts/deploy-xiaozhi.sh  # or whichever script you use
```

Then restart the container so the new env takes effect:

```bash
ssh root@<UNRAID_HOST> \
    'cd /mnt/user/appdata/xiaozhi-server && docker compose restart xiaozhi-esp32-server'
```

## 5. Smoke-test

```bash
# Perception event ingest hits the new container
ssh root@<UNRAID_HOST> 'curl -s http://localhost:8090/api/perception/state'

# Trigger a take_photo via the firmware (talk to Dotty and ask "what
# do you see?") — verify the description comes from dotty-behaviour's
# vision_cache, not the bridge:
ssh root@<UNRAID_HOST> 'docker logs --tail 20 dotty-behaviour | grep vision'
```

Optional: tail the bridge journal — there should be zero new
perception events, vision requests, or admin POSTs:

```bash
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> 'sudo journalctl -u zeroclaw-bridge -f'
```

## 6. Decommission the RPi

Once smoke-test passes:

```bash
# Stop and disable the service
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> '
    sudo systemctl stop zeroclaw-bridge
    sudo systemctl disable zeroclaw-bridge
'

# Archive the RPi state to Unraid for posterity
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> '
    sudo tar -czf /tmp/zeroclaw-archive.tgz \
        /root/.zeroclaw /root/zeroclaw-bridge /etc/systemd/system/zeroclaw-bridge.service
'
scp <ZEROCLAW_USER>@<ZEROCLAW_HOST>:/tmp/zeroclaw-archive.tgz /tmp/
scp /tmp/zeroclaw-archive.tgz \
    root@<UNRAID_HOST>:/mnt/user/appdata/dotty-behaviour/archives/
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> 'rm /tmp/zeroclaw-archive.tgz'

# Power off
ssh <ZEROCLAW_USER>@<ZEROCLAW_HOST> 'sudo poweroff'
```

The RPi can now be physically removed. Update CLAUDE.md /
README.md / docker-compose.yml comments to drop the `<ZEROCLAW_HOST>`
references in a follow-up commit.

## Rollback

If anything goes wrong before step 6, revert in this order:

1. Revert the `VISION_BRIDGE_URL` and `vision_explain` config edits.
2. Restart xiaozhi-server.
3. Bridge.py is still running on the RPi (disabled only in step 6),
   so it'll start receiving events again immediately.

After step 6, rollback requires restoring `/root/.zeroclaw/` and
`/root/zeroclaw-bridge/` from the archive tgz and re-enabling the
systemd unit.
