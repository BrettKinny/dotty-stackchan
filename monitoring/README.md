# monitoring/

Operational artifacts for observing the zeroclaw-bridge.

- `grafana-dashboard.json` — starter Grafana dashboard for the
  Prometheus metrics exposed by `bridge.py` at `/metrics`. Import it
  via the Grafana UI (Dashboards → New → Import) and pick a Prometheus
  datasource when prompted; the dashboard uses a `DS_PROMETHEUS`
  template variable so the same JSON is portable across environments.

For setup, scrape configuration, and what each metric means, see
[`docs/observability.md`](../docs/observability.md).
