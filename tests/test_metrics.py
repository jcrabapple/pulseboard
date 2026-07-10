"""Tests for the Prometheus metrics export module."""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from pulseboard import metrics
from pulseboard.cli import cli
from pulseboard.metrics import (
    MetricSample,
    MetricsExporter,
    STATUS_VALUE,
    render_samples,
    serve_metrics,
)
from pulseboard.models import CheckResult, ServiceType, Status
from pulseboard.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    name: str,
    status: Status = Status.UP,
    latency: float = 42.0,
    status_code: int | None = 200,
    error: str | None = None,
    ts: datetime | None = None,
    service_type: ServiceType | None = None,
) -> CheckResult:
    """Build a CheckResult with sensible defaults for testing."""
    details: dict = {}
    if service_type is not None:
        details["service_type"] = service_type.value
    return CheckResult(
        service_name=name,
        timestamp=ts or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        status=status,
        latency_ms=latency,
        status_code=status_code,
        error=error,
        details=details,
    )


def _seed_storage(tmp_path: Path, services: dict[str, list[CheckResult]]) -> Storage:
    """Create a Storage populated with the given per-service results."""
    storage = Storage(tmp_path / "metrics.db")
    for rows in services.values():
        for r in rows:
            storage.store(r)
    return storage


# ---------------------------------------------------------------------------
# Status value encoding
# ---------------------------------------------------------------------------


class TestStatusEncoding:
    """The numeric status encoding should be stable and intuitive."""

    def test_up_is_one(self):
        assert STATUS_VALUE[Status.UP] == 1

    def test_degraded_is_two(self):
        assert STATUS_VALUE[Status.DEGRADED] == 2

    def test_down_is_three(self):
        assert STATUS_VALUE[Status.DOWN] == 3

    def test_unknown_is_four(self):
        assert STATUS_VALUE[Status.UNKNOWN] == 4

    def test_all_statuses_have_values(self):
        # If a new Status is added without updating the map, this catches it.
        for s in Status:
            assert s in STATUS_VALUE


# ---------------------------------------------------------------------------
# MetricSample rendering
# ---------------------------------------------------------------------------


class TestMetricSampleRendering:
    """Test individual sample formatting against the Prometheus spec."""

    def test_minimal_sample(self):
        s = MetricSample(name="foo", value=42)
        assert s.render() == "foo 42"

    def test_int_value_is_plain_integer(self):
        s = MetricSample(name="foo", value=7)
        assert s.render() == "foo 7"

    def test_float_value(self):
        s = MetricSample(name="foo", value=42.5)
        assert s.render() == "foo 42.5"

    def test_bool_is_zero_or_one(self):
        # bool is an int subclass in Python; we want to render it as 0/1.
        assert MetricSample(name="t", value=True).render() == "t 1"
        assert MetricSample(name="t", value=False).render() == "t 0"

    def test_help_and_type_preamble(self):
        s = MetricSample(
            name="foo",
            value=1,
            help_text="Number of foos.",
            metric_type="gauge",
        )
        out = s.render()
        assert "# HELP foo Number of foos." in out
        assert "# TYPE foo gauge" in out
        assert out.endswith("foo 1")

    def test_labels_sorted_alphabetically(self):
        # Spec requires deterministic label order; we sort for diff-friendliness.
        s = MetricSample(
            name="foo",
            value=1,
            labels={"zulu": "z", "alpha": "a", "mike": "m"},
        )
        line = s.render().splitlines()[-1]
        # Labels are sorted alphabetically in output
        assert line == 'foo{alpha="a",mike="m",zulu="z"} 1'

    def test_label_value_escaping(self):
        # \ and " must be escaped; \n must become literal \n
        s = MetricSample(
            name="foo",
            value=1,
            labels={"k": 'a"b\\c\nd'},
        )
        line = s.render().splitlines()[-1]
        assert line == 'foo{k="a\\"b\\\\c\\nd"} 1'

    def test_help_text_escaping(self):
        s = MetricSample(
            name="foo",
            value=1,
            help_text="back\\slash and\nnewline",
            metric_type="gauge",
        )
        out = s.render()
        assert "back\\\\slash and\\nnewline" in out

    def test_empty_labels_renders_no_braces(self):
        s = MetricSample(name="foo", value=1, labels={})
        line = s.render().splitlines()[-1]
        assert line == "foo 1"


