"""Tests for the alert history log — persistent record of fired alerts.

The AlertManager produces transient ``Alert`` objects that are dispatched
to notification channels.  Until now there has been no durable record of
which alerts were fired, when, and for what reason.  This module tests a
new ``alerts`` table in :class:`pulseboard.storage.Storage` that lets the
operator answer:

* ``pulseboard alerts``                — show recent alerts
* ``pulseboard alerts --service X``    — alerts for one service
* ``pulseboard alerts --hours 6``      — last 6 hours
* ``pulseboard alerts --json``         — machine-readable

Storage gains ``record_alert()`` and ``get_alerts()``.

Tests are written FIRST (RED) — the feature does not exist yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulseboard.alerting import Alert, AlertType
from pulseboard.models import CheckResult, Status
from pulseboard.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    service_name: str = "svc",
    status: Status = Status.DOWN,
    error: str = "connection failed",
    latency_ms: float = 0.0,
) -> CheckResult:
    return CheckResult(
        service_name=service_name,
        timestamp=datetime.now(timezone.utc),
        status=status,
        latency_ms=latency_ms,
        error=error,
    )


def _make_alert(
    service_name: str = "svc",
    alert_type: str = AlertType.DOWN,
    message: str = "🔴 svc is DOWN",
    consecutive_failures: int = 1,
) -> Alert:
    return Alert(
        service_name=service_name,
        alert_type=alert_type,
        result=_make_result(service_name=service_name),
        message=message,
        consecutive_failures=consecutive_failures,
    )


# ---------------------------------------------------------------------------
# Storage.record_alert + get_alerts
# ---------------------------------------------------------------------------

class TestRecordAlert:
    """``Storage.record_alert`` persists an alert and returns its row id."""

    def test_record_alert_creates_table(self, tmp_path):
        """The ``alerts`` table should auto-create on first record_alert call."""
        storage = Storage(tmp_path / "test.db")
        alert = _make_alert()
        row_id = storage.record_alert(alert)
        assert row_id > 0
        storage.close()

    def test_record_alert_returns_increasing_ids(self, tmp_path):
        """Multiple alerts should get distinct, monotonically increasing ids."""
        storage = Storage(tmp_path / "test.db")
        id1 = storage.record_alert(_make_alert(service_name="a"))
        id2 = storage.record_alert(_make_alert(service_name="b"))
        assert id2 > id1
        storage.close()

    def test_record_alert_persists_service_name(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(service_name="my-api"))
        alerts = storage.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["service_name"] == "my-api"
        storage.close()

    def test_record_alert_persists_alert_type(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(alert_type=AlertType.DEGRADED))
        alerts = storage.get_alerts()
        assert alerts[0]["alert_type"] == "degraded"
        storage.close()

    def test_record_alert_persists_message(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(message="🔴 svc is DOWN: timeout"))
        alerts = storage.get_alerts()
        assert alerts[0]["message"] == "🔴 svc is DOWN: timeout"
        storage.close()

    def test_record_alert_persists_consecutive_failures(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(consecutive_failures=3))
        alerts = storage.get_alerts()
        assert alerts[0]["consecutive_failures"] == 3
        storage.close()


class TestGetAlertsFilters:
    """``Storage.get_alerts`` supports filtering by service, time, and type."""

    def test_get_alerts_empty_returns_empty_list(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        assert storage.get_alerts() == []
        storage.close()

    def test_get_alerts_filter_by_service(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(service_name="api"))
        storage.record_alert(_make_alert(service_name="web"))
        alerts = storage.get_alerts(service_name="api")
        assert len(alerts) == 1
        assert alerts[0]["service_name"] == "api"
        storage.close()

    def test_get_alerts_filter_by_alert_type(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert(alert_type=AlertType.DOWN))
        storage.record_alert(_make_alert(alert_type=AlertType.RECOVERY))
        alerts = storage.get_alerts(alert_type="down")
        assert len(alerts) == 1
        assert alerts[0]["alert_type"] == "down"
        storage.close()

    def test_get_alerts_order_desc_newest_first(self, tmp_path):
        """Default order should be newest-first (desc)."""
        storage = Storage(tmp_path / "test.db")
        ts_jan = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_feb = datetime(2026, 2, 1, tzinfo=timezone.utc)
        # We can't directly control the stored timestamp (record_alert uses
        # now), but inserting in sequence and checking relative order works
        # because all happen within milliseconds — the database id ordering
        # reflects insertion order. Let's verify desc by id.
        id1 = storage.record_alert(_make_alert(service_name="first"))
        id2 = storage.record_alert(_make_alert(service_name="second"))
        alerts = storage.get_alerts(order="desc")
        # Newest (second alert) should come first
        assert alerts[0]["service_name"] == "second"
        assert alerts[1]["service_name"] == "first"
        storage.close()

    def test_get_alerts_filter_by_since(self, tmp_path):
        """Alerts before ``since`` should be excluded."""
        storage = Storage(tmp_path / "test.db")
        # Record an alert "in the past"
        storage.record_alert(_make_alert(service_name="old"))
        # Now filter with since = 1 second in the future — nothing should match
        future = datetime.now(timezone.utc) + timedelta(seconds=1)
        alerts = storage.get_alerts(since=future)
        # The alert we just inserted is in the past relative to future, but wait —
        # the alert's timestamp is also ~now, so future should exclude it.
        # Actually the stored timestamp is ~now, so filtering since=future
        # excludes it. Let's check.
        assert len(alerts) == 0
        storage.close()

    def test_get_alerts_with_limit(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        for i in range(5):
            storage.record_alert(_make_alert(service_name=f"svc-{i}"))
        alerts = storage.get_alerts(limit=3)
        assert len(alerts) == 3
        storage.close()


class TestAlertLogRowShape:
    """The returned rows should have all the fields an operator needs."""

    def test_row_has_timestamp(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert())
        alerts = storage.get_alerts()
        assert "timestamp" in alerts[0]
        # Timestamp should be ISO-parseable
        datetime.fromisoformat(alerts[0]["timestamp"])
        storage.close()

    def test_row_has_id(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert())
        alerts = storage.get_alerts()
        assert "id" in alerts[0]
        assert isinstance(alerts[0]["id"], int)
        storage.close()

    def test_row_has_status_value(self, tmp_path):
        """The alert row should carry the CheckResult's status value."""
        storage = Storage(tmp_path / "test.db")
        # For a recovery alert, the result status should be UP.
        result = CheckResult(
            service_name="svc",
            timestamp=datetime.now(timezone.utc),
            status=Status.UP,
            latency_ms=42.0,
        )
        alert = Alert(
            service_name="svc",
            alert_type=AlertType.RECOVERY,
            result=result,
            message="🟢 svc recovered",
        )
        storage.record_alert(alert)
        alerts = storage.get_alerts()
        assert alerts[0]["status"] == "up"
        storage.close()

    def test_row_has_latency(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert())
        # latency_ms is from the CheckResult inside the Alert
        alerts = storage.get_alerts()
        assert "latency_ms" in alerts[0]
        storage.close()

    def test_row_has_error(self, tmp_path):
        storage = Storage(tmp_path / "test.db")
        storage.record_alert(_make_alert())
        alerts = storage.get_alerts()
        assert alerts[0]["error"] == "connection failed"
        storage.close()


