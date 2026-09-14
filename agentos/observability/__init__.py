"""Observability adapters (docs/DEVELOPMENT_STRUCTURE.md §11 A9).

Telemetry is a consumer of the event log. Both adapters implement
`agentos.core.ports.Observer`; the core imports neither SDK. Install with
`pip install 'agentos[observability]'`.

Configuration (env), read by `build_observers()`:
  AGENTOS_OTEL_EXPORTER   otlp | console | none      (default: none)
  OTEL_EXPORTER_OTLP_ENDPOINT  standard OTel variable, e.g. http://localhost:4318
  AGENTOS_PROMETHEUS      1 to enable /metrics (default: 1 if the extra is installed)
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from agentos.core.events import Event
from agentos.core.ports import Observer, Store

log = logging.getLogger("agentos.observability")


def build_observers() -> tuple[list[Observer], object | None]:
    """Returns (observers, prometheus_observer_or_None) from the environment. Missing
    packages are a logged no-op, never an import error in the API or worker."""
    observers: list[Observer] = []
    prom = None
    try:
        from agentos.observability.prometheus import PrometheusObserver
        if os.environ.get("AGENTOS_PROMETHEUS", "1") == "1":
            prom = PrometheusObserver()
            observers.append(prom)
    except ImportError:
        log.info("prometheus-client not installed; /metrics disabled")

    exporter = os.environ.get("AGENTOS_OTEL_EXPORTER", "none").lower()
    if exporter != "none":
        try:
            from agentos.observability.otel import OtelObserver
            if exporter == "otlp":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                observers.append(OtelObserver(exporter=OTLPSpanExporter()))
            elif exporter == "console":
                from opentelemetry.sdk.trace.export import ConsoleSpanExporter
                observers.append(OtelObserver(exporter=ConsoleSpanExporter()))
            else:
                log.warning("unknown AGENTOS_OTEL_EXPORTER %r", exporter)
        except ImportError:
            log.info("opentelemetry not installed; tracing disabled")
    return observers, prom


def replay(store: Store, run_ids: Iterable[str], observers: Iterable[Observer]) -> int:
    """Rebuild telemetry from the log. Because observers derive everything from events
    (timestamps included), replaying a run's log produces the same spans and metrics
    that were emitted live. Returns the number of events replayed."""
    n = 0
    obs = list(observers)
    for run_id in run_ids:
        for event in store.read_events(run_id):
            for o in obs:
                o.observe(event)
            n += 1
    return n


__all__ = ["Event", "build_observers", "replay"]