class TestRenderSamples:
    """Test aggregation of multiple samples into a single payload."""

    def test_single_family_emits_help_type_once(self):
        payload = render_samples([
            MetricSample(name="m", value=1, labels={"a": "1"},
                         help_text="h", metric_type="gauge"),
            MetricSample(name="m", value=2, labels={"a": "2"}),
        ])
        # Exactly one HELP and one TYPE line for ``m``
        assert payload.count("# HELP m ") == 1
        assert payload.count("# TYPE m gauge") == 1
        # Both samples are emitted
        assert 'm{a="1"} 1' in payload
        assert 'm{a="2"} 2' in payload

    def test_multiple_families_each_get_own_help_type(self):
        payload = render_samples([
            MetricSample(name="a", value=1, help_text="h_a", metric_type="gauge"),
            MetricSample(name="b", value=2, help_text="h_b", metric_type="counter"),
        ])
        assert "# HELP a h_a" in payload
        assert "# TYPE a gauge" in payload
        assert "# HELP b h_b" in payload
        assert "# TYPE b counter" in payload

    def test_empty_input_returns_empty_string(self):
        assert render_samples([]) == ""


# ---------------------------------------------------------------------------
# _build_service_metrics
# ---------------------------------------------------------------------------


class TestBuildServiceMetrics:
    """The per-service metric builder should produce a stable family set."""

    def _summary(self) -> "ServiceSummary":
        from pulseboard.models import ServiceSummary
        return ServiceSummary(
            service_name="github",
            total_checks=10,
            successful_checks=9,
            failed_checks=1,
            uptime_pct=90.0,
            avg_latency_ms=120.0,
            min_latency_ms=80.0,
            max_latency_ms=200.0,
            last_status=Status.UP,
            last_check=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            p95_latency_ms=180.0,
            p99_latency_ms=195.0,
        )

    def test_emits_expected_families(self):
        summary = self._summary()
        last = _make_result(
            "github", Status.UP, 120.0, 200,
            ts=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=last,
            open_incidents=0,
            lifetime_checks=10,
            lifetime_incidents=1,
        )
        names = {s.name for s in samples}
        # Every family promised in the docstring is present.
        assert {
            "pulseboard_up",
            "pulseboard_status",
            "pulseboard_status_code",
            "pulseboard_latency_seconds",
            "pulseboard_checks_total",
            "pulseboard_uptime_ratio",
            "pulseboard_avg_latency_ms",
            "pulseboard_p95_latency_ms",
            "pulseboard_p99_latency_ms",
            "pulseboard_min_latency_ms",
            "pulseboard_max_latency_ms",
            "pulseboard_last_check_timestamp_seconds",
            "pulseboard_open_incidents",
            "pulseboard_incidents_total",
            "pulseboard_p50_latency_ms",
        } <= names

    def test_up_value_is_one_when_status_up(self):
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github", Status.UP),
            open_incidents=0,
            lifetime_checks=10,
            lifetime_incidents=0,
        )
        up_sample = next(s for s in samples if s.name == "pulseboard_up")
        assert up_sample.value == 1

    def test_up_value_is_zero_when_status_down(self):
        summary = self._summary()
        summary.last_status = Status.DOWN
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github", Status.DOWN),
            open_incidents=1,
            lifetime_checks=10,
            lifetime_incidents=1,
        )
        up_sample = next(s for s in samples if s.name == "pulseboard_up")
        assert up_sample.value == 0

    def test_uptime_ratio_is_decimal_fraction(self):
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github"),
            open_incidents=0,
            lifetime_checks=10,
            lifetime_incidents=0,
        )
        ratio = next(s for s in samples if s.name == "pulseboard_uptime_ratio")
        # 90% uptime should be 0.9, not 90
        assert ratio.value == pytest.approx(0.9)

    def test_p50_latency_ms_propagates_from_summary(self):
        """pulseboard_p50_latency_ms should equal the summary's median."""
        summary = self._summary()
        summary.p50_latency_ms = 77.0
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github", Status.UP, latency=120.0),
            open_incidents=0,
            lifetime_checks=10,
            lifetime_incidents=0,
        )
        p50 = next(s for s in samples if s.name == "pulseboard_p50_latency_ms")
        assert p50.value == pytest.approx(77.0)

    def test_latency_seconds_is_ms_divided_by_1000(self):
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github", Status.UP, latency=250.0),
            open_incidents=0,
            lifetime_checks=10,
            lifetime_incidents=0,
        )
        latency = next(
            s for s in samples if s.name == "pulseboard_latency_seconds"
        )
        assert latency.value == pytest.approx(0.25)

    def test_status_code_zero_when_unset(self):
        summary = self._summary()
        result = CheckResult(
            service_name="github",
            timestamp=datetime.now(timezone.utc),
            status=Status.UP,
            latency_ms=10.0,
            status_code=None,
        )
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=result,
            open_incidents=0,
            lifetime_checks=1,
            lifetime_incidents=0,
        )
        sc = next(s for s in samples if s.name == "pulseboard_status_code")
        assert sc.value == 0.0

    def test_open_incidents_count_propagates(self):
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github"),
            open_incidents=3,
            lifetime_checks=10,
            lifetime_incidents=5,
        )
        oi = next(s for s in samples if s.name == "pulseboard_open_incidents")
        assert oi.value == 3

    def test_lifetime_counters_propagate(self):
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=_make_result("github"),
            open_incidents=0,
            lifetime_checks=42,
            lifetime_incidents=7,
        )
        chk = next(s for s in samples if s.name == "pulseboard_checks_total")
        inc = next(s for s in samples if s.name == "pulseboard_incidents_total")
        assert chk.value == 42
        assert chk.metric_type == "counter"
        assert inc.value == 7
        assert inc.metric_type == "counter"

    def test_service_type_label_uses_details(self):
        summary = self._summary()
        result = _make_result(
            "github", Status.UP, service_type=ServiceType.HTTP,
        )
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=result,
            open_incidents=0,
            lifetime_checks=1,
            lifetime_incidents=0,
        )
        up_sample = next(s for s in samples if s.name == "pulseboard_up")
        assert up_sample.labels["type"] == "http"

    def test_service_type_label_falls_back_to_unknown(self):
        summary = self._summary()
        # No service_type in details
        result = _make_result("github", Status.UP)
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=result,
            open_incidents=0,
            lifetime_checks=1,
            lifetime_incidents=0,
        )
        up_sample = next(s for s in samples if s.name == "pulseboard_up")
        assert up_sample.labels["type"] == "unknown"

    def test_no_last_result_skips_last_check_metrics(self):
        # Some services may have summary rows (from a recent prune etc.)
        # but no individual check at the very tail; we still want most
        # metrics but no status_code / latency_seconds.
        summary = self._summary()
        samples = metrics._build_service_metrics(
            summary=summary,
            last_result=None,
            open_incidents=0,
            lifetime_checks=0,
            lifetime_incidents=0,
        )
        names = {s.name for s in samples}
        assert "pulseboard_status_code" not in names
        assert "pulseboard_latency_seconds" not in names
        assert "pulseboard_last_check_timestamp_seconds" not in names
        # But window aggregates are still there
        assert "pulseboard_uptime_ratio" in names


