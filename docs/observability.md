---
title: Observability
description: Prometheus metrics and a starter Grafana dashboard for the bridge dashboard service.
---

# Observability

The `bridge.py` dashboard service exposes a Prometheus exposition endpoint at `/metrics`,
and a starter Grafana dashboard lives at
[`monitoring/grafana-dashboard.json`](https://github.com/BrettKinny/dotty-stackchan/blob/main/monitoring/grafana-dashboard.json).

!!! note "Status: minimal"
    This is a hobby project and observability isn't a focus. Today only two
    metrics are actually recorded: **Kid Mode state** and **content-filter
    hits**. The other metrics below are *defined* in `bridge/metrics.py` but
    not yet wired into the request path, so most Grafana panels read 0. The
    endpoint and dashboard are scaffolding to build on if you want them, not a
    maintained monitoring setup.

!!! warning "LAN-only — never expose `/metrics` to the internet"
    The bridge listener should live on your home LAN (or behind a
    reverse proxy with auth). `/metrics` is unauthenticated by design
    — Prometheus expects to scrape it directly. Do **not** publish
    the bridge port to the public internet.

## Enable

Metrics are on by default once the bridge has its dependency installed:

```bash
pip install -r bridge/requirements.txt   # picks up prometheus-client
docker compose restart bridge            # or restart however you deployed it
curl -s http://<XIAOZHI_HOST>:8081/metrics | head -20
```

If `prometheus-client` is missing the bridge still serves traffic — it
just returns a `503` from `/metrics` so you (and your alerting) can
notice the degraded state instead of waiting on a timeout.

## Prometheus scrape config

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: dotty-bridge
    metrics_path: /metrics
    scrape_interval: 15s
    static_configs:
      - targets: ["<XIAOZHI_HOST>:8081"]
        labels:
          service: dotty-bridge
          env: home
```

Replace `<XIAOZHI_HOST>` with the LAN address of the Docker host running
the bridge. Reload Prometheus (`SIGHUP` or `/-/reload`) and confirm the
target shows `UP` under **Status → Targets**.

## Import the Grafana dashboard

1. Open Grafana → **Dashboards → New → Import**.
2. Click **Upload JSON file** and pick
   [`monitoring/grafana-dashboard.json`](https://github.com/BrettKinny/dotty-stackchan/blob/main/monitoring/grafana-dashboard.json).
3. When prompted for the `DS_PROMETHEUS` datasource, choose your
   Prometheus instance. Save.

The dashboard ships with eight panels: first-audio latency
(P50/P95/P99), request rate by endpoint, error rate by
endpoint+kind, active sessions (legacy panel — always 0), Smart-Mode invocation rate,
perception events per minute (stacked by type), calendar fetch
failure rate, and a Kid Mode single-stat toggle.

## What each metric means

**Recorded today:**

| Metric | Type | What it tells you |
| --- | --- | --- |
| `dotty_kid_mode_active` | Gauge | `1` if Kid Mode guardrails are active, `0` otherwise. Flipped live by the portal admin endpoint. |
| `dotty_content_filter_hits_total` | Counter | Times the content filter blocked or rewrote model output. |

**Defined in `bridge/metrics.py` but not yet wired into the request path** —
they exist so the endpoint schema is stable, but currently read 0:
`dotty_first_audio_latency_seconds`, `dotty_request_duration_seconds`,
`dotty_request_errors_total`, `dotty_llm_tokens_total`,
`dotty_calendar_fetch_failures_total`, `dotty_smart_mode_invocations_total`,
`dotty_perception_events_total`, and `dotty_active_acp_sessions` (a legacy
ZeroClaw metric, always 0).

## Suggested alert

Until the latency/error metrics are wired, only one signal is meaningful:

- **Bridge target down.** `up{job="dotty-bridge"} == 0` for 5 m — catches the
  case where Docker hasn't restarted the bridge container.

## Adding new metrics

`bridge/metrics.py` is the single source of truth. New metrics belong
in that file with a `dotty_` prefix and bounded label cardinality —
**never** label on user input, device IDs, or session IDs (each unique
value adds a permanent time series). When you wire the metric into
`bridge.py`, wrap the call in `_safe_metric(...)` so a typo or label
mismatch can't break the request path.

## Cross-references

- [Architecture](architecture.md) — where the bridge sits in the pipeline.
- [Voice Pipeline](voice-pipeline.md) — context for the first-audio
  latency budget; pair this dashboard with the latency-reduction work.
- [Troubleshooting](troubleshooting.md) — symptom-to-fix when the
  dashboard shows red.