# ---------------------------------------------------------------------------
# Watch-loop integration: record_alert is called when an alert fires
# ---------------------------------------------------------------------------

class TestWatchLoopIntegration:
    """The watch loop should persist every fired alert to the alert log."""

    def test_watch_once_persists_alert_for_down_service(self, tmp_path):
        """When ``watch --once`` fires a DOWN alert, it should be in the log."""
        from unittest.mock import patch
        from click.testing import CliRunner
        from pulseboard.cli import cli
        from pulseboard.models import CheckResult as CR, Status as St
        from datetime import datetime, timezone

        config = tmp_path / "pulseboard.yaml"
        db_path = tmp_path / "test.db"
        config.write_text(
            f"settings:\n  db_path: {db_path}\n  alert_on_recovery: false\n"
            "services:\n"
            "  - name: down-svc\n"
            "    url: https://example.com\n"
        )

        fake_result = CR(
            service_name="down-svc",
            timestamp=datetime.now(timezone.utc),
            status=St.DOWN,
            latency_ms=0.0,
            error="connection refused",
        )

        def fake_run(services, history_provider=None):
            return [fake_result]

        with patch(
            "pulseboard.cli.run_all_checks_with_thresholds", side_effect=fake_run
        ):
            r = CliRunner().invoke(cli, ["watch", "-c", str(config), "--once"])

        assert r.exit_code == 0, r.output

        from pulseboard.storage import Storage
        storage = Storage(str(db_path))
        alerts = storage.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["service_name"] == "down-svc"
        assert alerts[0]["alert_type"] == "down"
        assert alerts[0]["error"] == "connection refused"
        storage.close()