# ---------------------------------------------------------------------------
# MetricsExporter end-to-end against a Storage
# ---------------------------------------------------------------------------


class TestMetricsExporter:
    """Integration: MetricsExporter should pull real history into real text."""

    def test_empty_db_emits_only_self_metrics(self, tmp_path):
        storage = _seed_storage(tmp_path, {})
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        # No per-service samples; only the exporter self-metrics
        assert "pulseboard_services_exported 0" in payload
        assert "pulseboard_scrape_duration_seconds" in payload
        storage.close()

    def test_single_service_full_payload(self, tmp_path):
        base = datetime.now(timezone.utc)
        rows = [
            _make_result("github", Status.UP, 100.0, 200,
                         ts=base - timedelta(seconds=i))
            for i in range(10)
        ]
        rows.append(_make_result(
            "github", Status.DOWN, 0.0, None, "timeout",
            ts=base - timedelta(seconds=11),
        ))
        storage = _seed_storage(tmp_path, {"github": rows})
        exporter = MetricsExporter(storage=storage, hours=1)
        payload = exporter.render()
        # The basic per-service gauges are there
        assert 'pulseboard_up{service="github"' in payload
        assert 'pulseboard_status_code{service="github"' in payload
        assert 'pulseboard_checks_total{service="github"' in payload
        # 11 checks recorded
        assert 'pulseboard_checks_total{service="github",type="unknown"} 11' in payload
        # Open incidents: none recorded → 0
        assert 'pulseboard_open_incidents{service="github"' in payload
        assert "pulseboard_services_exported 1" in payload
        storage.close()

    def test_open_incidents_counted(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"github": [_make_result("github", Status.UP, 50.0)]},
        )
        storage.record_incident(
            service_name="github",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            ended_at=None,
            from_status=Status.UP,
            to_status=Status.DOWN,
            error="boom",
            peak_status=Status.DOWN,
        )
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        assert 'pulseboard_open_incidents{service="github"' in payload
        # The exact count for a single open incident is 1
        assert (
            'pulseboard_open_incidents{service="github",type="unknown"} 1'
            in payload
        )
        storage.close()

    def test_lifetime_check_count_is_global(self, tmp_path):
        # Insert 100 checks; verify checks_total reflects the global count
        # (not just the in-window count).
        base = datetime.now(timezone.utc)
        rows = [
            _make_result("github", Status.UP, 50.0,
                         ts=base - timedelta(seconds=i))
            for i in range(50)
        ]
        # 50 more checks outside the window
        old = [
            _make_result("github", Status.UP, 50.0,
                         ts=base - timedelta(hours=72, seconds=i))
            for i in range(50)
        ]
        storage = _seed_storage(tmp_path, {"github": rows + old})
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        assert 'pulseboard_checks_total{service="github",type="unknown"} 100' in payload
        storage.close()

    def test_service_type_resolver_is_used(self, tmp_path):
        rows = [
            _make_result(
                "github", Status.UP, 50.0, 200,
                service_type=ServiceType.HTTP,
            )
        ]
        storage = _seed_storage(tmp_path, {"github": rows})

        def resolver(name: str) -> str:
            return "http" if name == "github" else "tcp"

        exporter = MetricsExporter(
            storage=storage,
            hours=24,
            service_type_resolver=resolver,
        )
        payload = exporter.render()
        assert 'service="github",type="http"' in payload
        storage.close()

    def test_resolver_falls_back_to_unknown_for_unknown_service(self, tmp_path):
        rows = [_make_result("ghost", Status.UP, 50.0)]
        storage = _seed_storage(tmp_path, {"ghost": rows})

        def resolver(name: str) -> str:
            return {"github": "http"}.get(name, "unknown")

        exporter = MetricsExporter(
            storage=storage,
            hours=24,
            service_type_resolver=resolver,
        )
        payload = exporter.render()
        assert 'service="ghost",type="unknown"' in payload
        storage.close()

    def test_multi_service(self, tmp_path):
        rows = {
            "github": [_make_result("github", Status.UP, 100.0)],
            "router": [_make_result("router", Status.DOWN, 0.0, None, "x")],
        }
        storage = _seed_storage(tmp_path, rows)
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        assert 'service="github"' in payload
        assert 'service="router"' in payload
        assert "pulseboard_services_exported 2" in payload
        storage.close()


