"""Prometheus metrics for zeroclaw-bridge.

Wired into bridge.py at module import time. Each metric is wrapped in
the bridge with a try/except so a metrics bug can never break the
request path; this module is also defensive — if `prometheus_client`
is missing, every public symbol degrades to a no-op so the bridge can
still serve traffic.

Conventions
-----------
- Metric names are prefixed with `dotty_` (per Prometheus best practice).
- Histograms expose latencies in seconds (NOT milliseconds), so Grafana's
  `histogram_quantile` math reads naturally.
- Label cardinality is kept small and bounded (`endpoint`, `kind`, `type`,
  `model`) — never user input, device-id, or session-id.

Usage
-----
    from bridge.metrics import (
        dotty_first_audio_latency_seconds,
        dotty_request_duration_seconds,
        dotty_request_errors_total,
        record_first_audio,
        metrics_app,
    )
    app.mount("/metrics", metrics_app())

    with dotty_request_duration_seconds.labels(endpoint="message").time():
        ...
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("zeroclaw-bridge.metrics")

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        make_asgi_app,
    )
    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover — exercised only when dep missing
    log.warning(
        "prometheus_client not importable — /metrics will return a stub. "
        "Install `prometheus-client>=0.20` to enable observability."
    )
    _PROMETHEUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# No-op fallbacks so bridge.py never has to guard imports.
# ---------------------------------------------------------------------------


class _NoopMetric:
    """Drop-in replacement for any prometheus_client metric.

    Supports `.labels(...)`, `.inc()`, `.dec()`, `.set()`, `.observe()`,
    `.time()` (returning a no-op context manager). Calling unknown
    attributes returns the same no-op so chained calls can't crash.
    """

    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def dec(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    class _NoopCM:
        def __enter__(self) -> "_NoopMetric._NoopCM":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    def time(self) -> "_NoopMetric._NoopCM":
        return _NoopMetric._NoopCM()


# A single dedicated registry keeps our metrics independent from any
# default global state (e.g. uvicorn's own gauges) and makes it easier
# for tests to scrape only the bridge's counters.
if _PROMETHEUS_AVAILABLE:
    REGISTRY = CollectorRegistry(auto_describe=True)

    dotty_first_audio_latency_seconds = Histogram(
        "dotty_first_audio_latency_seconds",
        "Wall-clock seconds from receiving a request to first audible TTS chunk.",
        buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
        registry=REGISTRY,
    )

    dotty_llm_tokens_total = Counter(
        "dotty_llm_tokens_total",
        "LLM token counts emitted/consumed, partitioned by direction and model.",
        labelnames=("kind", "model"),
        registry=REGISTRY,
    )

    dotty_request_duration_seconds = Histogram(
        "dotty_request_duration_seconds",
        "End-to-end bridge request duration, partitioned by endpoint.",
        labelnames=("endpoint",),
        registry=REGISTRY,
    )

    dotty_request_errors_total = Counter(
        "dotty_request_errors_total",
        "Request errors, partitioned by endpoint and error kind.",
        labelnames=("endpoint", "kind"),
        registry=REGISTRY,
    )

    dotty_active_acp_sessions = Gauge(
        "dotty_active_acp_sessions",
        "Number of in-flight ACP sessions (currently 0 or 1 — single child).",
        registry=REGISTRY,
    )

    dotty_calendar_fetch_failures_total = Counter(
        "dotty_calendar_fetch_failures_total",
        "Total Google Calendar fetch failures (excludes intentional skips).",
        registry=REGISTRY,
    )

    dotty_smart_mode_invocations_total = Counter(
        "dotty_smart_mode_invocations_total",
        "Smart-Mode invocations (request opted into the larger LLM).",
        registry=REGISTRY,
    )

    dotty_kid_mode_active = Gauge(
        "dotty_kid_mode_active",
        "1 if Kid Mode is currently enforcing child-safety guardrails, else 0.",
        registry=REGISTRY,
    )

    dotty_perception_events_total = Counter(
        "dotty_perception_events_total",
        "Ambient-perception events ingested, partitioned by event type.",
        labelnames=("type",),
        registry=REGISTRY,
    )

    dotty_content_filter_hits_total = Counter(
        "dotty_content_filter_hits_total",
        "Content-filter blocks partitioned by severity tier "
        "(redirect=profanity/slurs, log=sexual/violence, alert=hard drugs).",
        labelnames=("tier",),
        registry=REGISTRY,
    )

else:  # pragma: no cover — exercised only when prometheus_client missing
    REGISTRY = None  # type: ignore[assignment]
    dotty_first_audio_latency_seconds = _NoopMetric()  # type: ignore[assignment]
    dotty_llm_tokens_total = _NoopMetric()  # type: ignore[assignment]
    dotty_request_duration_seconds = _NoopMetric()  # type: ignore[assignment]
    dotty_request_errors_total = _NoopMetric()  # type: ignore[assignment]
    dotty_active_acp_sessions = _NoopMetric()  # type: ignore[assignment]
    dotty_calendar_fetch_failures_total = _NoopMetric()  # type: ignore[assignment]
    dotty_smart_mode_invocations_total = _NoopMetric()  # type: ignore[assignment]
    dotty_kid_mode_active = _NoopMetric()  # type: ignore[assignment]
    dotty_perception_events_total = _NoopMetric()  # type: ignore[assignment]
    dotty_content_filter_hits_total = _NoopMetric()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def record_first_audio(seconds: float) -> None:
    """Record a first-audio latency observation in seconds.

    Defensive — never raises. Negative or non-finite inputs are dropped
    so a clock skew or upstream bug can't poison the histogram.
    """
    try:
        s = float(seconds)
        if s < 0.0 or s != s:  # NaN check (NaN != NaN)
            return
        dotty_first_audio_latency_seconds.observe(s)
    except Exception:
        log.debug("record_first_audio failed", exc_info=True)


def metrics_app() -> Any:
    """Return an ASGI sub-app that serves the Prometheus exposition format.

    Mounted by bridge.py at `/metrics`. When prometheus_client is missing
    we return a tiny ASGI handler that responds with a 503 JSON payload
    so monitors can detect the degraded state instead of timing out.
    """
    if _PROMETHEUS_AVAILABLE:
        return make_asgi_app(registry=REGISTRY)

    async def _stub_app(scope: dict, receive: Any, send: Any) -> None:  # pragma: no cover
        if scope.get("type") != "http":
            return
        body = b'{"error":"prometheus_client not installed"}'
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    return _stub_app


__all__ = [
    "REGISTRY",
    "dotty_first_audio_latency_seconds",
    "dotty_llm_tokens_total",
    "dotty_request_duration_seconds",
    "dotty_request_errors_total",
    "dotty_active_acp_sessions",
    "dotty_calendar_fetch_failures_total",
    "dotty_smart_mode_invocations_total",
    "dotty_kid_mode_active",
    "dotty_perception_events_total",
    "dotty_content_filter_hits_total",
    "record_first_audio",
    "metrics_app",
]