# ---------------------------------------------------------------------------
# CLI: pulseboard alerts
# ---------------------------------------------------------------------------

class TestAlertsCLI:
    """End-to-end tests for the ``pulseboard alerts`` command."""

    def _seed_db(self, tmp_path):
        """Create a config + db with one alert, return config path."""
        config = tmp_path / "pulseboard.yaml"
        db_path = tmp_path / "test.db"
        config.write_text(
            f"settings:\n  db_path: {db_path}\nservices: []\n"
        )
        storage = Storage(str(db_path))
        storage.record_alert(_make_alert(
            service_name="api",
            alert_type=AlertType.DOWN,
            message="DOWN alert",
            consecutive_failures=2,
        ))
        storage.close()
        return str(config)

    def test_help_lists_options(self):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        r = CliRunner().invoke(cli, ["alerts", "--help"])
        assert r.exit_code == 0
        assert "--service" in r.output
        assert "--hours" in r.output
        assert "--json" in r.output
        assert "--type" in r.output
        assert "--limit" in r.output

    def test_empty_db_shows_friendly_message(self, tmp_path):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        config = tmp_path / "pulseboard.yaml"
        db_path = tmp_path / "test.db"
        config.write_text(f"settings:\n  db_path: {db_path}\nservices: []\n")
        r = CliRunner().invoke(cli, ["alerts", "-c", str(config)])
        assert r.exit_code == 0
        assert "No alerts" in r.output

    def test_human_output_shows_alert(self, tmp_path):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        config = self._seed_db(tmp_path)
        r = CliRunner().invoke(cli, ["alerts", "-c", config])
        assert r.exit_code == 0
        assert "api" in r.output
        assert "DOWN" in r.output

    def test_json_output(self, tmp_path):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        import json as _json
        config = self._seed_db(tmp_path)
        r = CliRunner().invoke(cli, ["alerts", "-c", config, "--json"])
        assert r.exit_code == 0
        data = _json.loads(r.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["service_name"] == "api"
        assert data[0]["alert_type"] == "down"
        assert data[0]["consecutive_failures"] == 2

    def test_filter_by_service(self, tmp_path):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        config = tmp_path / "pulseboard.yaml"
        db_path = tmp_path / "test.db"
        config.write_text(f"settings:\n  db_path: {db_path}\nservices: []\n")
        storage = Storage(str(db_path))
        storage.record_alert(_make_alert(service_name="api"))
        storage.record_alert(_make_alert(service_name="web"))
        storage.close()
        r = CliRunner().invoke(cli, ["alerts", "-c", str(config), "-s", "api"])
        assert r.exit_code == 0
        assert "api" in r.output
        assert "web" not in r.output

    def test_missing_config_exits_nonzero(self, tmp_path):
        from click.testing import CliRunner
        from pulseboard.cli import cli
        r = CliRunner().invoke(
            cli, ["alerts", "-c", str(tmp_path / "nonexistent.yaml")]
        )
        assert r.exit_code != 0
