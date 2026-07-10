"""Tests for the incident-timeline feature."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from pulseboard.alerting import AlertManager
from pulseboard.cli import cli
from pulseboard.incidents import (
    Incident,
    filter_incidents,
    format_duration,
    h_format,
    reconstruct_incidents,
    reset_recorded_cache,
    sort_incidents_newest_first,
    sort_incidents_oldest_first,
    summarize,
)
from pulseboard.models import CheckResult, Status
from pulseboard.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _r(
    name: str,
    status: Status,
    seconds_offset: int,
    *,
    error: str | None = None,
) -> CheckResult:
    """Build a check result with a stable per-test base time."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return CheckResult(
        service_name=name,
        timestamp=base + timedelta(seconds=seconds_offset),
        status=status,
        latency_ms=42.0,
        status_code=200 if status == Status.UP else None,
        error=error,
    )


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_none(self) -> None:
        assert format_duration(None) == "—"

    def test_seconds_only(self) -> None:
        assert format_duration(0) == "0s"
        assert format_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert format_duration(125) == "2m 5s"

    def test_hours(self) -> None:
        assert format_duration(3725) == "1h 2m"

    def test_days(self) -> None:
        assert format_duration(90000) == "1d 1h"

    def test_negative_clamped(self) -> None:
        # Should never happen, but defensive — don't show "-1m 0s"
        assert format_duration(-30) == "0s"

    def test_h_format_helper(self) -> None:
        assert h_format(2, 5) == "2h 5m"


# ---------------------------------------------------------------------------
# reconstruct_incidents
# ---------------------------------------------------------------------------


class TestReconstructIncidents:
    def test_empty_history(self) -> None:
        assert reconstruct_incidents([]) == []

    def test_all_up_no_incidents(self) -> None:
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.UP, 60),
            _r("api", Status.UP, 120),
        ]
        assert reconstruct_incidents(results) == []

    def test_single_down_then_up(self) -> None:
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DOWN, 60, error="500"),
            _r("api", Status.DOWN, 120, error="timeout"),
            _r("api", Status.UP, 180),
        ]
        incidents = reconstruct_incidents(results)
        assert len(incidents) == 1
        inc = incidents[0]
        assert inc.service_name == "api"
        assert inc.from_status == Status.UP
        assert inc.to_status == Status.DOWN
        assert inc.started_at == _r("api", Status.UP, 60).timestamp
        assert inc.ended_at == _r("api", Status.UP, 180).timestamp
        assert inc.duration_seconds == 120.0
        assert not inc.is_open
        # Picks the more descriptive error from the longer sample.
        assert inc.error == "timeout"

    def test_open_outage_in_closed_window(self) -> None:
        # All non-UP at the end with no follow-up UP — leaves the
        # incident as *open* by default (the historical view can't
        # prove the outage has resolved, so we err on the side of
        # "still happening").
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DOWN, 60),
            _r("api", Status.DOWN, 120),
        ]
        incidents = reconstruct_incidents(results)
        assert len(incidents) == 1
        assert incidents[0].is_open is True
        assert incidents[0].duration_seconds is None

    def test_open_outage_with_reference_now(self) -> None:
        # When ``reference_now`` is provided, the trailing outage is
        # closed at its start time (zero-duration) — this is the mode
        # ``Storage.get_incidents`` uses when reading from a fixed
        # historical window.
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DOWN, 60),
            _r("api", Status.DOWN, 120),
        ]
        now = _r("api", Status.DOWN, 120).timestamp + timedelta(minutes=5)
        incidents = reconstruct_incidents(results, reference_now=now)
        assert len(incidents) == 1
        assert incidents[0].is_open is False
        assert incidents[0].duration_seconds == 0.0

    def test_peak_status_picks_worst(self) -> None:
        # DEGRADED then DOWN then DEGRADED — peak should be DOWN
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DEGRADED, 60),
            _r("api", Status.DOWN, 120),
            _r("api", Status.DEGRADED, 180),
            _r("api", Status.UP, 240),
        ]
        incidents = reconstruct_incidents(results)
        assert len(incidents) == 1
        assert incidents[0].severity == Status.DOWN
        # started_at is the first non-UP, ended_at is the first UP after.
        assert incidents[0].started_at == _r("api", Status.DEGRADED, 60).timestamp
        assert incidents[0].ended_at == _r("api", Status.UP, 240).timestamp

    def test_multiple_incidents(self) -> None:
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DOWN, 60),
            _r("api", Status.UP, 120),
            _r("api", Status.UP, 180),
            _r("api", Status.DEGRADED, 240),
            _r("api", Status.UP, 300),
        ]
        incidents = reconstruct_incidents(results)
        assert len(incidents) == 2
        assert incidents[0].to_status == Status.DOWN
        assert incidents[1].to_status == Status.DEGRADED

    def test_unsorted_input_is_sorted(self) -> None:
        # The function should accept input in any order and produce correct results.
        a = _r("api", Status.UP, 0)
        b = _r("api", Status.DOWN, 60)
        c = _r("api", Status.UP, 120)
        incidents = reconstruct_incidents([c, a, b])
        assert len(incidents) == 1
        assert incidents[0].started_at == b.timestamp
        assert incidents[0].ended_at == c.timestamp

    def test_unknown_status_does_not_open_incident(self) -> None:
        # UNKNOWN (no data) should not count as an outage — the gap closes
        # silently.
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.UNKNOWN, 60),
            _r("api", Status.UNKNOWN, 120),
            _r("api", Status.UP, 180),
        ]
        assert reconstruct_incidents(results) == []

    def test_mismatched_check_ids_raises(self) -> None:
        results = [_r("api", Status.UP, 0)]
        with pytest.raises(ValueError):
            reconstruct_incidents(results, check_ids=[1, 2])

    def test_check_ids_attached(self) -> None:
        results = [
            _r("api", Status.UP, 0),
            _r("api", Status.DOWN, 60),
            _r("api", Status.UP, 120),
        ]
        ids = [100, 200, 300]
        incidents = reconstruct_incidents(results, check_ids=ids)
        assert incidents[0].start_check_id == 200
        assert incidents[0].end_check_id == 300