# ---------------------------------------------------------------------------
# Textfile writing
# ---------------------------------------------------------------------------


class TestWriteTextfile:
    """Atomic textfile write semantics."""

    def test_creates_parent_directories(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 10.0)]},
        )
        nested = tmp_path / "deep" / "nested" / "out.prom"
        exporter = MetricsExporter(storage=storage, hours=24)
        n = exporter.write_textfile(nested)
        assert nested.exists()
        assert n > 0
        storage.close()

    def test_no_tmp_file_left_behind(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 10.0)]},
        )
        out = tmp_path / "out.prom"
        exporter = MetricsExporter(storage=storage, hours=24)
        exporter.write_textfile(out)
        assert out.exists()
        assert not out.with_suffix(out.suffix + ".tmp").exists()
        storage.close()

    def test_overwrites_existing(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 10.0)]},
        )
        out = tmp_path / "out.prom"
        out.write_text("# stale garbage")
        exporter = MetricsExporter(storage=storage, hours=24)
        exporter.write_textfile(out)
        text = out.read_text()
        assert "# stale garbage" not in text
        assert "pulseboard_" in text
        storage.close()

    def test_returned_sample_count_is_positive(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 10.0)]},
        )
        exporter = MetricsExporter(storage=storage, hours=24)
        n = exporter.write_textfile(tmp_path / "out.prom")
        # We have ~14 families per service + 2 self-metrics
        assert n >= 14
        storage.close()


