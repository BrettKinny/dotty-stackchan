---
title: Observability
description: Prometheus metrics and a starter Grafana dashboard for the zeroclaw-bridge.
---

# Observability

The zeroclaw-bridge exposes a Prometheus exposition endpoint at `/metrics`
covering first-audio latency, request rate / errors per endpoint, ACP
session state, perception events, calendar health, and Kid Mode state.
A starter Grafana dashboard lives at
[`monitoring/grafana-dashboard.json`](https://github.com/BrettKinny/dotty-stackchan/blob/main/monitoring/grafana-dashboard.json).

These metrics are the **measurement prerequisite** for the
[first-audio latency reduction](../ROADMAP.md) follow-up work. Numbers
come first; you can't tune what you can't see.

!!! warning "LAN-only — never expose `/metrics` to the internet"
    The bridge listener should live on your home LAN (or behind a
    reverse proxy with auth). `/metrics` is unauthenticated by design
    — Prometheus expects to scrape it directly. Do **not** publish
    the bridge port to the public internet.

## Enable

Metrics are on by default once the bridge has its dependency installed:

```bash
pip install -r bridge/requirements.txt   # picks up prometheus-client
systemctl restart zeroclaw-bridge        # or `docker compose restart bridge`
curl -s http://<BRIDGE_HOST>:8080/metrics | head -20
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
      - targets: ["<BRIDGE_HOST>:8080"]
        labels:
          service: zeroclaw-bridge
          env: home
```

Replace `<BRIDGE_HOST>` with the LAN address of the box running the
bridge. Reload Prometheus (`SIGHUP` or `/-/reload`) and confirm the
target shows `UP` under **Status → Targets**.

## Import the Grafana dashboard

1. Open Grafana → **Dashboards → New → Import**.
2. Click **Upload JSON file** and pick
   [`monitoring/grafana-dashboard.json`](https://github.com/BrettKinny/dotty-stackchan/blob/main/monitoring/grafana-dashboard.json).
3. When prompted for the `DS_PROMETHEUS` datasource, choose your
   Prometheus instance. Save.

The dashboard ships with eight panels: first-audio latency
(P50/P95/P99), request rate by endpoint, error rate by
endpoint+kind, active ACP sessions, Smart-Mode invocation rate,
perception events per minute (stacked by type), calendar fetch
failure rate, and a Kid Mode single-stat toggle.

## What each metric means

| Metric | Type | What it tells you |
| --- | --- | --- |
| `dotty_first_audio_latency_seconds` | Histogram | Bridge-side seconds from request received to first content chunk emitted. Tightly correlated with perceived robot responsiveness. |
| `dotty_request_duration_seconds{endpoint}` | Histogram | End-to-end duration per endpoint (`message`, `message_stream`, `vision_explain`, `calendar_today`, `perception_event`). |
| `dotty_request_errors_total{endpoint,kind}` | Counter | Errors partitioned by endpoint and `kind` (`timeout`, `binary_missing`, `exception`). |
| `dotty_llm_tokens_total{kind,model}` | Counter | LLM token volume; reserved for future per-call accounting. |
| `dotty_active_acp_sessions` | Gauge | Live ACP child sessions. The bridge is single-child so this is normally 0 (idle) or 1 (in flight). |
| `dotty_calendar_fetch_failures_total` | Counter | Google Calendar fetch errors. The cache backs off automatically; sustained failures mean look at the bridge log. |
| `dotty_smart_mode_invocations_total` | Counter | Smart-Mode requests (the `metadata.smart_mode` flag opted into the larger LLM). |
| `dotty_kid_mode_active` | Gauge | `1` if Kid Mode guardrails are active, `0` otherwise. Flipped live by the portal admin endpoint. |
| `dotty_perception_events_total{type}` | Counter | Ambient-perception events ingested, partitioned by `face_detected` / `face_lost` / `sound_event`. |

## Suggested alerts

Start small — these are the four signals worth paging on for a
home-deployed robot:

- **First-audio latency P95 > 3 s for 10 minutes.**
  `histogram_quantile(0.95, sum by (le) (rate(dotty_first_audio_latency_seconds_bucket[5m]))) > 3`
- **Sustained error rate.**
  `sum by (endpoint, kind) (rate(dotty_request_errors_total[5m])) > 0.05`
- **Calendar fetch flatlined failing.**
  `rate(dotty_calendar_fetch_failures_total[15m]) > 0.005` for 30 m.
- **Bridge target down.**
  `up{job="dotty-bridge"} == 0` for 5 m. Catches the case where
  systemd / Docker hasn't restarted the bridge.

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