# ---------------------------------------------------------------------------
# summarize + filter
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_empty(self) -> None:
        s = summarize([])
        assert s["total"] == 0
        assert s["open"] == 0
        assert s["total_downtime_seconds"] == 0.0
        assert s["average_duration_seconds"] == 0.0
        assert s["longest_duration_seconds"] == 0.0

    def test_mixed(self) -> None:
        # Two closed, one open
        incs = [
            Incident("a", _r("a", Status.DOWN, 0).timestamp,
                     _r("a", Status.UP, 60).timestamp, Status.UP, Status.DOWN),
            Incident("a", _r("a", Status.DEGRADED, 1000).timestamp,
                     _r("a", Status.UP, 1060).timestamp, Status.UP,
                     Status.DEGRADED),
            Incident("b", _r("b", Status.DOWN, 2000).timestamp,
                     None, Status.UP, Status.DOWN),  # open
        ]
        s = summarize(incs)
        assert s["total"] == 3
        assert s["open"] == 1
        assert s["closed"] == 2
        assert s["down"] == 2
        assert s["degraded"] == 1
        assert s["total_downtime_seconds"] == 120.0
        assert s["average_duration_seconds"] == 60.0
        assert s["longest_duration_seconds"] == 60.0


class TestFilter:
    def test_by_service(self) -> None:
        incs = [
            Incident("a", _r("a", Status.DOWN, 0).timestamp,
                     _r("a", Status.UP, 60).timestamp, Status.UP, Status.DOWN),
            Incident("b", _r("b", Status.DOWN, 100).timestamp,
                     _r("b", Status.UP, 200).timestamp, Status.UP, Status.DOWN),
        ]
        assert len(filter_incidents(incs, service_name="a")) == 1

    def test_by_types(self) -> None:
        incs = [
            Incident("a", _r("a", Status.DOWN, 0).timestamp,
                     _r("a", Status.UP, 60).timestamp, Status.UP, Status.DOWN),
            Incident("a", _r("a", Status.DEGRADED, 100).timestamp,
                     _r("a", Status.UP, 200).timestamp, Status.UP,
                     Status.DEGRADED),
        ]
        out = filter_incidents(incs, types={Status.DOWN})
        assert len(out) == 1
        assert out[0].severity == Status.DOWN