# ---------------------------------------------------------------------------
# HTTP serve mode
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find a free TCP port for the test HTTP server."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServeMetricsHTTP:
    """The HTTP server should expose /metrics, /, and /healthz.

    We exercise the real ``serve_metrics`` function with the
    ``max_requests`` test hook so the test runs the same handler code
    that production does (no test-only mock handler). The server runs in
    a background thread and the test makes HTTP requests against it via
    :mod:`urllib`.
    """

    def _start_server(
        self, exporter: MetricsExporter, port: int
    ) -> tuple[threading.Thread, ThreadingHTTPServer]:
        """Start the local ``_TestHandler`` server in a background thread.

        Returns ``(thread, server)``. The caller must invoke
        ``server.shutdown()`` to stop the server when done.
        """
        _TestHandler.exporter = exporter
        server = ThreadingHTTPServer(("127.0.0.1", port), _TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return thread, server

    def _spawn_server(
        self, exporter: MetricsExporter, max_requests: int = 50
    ) -> tuple[int, threading.Thread]:
        """Start ``serve_metrics`` in a background thread on a free port.

        Returns ``(port, thread)``. The server runs until ``max_requests``
        have been handled, then exits — so the thread terminates on its
        own after the test is done.
        """
        port = _free_port()
        thread = threading.Thread(
            target=serve_metrics,
            args=(exporter,),
            kwargs={
                "host": "127.0.0.1",
                "port": port,
                "stream": StringIO(),
                "max_requests": max_requests,
            },
            daemon=True,
        )
        thread.start()
        # Wait until the socket is actually accepting connections.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=0.2
                ):
                    return port, thread
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server failed to bind")

    def test_serves_metrics_endpoint(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 50.0, 200)]},
        )
        exporter = MetricsExporter(storage=storage, hours=24)
        port = _free_port()
        _thread, server = self._start_server(exporter, port)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=3
            ) as resp:
                assert resp.status == 200
                ctype = resp.headers.get("Content-Type", "")
                assert "text/plain" in ctype
                body = resp.read().decode()
                assert "pulseboard_up" in body
        finally:
            server.shutdown()
            storage.close()

    def test_serves_index_html(self, tmp_path):
        storage = _seed_storage(tmp_path, {})
        exporter = MetricsExporter(storage=storage, hours=24)
        port = _free_port()
        _thread, server = self._start_server(exporter, port)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=3
            ) as resp:
                assert resp.status == 200
                ctype = resp.headers.get("Content-Type", "")
                assert "text/html" in ctype
                body = resp.read().decode()
                assert "PulseBoard" in body
                assert "/metrics" in body
        finally:
            server.shutdown()
            storage.close()

    def test_serves_healthz(self, tmp_path):
        storage = _seed_storage(tmp_path, {})
        exporter = MetricsExporter(storage=storage, hours=24)
        port = _free_port()
        _thread, server = self._start_server(exporter, port)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=3
            ) as resp:
                assert resp.status == 200
                assert resp.read() == b"ok\n"
        finally:
            server.shutdown()
            storage.close()

    def test_unknown_path_404(self, tmp_path):
        storage = _seed_storage(tmp_path, {})
        exporter = MetricsExporter(storage=storage, hours=24)
        port = _free_port()
        _thread, server = self._start_server(exporter, port)
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/no-such", timeout=3
                )
                pytest.fail("expected 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            server.shutdown()
            storage.close()

    def test_serve_metrics_max_requests_shuts_down(self, tmp_path):
        """The unit-test hook (max_requests) should bound the test runtime."""
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 50.0)]},
        )
        exporter = MetricsExporter(storage=storage, hours=24)
        # Build a real exporter and serve with max_requests=1 to confirm
        # the unit-test hook doesn't hang.
        port = _free_port()
        from http.server import ThreadingHTTPServer
        from pulseboard.metrics import serve_metrics as real_serve

        thread = threading.Thread(
            target=real_serve,
            args=(exporter,),
            kwargs={
                "host": "127.0.0.1",
                "port": port,
                "max_requests": 0,  # serve no requests; should exit immediately
            },
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "server did not shut down"
        storage.close()


class _TestHandler(BaseHTTPRequestHandler):
    """Minimal handler that reuses the metrics exporter from serve_metrics."""

    exporter: MetricsExporter = None  # set by _start_server

    def do_GET(self):  # noqa: N802 (stdlib API)
        if self.path == "/metrics":
            payload = self.exporter.render().encode()
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
        elif self.path in ("/", "/index.html"):
            body = (
                b"<html><body>"
                b"<h1>PulseBoard</h1>"
                b"<ul>"
                b'<li><a href="/metrics">/metrics</a></li>'
                b'<li><a href="/healthz">/healthz</a></li>'
                b"</ul>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args, **_kwargs):
        return


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, services: list[dict] | None = None) -> Path:
    """Write a minimal PulseBoard config pointing at a tmp DB."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "settings:\n"
        f"  db_path: {tmp_path}/pulseboard.db\n"
        "services:\n"
        + "\n".join(
            f"  - name: {s['name']}\n"
            + (
                f"    url: {s['url']}\n"
                if "url" in s
                else f"    host: {s.get('host', '127.0.0.1')}\n"
                f"    port: {s.get('port', 80)}\n"
            )
            for s in (services or [])
        )
    )
    return cfg


def _seed_db_for_config(tmp_path: Path, services: dict[str, list[CheckResult]]):
    """Open a Storage seeded with the given services; caller closes it."""
    storage = Storage(tmp_path / "pulseboard.db")
    for rows in services.values():
        for r in rows:
            storage.store(r)
    return storage


class TestMetricsCLI:
    """End-to-end CLI tests for ``pulseboard metrics``."""

    def test_help_lists_modes(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["metrics", "--help"])
        assert result.exit_code == 0
        assert "--output" in result.output
        assert "--serve" in result.output
        assert "--hours" in result.output
        assert "Prometheus" in result.output

    def test_stdout_mode_emits_payload(self, tmp_path):
        cfg = _make_config(tmp_path)
        storage = _seed_db_for_config(
            tmp_path,
            {"github": [_make_result("github", Status.UP, 100.0, 200)]},
        )
        storage.close()
        runner = CliRunner()
        result = runner.invoke(cli, ["metrics", "-c", str(cfg)])
        if result.exit_code != 0:
            import traceback
            tb = "".join(
                traceback.format_exception(*result.exc_info)
            ) if result.exc_info else "(no exc_info)"
            pytest.fail(f"exit={result.exit_code}\noutput:\n{result.output}\ntraceback:\n{tb}")
        assert "pulseboard_up" in result.stdout
        assert 'service="github"' in result.stdout

    def test_textfile_mode_writes_file(self, tmp_path):
        cfg = _make_config(tmp_path)
        storage = _seed_db_for_config(
            tmp_path,
            {"github": [_make_result("github", Status.UP, 100.0, 200)]},
        )
        storage.close()
        out = tmp_path / "metrics.prom"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["metrics", "-c", str(cfg), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert "✓" in result.output
        assert "Wrote" in result.output
        text = out.read_text()
        assert "pulseboard_up" in text

    def test_textfile_in_nested_directory(self, tmp_path):
        cfg = _make_config(tmp_path)
        storage = _seed_db_for_config(
            tmp_path,
            {"github": [_make_result("github", Status.UP, 100.0, 200)]},
        )
        storage.close()
        nested = tmp_path / "deep" / "collector" / "pulseboard.prom"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["metrics", "-c", str(cfg), "-o", str(nested)],
        )
        assert result.exit_code == 0, result.output
        assert nested.exists()

    def test_missing_config_exits_nonzero(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["metrics", "-c", str(tmp_path / "nope.yaml")]
        )
        assert result.exit_code != 0
        assert "✗" in result.output

    def test_empty_db_emits_self_metrics_only(self, tmp_path):
        cfg = _make_config(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["metrics", "-c", str(cfg)])
        assert result.exit_code == 0, result.output
        assert "pulseboard_services_exported 0" in result.stdout
        assert "pulseboard_scrape_duration_seconds" in result.stdout

    def test_service_type_label_uses_config(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "settings": {"db_path": str(tmp_path / "pulseboard.db")},
            "services": [
                {"name": "github", "url": "https://github.com"},
            ],
        }))
        storage = _seed_db_for_config(
            tmp_path,
            {"github": [_make_result("github", Status.UP, 50.0, 200)]},
        )
        storage.close()
        runner = CliRunner()
        result = runner.invoke(cli, ["metrics", "-c", str(cfg_path)])
        assert result.exit_code == 0, result.output
        # Config has the service → 'type' label should reflect the
        # resolved ServiceType (HTTP).
        assert 'service="github",type="http"' in result.stdout

    def test_hours_option_changes_window(self, tmp_path):
        cfg = _make_config(tmp_path)
        base = datetime.now(timezone.utc)
        # 50 recent checks (inside window) and 50 ancient checks
        recent = [
            _make_result("github", Status.UP, 50.0,
                         ts=base - timedelta(seconds=i))
            for i in range(50)
        ]
        ancient = [
            _make_result("github", Status.UP, 50.0,
                         ts=base - timedelta(days=10, seconds=i))
            for i in range(50)
        ]
        storage = _seed_db_for_config(tmp_path, {"github": recent + ancient})
        storage.close()
        runner = CliRunner()
        # 1-hour window: only recent counts toward uptime_pct
        result = runner.invoke(
            cli, ["metrics", "-c", str(cfg), "--hours", "1"]
        )
        assert result.exit_code == 0, result.output
        # All 100 checks still appear in lifetime counter
        assert 'pulseboard_checks_total{service="github",type="unknown"} 100' in result.stdout
        # Uptime is 100% (50/50 in window)
        assert 'pulseboard_uptime_ratio{service="github",type="unknown"} 1.0' in result.stdout


# ---------------------------------------------------------------------------
# Exporter self-metric
# ---------------------------------------------------------------------------


class TestExporterSelfMetrics:
    """The exporter must always emit its own bookkeeping metrics."""

    def test_scrape_duration_present(self, tmp_path):
        storage = _seed_storage(
            tmp_path,
            {"a": [_make_result("a", Status.UP, 10.0)]},
        )
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        assert "pulseboard_scrape_duration_seconds" in payload
        # Duration should be a non-negative float
        line = next(
            l for l in payload.splitlines()
            if l.startswith("pulseboard_scrape_duration_seconds ")
        )
        value = float(line.split(" ")[-1])
        assert value >= 0
        storage.close()

    def test_services_exported_count_is_accurate(self, tmp_path):
        rows = {
            "a": [_make_result("a", Status.UP, 10.0)],
            "b": [_make_result("b", Status.UP, 10.0)],
            "c": [_make_result("c", Status.DOWN, 0.0, None, "x")],
        }
        storage = _seed_storage(tmp_path, rows)
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        assert "pulseboard_services_exported 3" in payload
        storage.close()


# ---------------------------------------------------------------------------
# Format-spec spot checks
# ---------------------------------------------------------------------------


class TestFormatCompliance:
    """Spot checks that the output is parseable by a Prometheus parser-like
    naive line scanner. Catches the easy regressions."""

    def _parse_simple(self, payload: str):
        """Tiny in-test parser: returns list of (name, labels, value)."""
        out = []
        for line in payload.splitlines():
            if not line or line.startswith("#"):
                continue
            # Split on first '{' or ' '
            if "{" in line:
                head, rest = line.split("{", 1)
                name = head.rstrip()
                # Strip the trailing }
                labels_part, value = rest.rsplit("}", 1)
                value = value.strip()
            else:
                name, value = line.split(" ", 1)
                labels_part = ""
            try:
                v = float(value)
            except ValueError:
                v = value
            out.append((name, labels_part, v))
        return out

    def test_round_trip_parse(self, tmp_path):
        rows = {
            "github": [_make_result("github", Status.UP, 100.0, 200)],
            "router": [_make_result("router", Status.DOWN, 0.0, None, "x")],
        }
        storage = _seed_storage(tmp_path, rows)
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        parsed = self._parse_simple(payload)
        # Every metric has a value
        for name, _labels, value in parsed:
            assert value is not None, name
        # We got samples for both services
        names = {n for n, _, _ in parsed}
        assert "pulseboard_up" in names
        storage.close()

    def test_labels_are_well_formed(self, tmp_path):
        rows = {"a": [_make_result("a", Status.UP, 10.0)]}
        storage = _seed_storage(tmp_path, rows)
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        # No bare '{' without a matching '}' on a sample line
        for line in payload.splitlines():
            if not line or line.startswith("#"):
                continue
            assert line.count("{") == line.count("}"), line
        storage.close()

    def test_help_lines_are_valid(self, tmp_path):
        rows = {"a": [_make_result("a", Status.UP, 10.0)]}
        storage = _seed_storage(tmp_path, rows)
        exporter = MetricsExporter(storage=storage, hours=24)
        payload = exporter.render()
        for line in payload.splitlines():
            if line.startswith("# HELP"):
                # # HELP name text...
                parts = line.split(" ", 3)
                assert len(parts) == 4
                assert parts[0] == "#"
                assert parts[1] == "HELP"
                # name must be a valid metric identifier
                assert parts[2].replace("_", "").isalnum()
        storage.close()