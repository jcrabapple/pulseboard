"""Prometheus metrics export for PulseBoard.

This module renders PulseBoard's check history into the Prometheus text
exposition format so it can be scraped by a Prometheus server, a
``node_exporter`` textfile collector, or any other OpenMetrics-compatible
scrape target.

Three output modes are supported:

- ``stdout`` — emit the rendered text payload to standard output (the
  default; useful for piping to ``curl --data-binary @-`` against a
  Pushgateway).
- ``textfile`` — atomically write the rendered text payload to a file
  on disk, suitable for the ``node_exporter`` ``--collector.textfile.directory``
  mechanism.
- ``serve`` — run an HTTP server exposing ``/metrics``, ``/``, and
  ``/healthz`` endpoints.

Metric families
---------------

For every configured service, the exporter emits a stable set of
gauges and counters with the ``pulseboard_`` prefix:

- ``pulseboard_up`` (gauge) — 1 when last status is UP, 0 otherwise.
- ``pulseboard_status`` (gauge) — numeric status encoding
  (1=UP, 2=DEGRADED, 3=DOWN, 4=UNKNOWN).
- ``pulseboard_status_code`` (gauge) — last HTTP status code seen
  (0 when not applicable / unset).
- ``pulseboard_latency_seconds`` (gauge) — last observed latency in
  seconds.
- ``pulseboard_checks_total`` (counter) — lifetime number of checks
  performed for the service.
- ``pulseboard_incidents_total`` (counter) — lifetime number of
  incidents opened for the service.
- ``pulseboard_open_incidents`` (gauge) — number of currently-open
  incidents for the service.
- ``pulseboard_uptime_ratio`` (gauge) — uptime fraction over the
  requested window (0.0–1.0).
- ``pulseboard_avg_latency_ms`` / ``pulseboard_min_latency_ms`` /
  ``pulseboard_max_latency_ms`` / ``pulseboard_p50_latency_ms`` /
  ``pulseboard_p95_latency_ms`` / ``pulseboard_p99_latency_ms`` (gauges)
  — aggregate latency statistics over the window.
- ``pulseboard_last_check_timestamp_seconds`` (gauge) — Unix timestamp
  of the most recent check.

Plus two self-metrics describing the exporter itself:

- ``pulseboard_scrape_duration_seconds`` (gauge) — wall-clock time
  spent rendering the payload.
- ``pulseboard_services_exported`` (gauge) — count of services in
  the rendered payload.

Labels
------

Every per-service sample carries ``service`` (the configured service
name) and ``type`` (the resolved service type — ``http``, ``tcp``,
``dns``, ``ssl``, ``content``, or ``unknown`` when unknown).
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, TextIO

from .models import CheckResult, ServiceSummary, Status

# ---------------------------------------------------------------------------
# Status encoding
# ---------------------------------------------------------------------------

#: Numeric encoding of :class:`~pulseboard.models.Status` values for the
#: ``pulseboard_status`` gauge. Kept stable so dashboards can rely on it.
STATUS_VALUE: dict[Status, int] = {
    Status.UP: 1,
    Status.DEGRADED: 2,
    Status.DOWN: 3,
    Status.UNKNOWN: 4,
}


# ---------------------------------------------------------------------------
# Sample model + rendering
# ---------------------------------------------------------------------------


@dataclass
class MetricSample:
    """A single Prometheus text-format sample.

    Holds the metric name, its numeric value, an optional set of
    string-valued labels, and optional HELP / TYPE lines. Rendering
    produces the exact Prometheus text-format line (or block of lines
    when HELP/TYPE are present), suitable for concatenation into a
    full scrape payload.
    """

    name: str
    value: float | int | bool
    labels: dict[str, str] = field(default_factory=dict)
    help_text: str | None = None
    metric_type: str | None = None  # "gauge" or "counter"

    def render(self) -> str:
        """Render this sample as Prometheus text.

        When ``help_text`` and ``metric_type`` are both set, the
        rendered output is a three-line block:

        .. code-block:: text

            # HELP <name> <help_text>
            # TYPE <name> <metric_type>
            <name>{<labels>} <value>

        Otherwise just the sample line is returned. Labels are sorted
        alphabetically for deterministic output. Label values have
        backslash, double-quote, and newline characters escaped per
        the Prometheus spec. HELP text has backslash and newline
        characters escaped as well.
        """
        lines: list[str] = []
        if self.help_text is not None and self.metric_type is not None:
            lines.append(f"# HELP {self.name} {self._escape_help(self.help_text)}")
            lines.append(f"# TYPE {self.name} {self.metric_type}")
        lines.append(self._render_sample_line())
        # No trailing newline: each sample line stands alone and the
        # outer renderer controls final line endings.
        return "\n".join(lines)

    def _render_sample_line(self) -> str:
        if self.labels:
            sorted_kv = sorted(self.labels.items())
            label_str = ",".join(
                f'{k}="{self._escape_label_value(v)}"' for k, v in sorted_kv
            )
            return f"{self.name}{{{label_str}}} {self._format_value()}"
        return f"{self.name} {self._format_value()}"

    def _format_value(self) -> str:
        # bool is an int subclass; render explicitly as 0 / 1.
        if isinstance(self.value, bool):
            return "1" if self.value else "0"
        if isinstance(self.value, int):
            return str(self.value)
        # float: trust Python's repr; spec accepts standard floats.
        return repr(float(self.value))

    @staticmethod
    def _escape_label_value(value: str) -> str:
        # Per the Prometheus text format: \, ", and newline must be escaped.
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
        )

    @staticmethod
    def _escape_help(text: str) -> str:
        # HELP only requires \ and \n to be escaped (not ").
        return text.replace("\\", "\\\\").replace("\n", "\\n")


def render_samples(samples: Iterable[MetricSample]) -> str:
    """Render an iterable of samples into a single Prometheus payload.

    Aggregates samples by name and emits a single HELP / TYPE pair per
    family. Families are ordered by first appearance; samples within a
    family keep their input order.
    """
    samples = list(samples)
    if not samples:
        return ""

    # Group samples by family name, preserving the first-seen order
    # for family order, and grouping the per-sample renderings.
    family_order: list[str] = []
    family_help_type: dict[str, tuple[str, str]] = {}
    family_bodies: dict[str, list[str]] = {}

    for s in samples:
        if s.name not in family_bodies:
            family_order.append(s.name)
            if s.help_text is not None and s.metric_type is not None:
                family_help_type[s.name] = (s.help_text, s.metric_type)
            family_bodies[s.name] = []
        family_bodies[s.name].append(s._render_sample_line())

    out: list[str] = []
    for name in family_order:
        if name in family_help_type:
            help_text, metric_type = family_help_type[name]
            out.append(f"# HELP {name} {MetricSample._escape_help(help_text)}")
            out.append(f"# TYPE {name} {metric_type}")
        for body in family_bodies[name]:
            out.append(body)

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Per-service metric builder
# ---------------------------------------------------------------------------


def _resolve_type(
    result: CheckResult | None,
    fallback_name: str,
    service_type_resolver: Callable[[str], str] | None,
) -> str:
    """Resolve the ``type`` label value for a service.

    Preference order:
    1. The service_type_resolver callback (if provided).
    2. The ``service_type`` field stored in the latest CheckResult's
       ``details`` (if present).
    3. ``"unknown"``.
    """
    if service_type_resolver is not None:
        try:
            return service_type_resolver(fallback_name)
        except Exception:  # pragma: no cover - resolver must not break us
            return "unknown"
    if result is not None:
        details = result.details or {}
        st = details.get("service_type")
        if isinstance(st, str) and st:
            return st
    return "unknown"


def _build_service_metrics(
    summary: ServiceSummary,
    last_result: CheckResult | None,
    open_incidents: int,
    lifetime_checks: int,
    lifetime_incidents: int,
    *,
    service_type_resolver: Callable[[str], str] | None = None,
) -> list[MetricSample]:
    """Build the list of :class:`MetricSample` objects for one service.

    Every metric family is labeled with ``service`` and ``type``. When
    ``last_result`` is ``None`` (the service has no checks yet, or has
    been pruned), the per-check families (``status_code``,
    ``latency_seconds``, ``last_check_timestamp_seconds``) are skipped
    — the summary-level aggregates are still emitted.
    """
    type_label = _resolve_type(
        last_result, summary.service_name, service_type_resolver
    )
    base_labels: dict[str, str] = {
        "service": summary.service_name,
        "type": type_label,
    }

    samples: list[MetricSample] = []

    # pulseboard_up — 1 if UP, 0 otherwise.
    samples.append(MetricSample(
        name="pulseboard_up",
        value=1 if summary.last_status == Status.UP else 0,
        labels=dict(base_labels),
        help_text="1 if the most recent check was UP, 0 otherwise.",
        metric_type="gauge",
    ))

    # pulseboard_status — numeric encoding.
    samples.append(MetricSample(
        name="pulseboard_status",
        value=STATUS_VALUE.get(summary.last_status, 0),
        labels=dict(base_labels),
        help_text="Numeric status encoding: 1=UP, 2=DEGRADED, 3=DOWN, 4=UNKNOWN.",
        metric_type="gauge",
    ))

    if last_result is not None:
        # pulseboard_status_code — last HTTP status code (or 0 if N/A).
        samples.append(MetricSample(
            name="pulseboard_status_code",
            value=float(last_result.status_code or 0),
            labels=dict(base_labels),
            help_text="Last HTTP status code observed (0 when not applicable).",
            metric_type="gauge",
        ))

        # pulseboard_latency_seconds — last latency in seconds.
        samples.append(MetricSample(
            name="pulseboard_latency_seconds",
            value=float(last_result.latency_ms) / 1000.0,
            labels=dict(base_labels),
            help_text="Latency of the most recent check, in seconds.",
            metric_type="gauge",
        ))

        # pulseboard_last_check_timestamp_seconds — Unix timestamp.
        ts = last_result.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        samples.append(MetricSample(
            name="pulseboard_last_check_timestamp_seconds",
            value=ts.timestamp(),
            labels=dict(base_labels),
            help_text="Unix timestamp of the most recent check.",
            metric_type="gauge",
        ))

    # pulseboard_checks_total — lifetime counter.
    samples.append(MetricSample(
        name="pulseboard_checks_total",
        value=int(lifetime_checks),
        labels=dict(base_labels),
        help_text="Lifetime number of checks performed for this service.",
        metric_type="counter",
    ))

    # pulseboard_incidents_total — lifetime counter.
    samples.append(MetricSample(
        name="pulseboard_incidents_total",
        value=int(lifetime_incidents),
        labels=dict(base_labels),
        help_text="Lifetime number of incidents opened for this service.",
        metric_type="counter",
    ))

    # pulseboard_open_incidents — gauge.
    samples.append(MetricSample(
        name="pulseboard_open_incidents",
        value=int(open_incidents),
        labels=dict(base_labels),
        help_text="Number of incidents currently open for this service.",
        metric_type="gauge",
    ))

    # pulseboard_uptime_ratio — fraction over the window (0.0–1.0).
    ratio = (
        summary.successful_checks / summary.total_checks
        if summary.total_checks > 0
        else 0.0
    )
    samples.append(MetricSample(
        name="pulseboard_uptime_ratio",
        value=float(ratio),
        labels=dict(base_labels),
        help_text="Uptime ratio over the requested time window (0.0–1.0).",
        metric_type="gauge",
    ))

    # Latency aggregates.
    samples.append(MetricSample(
        name="pulseboard_avg_latency_ms",
        value=float(summary.avg_latency_ms),
        labels=dict(base_labels),
        help_text="Average latency over the window, in milliseconds.",
        metric_type="gauge",
    ))
    samples.append(MetricSample(
        name="pulseboard_min_latency_ms",
        value=float(summary.min_latency_ms),
        labels=dict(base_labels),
        help_text="Minimum latency over the window, in milliseconds.",
        metric_type="gauge",
    ))
    samples.append(MetricSample(
        name="pulseboard_max_latency_ms",
        value=float(summary.max_latency_ms),
        labels=dict(base_labels),
        help_text="Maximum latency over the window, in milliseconds.",
        metric_type="gauge",
    ))
    samples.append(MetricSample(
        name="pulseboard_p95_latency_ms",
        value=float(summary.p95_latency_ms),
        labels=dict(base_labels),
        help_text="95th-percentile latency over the window, in milliseconds.",
        metric_type="gauge",
    ))
    samples.append(MetricSample(
        name="pulseboard_p99_latency_ms",
        value=float(summary.p99_latency_ms),
        labels=dict(base_labels),
        help_text="99th-percentile latency over the window, in milliseconds.",
        metric_type="gauge",
    ))
    samples.append(MetricSample(
        name="pulseboard_p50_latency_ms",
        value=float(summary.p50_latency_ms),
        labels=dict(base_labels),
        help_text="50th-percentile (median) latency over the window, in milliseconds.",
        metric_type="gauge",
    ))

    return samples


# ---------------------------------------------------------------------------
# MetricsExporter
# ---------------------------------------------------------------------------


class MetricsExporter:
    """Render PulseBoard history into Prometheus text format.

    Parameters
    ----------
    storage:
        A :class:`~pulseboard.storage.Storage` instance to read
        history from. The exporter takes ownership of closing it
        only when *it* opened it; otherwise the caller is responsible.
    hours:
        Time window for windowed aggregates (uptime, latency stats).
    service_type_resolver:
        Optional callback ``(service_name) -> type_label``. When given,
        overrides the ``type`` label that would otherwise be derived
        from the latest ``CheckResult.details.service_type``.
    """

    def __init__(
        self,
        storage: object,
        hours: int = 24,
        service_type_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.storage = storage
        self.hours = int(hours)
        self.service_type_resolver = service_type_resolver

    # ---- Public API -----------------------------------------------------

    def render(self) -> str:
        """Render the full scrape payload as a Prometheus text string."""
        start = time.monotonic()
        samples: list[MetricSample] = []

        summaries = self.storage.get_all_summaries(hours=self.hours)
        open_incidents_by_service = self._open_incidents_by_service()
        lifetime_checks_by_service = self._lifetime_checks_by_service()

        for summary in summaries:
            last_result = self._last_result_for(summary.service_name)
            open_n = open_incidents_by_service.get(summary.service_name, 0)
            lifetime_n = lifetime_checks_by_service.get(summary.service_name, 0)
            lifetime_inc = self._lifetime_incidents_for(summary.service_name)
            samples.extend(_build_service_metrics(
                summary=summary,
                last_result=last_result,
                open_incidents=open_n,
                lifetime_checks=lifetime_n,
                lifetime_incidents=lifetime_inc,
                service_type_resolver=self.service_type_resolver,
            ))

        # Self-metrics.
        duration = time.monotonic() - start
        samples.append(MetricSample(
            name="pulseboard_scrape_duration_seconds",
            value=float(duration),
            help_text="Wall-clock seconds spent rendering this scrape payload.",
            metric_type="gauge",
        ))
        samples.append(MetricSample(
            name="pulseboard_services_exported",
            value=len(summaries),
            help_text="Number of services included in this scrape payload.",
            metric_type="gauge",
        ))

        return render_samples(samples)

    def write_textfile(self, path: str | Path) -> int:
        """Atomically write the payload to ``path``.

        Creates parent directories as needed. Writes to a sibling
        ``.tmp`` file first, then renames into place — so a
        ``node_exporter`` scraping the directory never sees a partial
        file.

        Returns the number of samples rendered.
        """
        payload = self.render()
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            prefix=out_path.name + ".",
            suffix=".tmp",
            dir=str(out_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:  # pragma: no cover - some FS reject fsync
                    pass
            os.replace(tmp_path, out_path)
        except Exception:
            # Clean up the partial temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Count samples (lines that aren't comments or blank).
        n = sum(
            1 for line in payload.splitlines()
            if line and not line.startswith("#")
        )
        return n

    # ---- Storage adapters ----------------------------------------------

    def _last_result_for(self, service_name: str) -> CheckResult | None:
        """Return the most recent CheckResult for a service, or None."""
        # Prefer get_recent(limit=1) — it's already implemented for the
        # dashboard. Fall back to scanning get_history() if needed.
        try:
            recent = self.storage.get_recent(service_name, limit=1)
            if recent:
                return recent[0]
        except AttributeError:
            pass
        try:
            history = self.storage.get_history(service_name, hours=self.hours)
            if history:
                # History is oldest-first; take the last.
                return history[-1]
        except AttributeError:
            pass
        return None

    def _open_incidents_by_service(self) -> dict[str, int]:
        """Return a mapping of service_name -> open incident count."""
        # Try a dedicated API first; fall back to scanning all incidents.
        try:
            counts = self.storage.count_open_incidents_by_service()  # type: ignore[attr-defined]
            if isinstance(counts, dict):
                return counts
        except AttributeError:
            pass
        out: dict[str, int] = {}
        try:
            for inc in self.storage.get_incidents(open_only=True):
                out[inc.service_name] = out.get(inc.service_name, 0) + 1
        except AttributeError:
            pass
        return out

    def _lifetime_checks_by_service(self) -> dict[str, int]:
        """Return a mapping of service_name -> lifetime check count."""
        try:
            counts = self.storage.count_checks_by_service()  # type: ignore[attr-defined]
            if isinstance(counts, dict):
                return counts
        except AttributeError:
            pass
        # Fallback: count per-service rows.
        out: dict[str, int] = {}
        for name in self.storage.get_all_service_names():
            try:
                out[name] = self.storage.count_checks(service_name=name)  # type: ignore[attr-defined]
            except AttributeError:
                out[name] = 0
        return out

    def _lifetime_incidents_for(self, service_name: str) -> int:
        """Return the lifetime incident count for one service."""
        try:
            return self.storage.count_incidents(service_name=service_name)  # type: ignore[attr-defined]
        except AttributeError:
            pass
        # Fallback: scan get_incidents for this service.
        try:
            incidents = self.storage.get_incidents(service_name=service_name)
            return len(incidents)
        except AttributeError:
            return 0


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>PulseBoard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 640px; margin: 4em auto; color: #222; padding: 0 1em; }}
  h1 {{ margin-bottom: 0.2em; }}
  code {{ background: #f4f4f4; padding: 0.1em 0.4em; border-radius: 3px; }}
  ul {{ line-height: 1.7; }}
</style>
</head>
<body>
  <h1>PulseBoard</h1>
  <p>Uptime monitor &amp; service dashboard.</p>
  <ul>
    <li><a href="/metrics"><code>/metrics</code></a> &mdash; Prometheus scrape target.</li>
    <li><a href="/healthz"><code>/healthz</code></a> &mdash; Liveness check (returns <code>ok</code>).</li>
  </ul>
</body>
</html>
"""