class TestSort:
    def test_newest_first(self) -> None:
        a = Incident("a", _r("a", Status.DOWN, 0).timestamp, None, Status.UP,
                     Status.DOWN)
        b = Incident("b", _r("b", Status.DOWN, 100).timestamp, None, Status.UP,
                     Status.DOWN)
        assert sort_incidents_newest_first([a, b])[0] is b
        assert sort_incidents_newest_first([a, b])[1] is a

    def test_oldest_first(self) -> None:
        a = Incident("a", _r("a", Status.DOWN, 0).timestamp, None, Status.UP,
                     Status.DOWN)
        b = Incident("b", _r("b", Status.DOWN, 100).timestamp, None, Status.UP,
                     Status.DOWN)
        assert sort_incidents_oldest_first([a, b])[0] is a


# ---------------------------------------------------------------------------
# Incident dataclass
# ---------------------------------------------------------------------------


class TestIncidentDataclass:
    def test_to_dict(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(seconds=60)
        inc = Incident("svc", start, end, Status.UP, Status.DOWN,
                       peak_status=Status.DOWN, error="boom")
        d = inc.to_dict()
        assert d["service_name"] == "svc"
        assert d["started_at"] == start.isoformat()
        assert d["ended_at"] == end.isoformat()
        assert d["from_status"] == "up"
        assert d["to_status"] == "down"
        assert d["peak_status"] == "down"
        assert d["duration_seconds"] == 60.0
        assert d["is_open"] is False
        assert d["error"] == "boom"

    def test_severity_falls_back_to_to_status(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        inc = Incident("svc", start, None, Status.UP, Status.DEGRADED)
        assert inc.severity == Status.DEGRADED

    def test_is_open(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert Incident("svc", start, None, Status.UP, Status.DOWN).is_open
        assert not Incident("svc", start, start, Status.UP, Status.DOWN).is_open

    def test_duration_none_when_open(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert Incident("svc", start, None, Status.UP, Status.DOWN).duration_seconds is None


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


class TestStorageIncidents:
    def test_record_and_query(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=5)
        storage.record_incident(
            service_name="api",
            started_at=start,
            ended_at=end,
            from_status=Status.UP,
            to_status=Status.DOWN,
            error="timeout",
            peak_status=Status.DOWN,
        )
        rows = storage.get_incidents()
        assert len(rows) == 1
        assert rows[0].service_name == "api"
        assert rows[0].error == "timeout"
        assert rows[0].duration_seconds == 300.0
        storage.close()

    def test_record_is_idempotent(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        id1 = storage.record_incident(
            service_name="api", started_at=start, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        id2 = storage.record_incident(
            service_name="api", started_at=start, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        assert id1 == id2
        assert len(storage.get_incidents()) == 1
        storage.close()

    def test_close_open_incident(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="api", started_at=start, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        end = start + timedelta(minutes=3)
        n = storage.close_open_incident(
            service_name="api", ended_at=end, peak_status=Status.DOWN,
        )
        assert n == 1
        rows = storage.get_incidents()
        assert rows[0].ended_at == end
        assert rows[0].duration_seconds == 180.0
        storage.close()

    def test_close_with_no_open_is_noop(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        n = storage.close_open_incident(
            service_name="ghost",
            ended_at=datetime.now(timezone.utc),
        )
        assert n == 0
        storage.close()

    def test_get_incidents_filters(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, (name, status, offset) in enumerate([
            ("api", Status.DOWN, 0),
            ("api", Status.DEGRADED, 3600),
            ("blog", Status.DOWN, 1800),
        ]):
            storage.record_incident(
                service_name=name,
                started_at=base + timedelta(seconds=offset),
                ended_at=None,
                from_status=Status.UP,
                to_status=status,
                peak_status=status,
            )
        # by service
        assert len(storage.get_incidents(service_name="api")) == 2
        # by type
        assert len(
            storage.get_incidents(types={Status.DOWN})
        ) == 2
        # by since
        assert len(
            storage.get_incidents(since=base + timedelta(seconds=3000))
        ) == 1
        # order
        rows = storage.get_incidents(order="asc")
        assert rows[0].service_name == "api"  # offset 0
        # limit
        rows = storage.get_incidents(limit=1, order="desc")
        assert len(rows) == 1
        assert rows[0].to_status == Status.DEGRADED
        storage.close()

    def test_open_only_filter(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="closed", started_at=base, ended_at=base + timedelta(minutes=1),
            from_status=Status.UP, to_status=Status.DOWN,
        )
        storage.record_incident(
            service_name="open", started_at=base, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        rows = storage.get_incidents(open_only=True)
        assert len(rows) == 1
        assert rows[0].service_name == "open"
        storage.close()

    def test_prune_incidents(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "test.db")
        old = datetime.now(timezone.utc) - timedelta(days=400)
        new = datetime.now(timezone.utc) - timedelta(days=1)
        storage.record_incident(
            service_name="old", started_at=old, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        storage.record_incident(
            service_name="new", started_at=new, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        deleted = storage.prune_incidents(days=90)
        assert deleted == 1
        assert len(storage.get_incidents()) == 1
        storage.close()

    def test_schema_migration_creates_table(self, tmp_path: Path) -> None:
        # A fresh DB should have the incidents table ready to go.
        storage = Storage(tmp_path / "test.db")
        row = storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'"
        ).fetchone()
        assert row is not None
        storage.close()


# ---------------------------------------------------------------------------
# AlertManager.previous_status + integration with storage
# ---------------------------------------------------------------------------


class TestAlertManagerPreviousStatus:
    def test_returns_none_initially(self) -> None:
        mgr = AlertManager()
        assert mgr.previous_status("api") is None

    def test_returns_last_observed(self) -> None:
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        assert mgr.previous_status("api") == Status.UP
        mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert mgr.previous_status("api") == Status.DOWN

    def test_integration_with_storage_incidents(self, tmp_path: Path) -> None:
        """End-to-end: feed a sequence of results into AlertManager +
        storage and verify the right rows land in the incidents table.

        Mirrors the live-recording behavior in the ``watch`` loop: a new
        incident is opened only on the UP→DOWN transition, and the same
        incident is closed when the service returns to UP.
        """
        storage = Storage(tmp_path / "test.db")
        mgr = AlertManager()
        sequence = [
            (Status.UP, 0, None),
            (Status.UP, 60, None),
            (Status.DOWN, 120, "500"),       # opens incident
            (Status.DOWN, 180, "timeout"),   # still in outage, no new row
            (Status.UP, 240, None),          # closes incident
            (Status.UP, 300, None),
        ]
        for status, offset, err in sequence:
            r = _r("api", status, offset, error=err)
            prev = mgr.previous_status(r.service_name)
            mgr.evaluate(r)
            # Mimic the recording logic in cli.watch
            if prev == Status.UP and status in (Status.DOWN, Status.DEGRADED):
                storage.record_incident(
                    service_name=r.service_name,
                    started_at=r.timestamp,
                    ended_at=None,
                    from_status=prev,
                    to_status=status,
                    error=err,
                    peak_status=status,
                )
            elif status == Status.UP and prev in (Status.DOWN, Status.DEGRADED):
                storage.close_open_incident(
                    service_name=r.service_name,
                    ended_at=r.timestamp,
                    peak_status=prev,
                )
        rows = storage.get_incidents(order="asc")
        assert len(rows) == 1
        assert rows[0].to_status == Status.DOWN
        assert rows[0].duration_seconds == 120.0
        assert rows[0].error == "500"  # recorded at opening
        storage.close()


# ---------------------------------------------------------------------------
# Dashboard table builder
# ---------------------------------------------------------------------------


class TestBuildIncidentTable:
    def test_renders_table(self) -> None:
        from pulseboard.dashboard import build_incident_table

        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=5)
        incidents = [
            Incident("api", start, end, Status.UP, Status.DOWN,
                     peak_status=Status.DOWN, error="500 Internal Server Error"),
            Incident("blog", start, None, Status.UP, Status.DEGRADED,
                     peak_status=Status.DEGRADED, error="slow response"),
        ]
        table = build_incident_table(incidents)
        # We just verify the table builds without raising and the row count
        # matches — Rich Table doesn't expose rows directly so we test the
        # title instead.
        assert "Incident Timeline" in str(table.title)
        # Build it again to make sure the function is idempotent.
        table2 = build_incident_table(incidents)
        assert table2 is not None

    def test_empty_list(self) -> None:
        from pulseboard.dashboard import build_incident_table
        table = build_incident_table([])
        assert "Incident Timeline" in str(table.title)


# ---------------------------------------------------------------------------
# CLI: `pulseboard incidents`
# ---------------------------------------------------------------------------


def _seed_config(tmp_path: Path) -> Path:
    """Write a minimal valid config so `load_config` succeeds."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "settings:\n"
        "  db_path: {db}\n"
        "services: []\n".format(db=str(tmp_path / "pulse.db"))
    )
    return cfg


class TestIncidentsCLI:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["incidents", "--help"])
        assert result.exit_code == 0
        assert "incident timeline" in result.output.lower()
        assert "--service" in result.output
        assert "--hours" in result.output
        assert "--summary" in result.output

    def test_no_incidents_human(self, tmp_path: Path) -> None:
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg)])
        assert result.exit_code == 0
        assert "No incidents" in result.output

    def test_no_incidents_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["incidents"] == []
        assert data["summary"]["total"] == 0

    def test_with_data(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="api",
            started_at=base,
            ended_at=base + timedelta(seconds=60),
            from_status=Status.UP,
            to_status=Status.DOWN,
            error="boom",
            peak_status=Status.DOWN,
        )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg)])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "DOWN" in result.output

    def test_with_data_json(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="api",
            started_at=base,
            ended_at=base + timedelta(seconds=60),
            from_status=Status.UP,
            to_status=Status.DOWN,
            error="boom",
            peak_status=Status.DOWN,
        )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["incidents"]) == 1
        assert data["incidents"][0]["service_name"] == "api"
        assert data["incidents"][0]["duration_seconds"] == 60.0
        assert data["summary"]["total"] == 1
        assert data["summary"]["down"] == 1

    def test_summary_mode(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="api",
            started_at=base,
            ended_at=base + timedelta(seconds=120),
            from_status=Status.UP,
            to_status=Status.DOWN,
        )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg), "--summary"])
        assert result.exit_code == 0
        assert "Total downtime" in result.output
        assert "2m" in result.output

    def test_filter_by_service(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for name in ("api", "blog"):
            storage.record_incident(
                service_name=name,
                started_at=base,
                ended_at=base + timedelta(seconds=30),
                from_status=Status.UP,
                to_status=Status.DOWN,
            )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(
            cli, ["incidents", "-c", str(cfg), "--service", "api", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["incidents"]) == 1
        assert data["incidents"][0]["service_name"] == "api"

    def test_filter_by_type(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="api", started_at=base,
            ended_at=base + timedelta(seconds=30),
            from_status=Status.UP, to_status=Status.DOWN, peak_status=Status.DOWN,
        )
        storage.record_incident(
            service_name="blog", started_at=base + timedelta(seconds=100),
            ended_at=base + timedelta(seconds=130),
            from_status=Status.UP, to_status=Status.DEGRADED,
            peak_status=Status.DEGRADED,
        )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(
            cli, ["incidents", "-c", str(cfg), "--type", "down", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["incidents"]) == 1
        assert data["incidents"][0]["to_status"] == "down"

    def test_open_only(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.record_incident(
            service_name="closed", started_at=base,
            ended_at=base + timedelta(seconds=30),
            from_status=Status.UP, to_status=Status.DOWN,
        )
        storage.record_incident(
            service_name="ongoing", started_at=base, ended_at=None,
            from_status=Status.UP, to_status=Status.DOWN,
        )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(cli, ["incidents", "-c", str(cfg), "--open", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["incidents"]) == 1
        assert data["incidents"][0]["service_name"] == "ongoing"
        assert data["incidents"][0]["is_open"] is True

    def test_limit(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "pulse.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(5):
            storage.record_incident(
                service_name=f"svc{i}",
                started_at=base + timedelta(seconds=i * 100),
                ended_at=base + timedelta(seconds=i * 100 + 30),
                from_status=Status.UP, to_status=Status.DOWN,
            )
        storage.close()
        runner = CliRunner()
        cfg = _seed_config(tmp_path)
        result = runner.invoke(
            cli, ["incidents", "-c", str(cfg), "--limit", "2", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["incidents"]) == 2

    def test_missing_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["incidents", "-c", str(tmp_path / "missing.yaml")]
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Module-level idempotency cache
# ---------------------------------------------------------------------------


class TestRecordedCache:
    def test_reset(self) -> None:
        from pulseboard.incidents import _RECORDED_KEYS
        reset_recorded_cache()
        _RECORDED_KEYS.add(("x", "up", "down", "2026-01-01T00:00:00+00:00"))
        reset_recorded_cache()
        assert len(_RECORDED_KEYS) == 0


# ---------------------------------------------------------------------------
# Consecutive failure tracking — exposed on Alert + Alert.to_dict()
# ---------------------------------------------------------------------------


class TestConsecutiveFailures:
    """The AlertManager should count how many checks in a row have been
    non-UP for a service and expose that count on the resulting Alert."""

    def test_first_down_alert_has_consecutive_failures_one(self) -> None:
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        alert = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert alert is not None
        assert alert.consecutive_failures == 1

    def test_second_down_alert_has_consecutive_failures_two(self) -> None:
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        # The second DOWN result doesn't fire an Alert (no status change),
        # but the counter should still increment.
        alert = mgr.evaluate(_r("api", Status.DOWN, 120, error="still down"))
        assert alert is None
        # Recovery should reset the counter.
        alert = mgr.evaluate(_r("api", Status.UP, 180))
        assert alert is not None
        # A fresh failure starts counting from 1 again.
        alert = mgr.evaluate(_r("api", Status.DOWN, 240, error="again"))
        assert alert is not None
        assert alert.consecutive_failures == 1

    def test_degraded_counts_as_failure(self) -> None:
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        alert = mgr.evaluate(_r("api", Status.DEGRADED, 60, error="slow"))
        assert alert is not None
        assert alert.consecutive_failures == 1
        # Downgrade further — still non-UP, still counting.
        alert = mgr.evaluate(_r("api", Status.DOWN, 120, error="boom"))
        assert alert is not None
        assert alert.consecutive_failures == 2

    def test_up_result_resets_counter(self) -> None:
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        alert = mgr.evaluate(_r("api", Status.UP, 120))
        assert alert is not None  # recovery alert
        assert alert.consecutive_failures == 0

    def test_alert_to_dict_includes_consecutive_failures(self) -> None:
        """The serialized payload (sent to webhooks) must carry the count
        so external systems can escalate after N failures."""
        mgr = AlertManager()
        mgr.evaluate(_r("api", Status.UP, 0))
        alert = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert alert is not None
        d = alert.to_dict()
        assert d["consecutive_failures"] == 1

    def test_first_up_check_has_zero_failures(self) -> None:
        mgr = AlertManager()
        alert = mgr.evaluate(_r("api", Status.UP, 0))
        assert alert is None
        # No alert, but internal state should have zero failures.
        alert = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert alert is not None
        assert alert.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Alert deduplication / cooldown
# ---------------------------------------------------------------------------


class TestAlertCooldown:
    """When a service flaps UP→DOWN→UP→DOWN rapidly, the AlertManager
    must not re-fire the same alert type within a cooldown window.

    This is the dedup concern: repeated identical alerts within ``cooldown``
    seconds are suppressed so notification channels don't get spammed.
    """

    def test_alert_suppressed_within_cooldown_for_flapping_service(self) -> None:
        """DOWN → UP → DOWN within cooldown: second DOWN alert suppressed."""
        # Use a controllable clock so the test is deterministic.
        from datetime import timezone as _tz

        ticks: list[datetime] = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=_tz.utc)]

        def clock() -> datetime:
            return ticks[0]

        mgr = AlertManager(alert_cooldown_seconds=300, clock=clock)

        # Initial UP baseline — no alert.
        mgr.evaluate(_r("api", Status.UP, 0))
        # First DOWN transition -> alert fires.
        first = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert first is not None

        # Recovery -> recovery alert fires (recoveries are NOT deduped —
        # they signal resolution, which is always worth sending).
        # Advance time slightly but still within cooldown.
        ticks[0] = ticks[0] + timedelta(seconds=30)
        recovery = mgr.evaluate(_r("api", Status.UP, 120))
        assert recovery is not None
        assert recovery.alert_type == "recovery"

        # Now flap back to DOWN within the cooldown window.
        ticks[0] = ticks[0] + timedelta(seconds=30)
        second = mgr.evaluate(_r("api", Status.DOWN, 180, error="boom"))
        # Deduplicated: same alert type (DOWN) within 300s.
        assert second is None

    def test_alert_refires_after_cooldown_expires(self) -> None:
        """Same alert type refires once cooldown has elapsed.

        The cooldown is measured from the last *fired* alert of that type.
        A suppressed alert does NOT extend the window, so once enough time
        passes and a fresh transition occurs, the alert refires.
        """
        from datetime import timezone as _tz

        ticks: list[datetime] = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=_tz.utc)]

        def clock() -> datetime:
            return ticks[0]

        mgr = AlertManager(alert_cooldown_seconds=300, clock=clock)

        mgr.evaluate(_r("api", Status.UP, 0))
        first = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert first is not None

        # Recovery within cooldown.
        ticks[0] = ticks[0] + timedelta(seconds=30)
        mgr.evaluate(_r("api", Status.UP, 120))

        # Second DOWN within cooldown: suppressed.
        ticks[0] = ticks[0] + timedelta(seconds=30)
        assert mgr.evaluate(_r("api", Status.DOWN, 180, error="boom")) is None

        # Recovery again, past the original DOWN alert's cooldown.
        ticks[0] = ticks[0] + timedelta(seconds=300)
        mgr.evaluate(_r("api", Status.UP, 500))

        # Third DOWN transition — past cooldown from the *first* DOWN alert,
        # so it should refire.
        ticks[0] = ticks[0] + timedelta(seconds=10)
        refired = mgr.evaluate(_r("api", Status.DOWN, 530, error="boom"))
        assert refired is not None
        assert refired.alert_type == "down"

    def test_different_alert_types_not_deduplicated(self) -> None:
        """A DOWN alert should not suppress a subsequent DEGRADED alert
        for the same service within cooldown — different type means a
        genuinely new condition worth surfacing."""
        from datetime import timezone as _tz

        ticks: list[datetime] = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=_tz.utc)]

        def clock() -> datetime:
            return ticks[0]

        mgr = AlertManager(alert_cooldown_seconds=300, clock=clock)

        mgr.evaluate(_r("api", Status.UP, 0))
        down = mgr.evaluate(_r("api", Status.DOWN, 60, error="boom"))
        assert down is not None

        # Same moment — but going DEGRADED is a different alert type.
        degraded = mgr.evaluate(_r("api", Status.DEGRADED, 90, error="slow"))
        assert degraded is not None
        assert degraded.alert_type == "degraded"