class _MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves /metrics, /, and /healthz."""

    # Class-level exporter set by serve_metrics before the server starts.
    exporter: MetricsExporter = None  # type: ignore[assignment]
    server_version = "PulseBoard/1.0"

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/metrics" or self.path.startswith("/metrics?"):
            try:
                payload = self.exporter.render().encode("utf-8")
            except Exception:  # pragma: no cover - defensive
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"internal error rendering metrics\n")
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "/index.html":
            body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default stderr access logging; the CLI handles its
        # own logging.
        return


def serve_metrics(
    exporter: MetricsExporter,
    host: str = "127.0.0.1",
    port: int = 9464,
    stream: TextIO | None = None,
    *,
    max_requests: int | None = None,
) -> None:
    """Run an HTTP server exposing the exporter's payload.

    Parameters
    ----------
    exporter:
        A :class:`MetricsExporter` whose ``render()`` is called on
        every ``/metrics`` request.
    host, port:
        Bind address. ``127.0.0.1`` by default — set ``0.0.0.0`` to
        expose on all interfaces.
    stream:
        Unused — kept for backward compatibility with earlier API
        sketches.
    max_requests:
        Optional bound on the number of requests served before the
        server exits. Used by tests to bound runtime; production
        callers leave this ``None`` and stop the server with
        ``KeyboardInterrupt``.
    """
    _MetricsHandler.exporter = exporter

    # max_requests=0 means "serve no requests and exit immediately".
    # Don't even bind the port — the test hook is purely to bound
    # runtime, and binding+serv+e+shut-down costs at least one socket
    # cycle.
    if max_requests is not None and int(max_requests) <= 0:
        return

    server = ThreadingHTTPServer((host, port), _MetricsHandler)

    if max_requests is not None:
        # Wrap the server so it shuts down after N requests — useful
        # for unit tests that don't want a real long-running server.
        max_requests = int(max_requests)
        remaining = [max_requests]

        original_finish = _MetricsHandler.finish  # type: ignore[attr-defined]

        def counting_finish(self: _MetricsHandler) -> None:  # type: ignore[no-redef]
            original_finish(self)
            remaining[0] -= 1
            if remaining[0] <= 0:
                # shutdown must be called from a different thread.
                threading.Thread(
                    target=server.shutdown, daemon=True
                ).start()

        _MetricsHandler.finish = counting_finish  # type: ignore[assignment]

    try:
        server.serve_forever()
    finally:
        server.server_close()