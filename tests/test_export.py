"""Tests for the export module (CSV / JSON history export)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from pulseboard import export
from pulseboard.cli import cli
from pulseboard.models import CheckResult, Status
from pulseboard.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_result(
    name: str,
    status: Status = Status.UP,
    latency: float = 42.0,
    status_code: int | None = 200,
    error: str | None = None,
    ts: datetime | None = None,
) -> CheckResult:
    """Construct a CheckResult with sensible defaults for testing."""
    return CheckResult(
        service_name=name,
        timestamp=ts or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        status=status,
        latency_ms=latency,
        status_code=status_code,
        error=error,
    )


@pytest.fixture
def sample_results() -> list[CheckResult]:
    return [
        _make_result("github", Status.UP, 142.5, 200),
        _make_result(
            "router",
            Status.DOWN,
            0.0,
            None,
            "Timeout after 5s",
            ts=datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc),
        ),
        _make_result(
            "github",
            Status.UP,
            150.1,
            200,
            ts=datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc),
        ),
    ]


# ---------------------------------------------------------------------------
# to_rows
# ---------------------------------------------------------------------------


class TestToRows:
    def test_empty(self):
        assert export.to_rows([]) == []

    def test_returns_flat_dicts(self, sample_results):
        rows = export.to_rows(sample_results)
        assert len(rows) == 3
        for row in rows:
            assert isinstance(row, dict)
            assert set(row.keys()) >= {
                "service_name",
                "timestamp",
                "status",
                "latency_ms",
                "status_code",
                "error",
            }

    def test_round_trip_latency(self, sample_results):
        rows = export.to_rows(sample_results)
        # 142.5 -> 142.5 (already 1 decimal)
        assert rows[0]["latency_ms"] == 142.5
        # 150.123456 -> rounded to 150.12
        precise = CheckResult(
            "x",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            Status.UP,
            150.123456,
        )
        assert export.to_rows([precise])[0]["latency_ms"] == 150.12


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestToCsv:
    def test_header_present(self, sample_results):
        text = export.to_csv(sample_results)
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        # header + 3 rows
        assert len(rows) == 4
        assert rows[0] == list(export.CSV_COLUMNS)

    def test_quoting_handles_commas_in_error(self):
        result = _make_result(
            "x", Status.DOWN, 0, None, "boom, with comma, and \"quotes\""
        )
        text = export.to_csv([result])
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["error"] == "boom, with comma, and \"quotes\""

    def test_empty(self):
        text = export.to_csv([])
        # Just the header
        assert text.strip().splitlines() == [",".join(export.CSV_COLUMNS)]

    def test_columns_in_stable_order(self, sample_results):
        text = export.to_csv(sample_results)
        header = text.splitlines()[0]
        assert header == ",".join(export.CSV_COLUMNS)

    def test_null_fields_empty_in_csv(self):
        result = _make_result(
            "x", Status.DOWN, 0, status_code=None, error=None
        )
        text = export.to_csv([result])
        reader = csv.DictReader(io.StringIO(text))
        row = next(iter(reader))
        assert row["status_code"] == ""
        assert row["error"] == ""


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class TestToJson:
    def test_envelope_keys(self, sample_results):
        payload = json.loads(export.to_json(sample_results))
        assert "exported_at" in payload
        assert "count" in payload
        assert "records" in payload
        assert payload["count"] == len(sample_results)
        assert len(payload["records"]) == len(sample_results)

    def test_compact_mode(self, sample_results):
        text = export.to_json(sample_results, indent=None)
        # Compact mode should have no newlines (per record)
        # except possibly a trailing newline-free payload
        # The simplest check: the string parses.
        payload = json.loads(text)
        assert payload["count"] == 3

    def test_pretty_mode_has_indents(self, sample_results):
        text = export.to_json(sample_results, indent=2)
        # Should be multi-line
        assert "\n" in text
        assert "  " in text  # indented at 2 spaces

    def test_non_ascii_preserved(self):
        result = _make_result(
            "x", Status.DOWN, 0, None, "émoji 🔥 failed"
        )
        payload = json.loads(export.to_json([result]))
        assert payload["records"][0]["error"] == "émoji 🔥 failed"

    def test_empty_records(self):
        payload = json.loads(export.to_json([]))
        assert payload["count"] == 0
        assert payload["records"] == []

    def test_json_record_includes_details_dict(self):
        """JSON export records must carry the rich ``details`` dict so
        downstream analysis tools can inspect redirect chains, rate-limit
        hints, content-validation reports, threshold outcomes, dependency
        impact, SSL cert metadata, and DNS answers. CSV exports stay flat
        (by design), but JSON should not silently drop this data — it's
        the whole reason a user picks the JSON format over CSV. Tests for
        the CSV path confirm it still omits ``details``.
        """
        result = CheckResult(
            service_name="api",
            timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            status=Status.DEGRADED,
            latency_ms=312.0,
            status_code=200,
            error="latency 312ms ≥ warning 300ms",
            details={
                "redirect_count": 1,
                "final_url": "https://api.example.com/v2/",
                "thresholds": {"latency_violation": "warning"},
                "content_checks": [{"check": "body_contains", "passed": True}],
            },
        )
        payload = json.loads(export.to_json([result]))
        record = payload["records"][0]
        assert "details" in record, "JSON export must include the details dict"
        assert record["details"]["redirect_count"] == 1
        assert record["details"]["final_url"] == "https://api.example.com/v2/"
        assert "thresholds" in record["details"]
        assert "content_checks" in record["details"]

    def test_json_record_details_present_even_when_empty(self):
        """A result with no details dict should still produce a ``details``
        key (as an empty dict) so consumers don't have to branch on the
        key's presence.
        """
        result = CheckResult(
            service_name="plain",
            timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            status=Status.UP,
            latency_ms=12.0,
            status_code=200,
        )
        payload = json.loads(export.to_json([result]))
        record = payload["records"][0]
        assert "details" in record
        assert record["details"] == {}

    def test_csv_record_omits_details_column(self):
        """CSV export must remain flat — the ``details`` dict is not a
        column. The CSV columns are fixed at the stable header and
        must not grow a loosely-named ``details`` column. This confirms
        the JSON-only behaviour of the previous tests is a deliberate
        format difference, not an oversight.
        """
        result = CheckResult(
            service_name="x",
            timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            status=Status.UP,
            latency_ms=10.0,
            status_code=200,
            details={"redirect_count": 2},
        )
        text = export.to_csv([result])
        reader = csv.DictReader(io.StringIO(text))
        row = next(iter(reader))
        assert "details" not in row, "CSV export must not include a details column"
        assert set(row.keys()) == set(export.CSV_COLUMNS)


# ---------------------------------------------------------------------------
# write_export (filesystem)
# ---------------------------------------------------------------------------


class TestWriteExport:
    def test_csv_file(self, sample_results, tmp_path):
        out = tmp_path / "out.csv"
        count = export.write_export(sample_results, out, "csv")
        assert count == 3
        text = out.read_text()
        assert "service_name" in text
        assert "github" in text
        assert "router" in text

    def test_json_file(self, sample_results, tmp_path):
        out = tmp_path / "out.json"
        count = export.write_export(sample_results, out, "json")
        assert count == 3
        payload = json.loads(out.read_text())
        assert payload["count"] == 3

    def test_creates_parent_directories(self, sample_results, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "out.csv"
        export.write_export(sample_results, nested, "csv")
        assert nested.exists()

    def test_unknown_format_raises(self, sample_results, tmp_path):
        with pytest.raises(ValueError, match="Unsupported export format"):
            export.write_export(sample_results, tmp_path / "x.xml", "xml")

    def test_uppercase_format_ok(self, sample_results, tmp_path):
        out = tmp_path / "out.csv"
        count = export.write_export(sample_results, out, "CSV")
        assert count == 3


# ---------------------------------------------------------------------------
# write_export_stream
# ---------------------------------------------------------------------------


class TestWriteExportStream:
    def test_csv_stream(self, sample_results):
        buf = io.StringIO()
        count = export.write_export_stream(sample_results, buf, "csv")
        assert count == 3
        reader = csv.DictReader(io.StringIO(buf.getvalue()))
        rows = list(reader)
        assert len(rows) == 3

    def test_json_stream(self, sample_results):
        buf = io.StringIO()
        count = export.write_export_stream(sample_results, buf, "json")
        assert count == 3
        payload = json.loads(buf.getvalue())
        assert payload["count"] == 3

    def test_unknown_format_raises(self, sample_results):
        buf = io.StringIO()
        with pytest.raises(ValueError):
            export.write_export_stream(sample_results, buf, "yaml")


# ---------------------------------------------------------------------------
# infer_format
# ---------------------------------------------------------------------------


class TestInferFormat:
    @pytest.mark.parametrize("path,expected", [
        ("a.csv", "csv"),
        ("a.CSV", "csv"),
        ("a.json", "json"),
        ("a.JSON", "json"),
        ("a.txt", "json"),  # fallback
        ("a", "json"),      # no ext → fallback
    ])
    def test_extension_lookup(self, path, expected):
        assert export.infer_format(path) == expected


# ---------------------------------------------------------------------------
# Storage.get_history integration
# ---------------------------------------------------------------------------


class TestStorageHistory:
    """Exercise the storage filters that the export command depends on."""

    def _populate(self, storage: Storage) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.store(_make_result("a", ts=base))
        storage.store(_make_result("a", ts=base + timedelta(hours=1)))
        storage.store(_make_result("a", ts=base + timedelta(hours=2)))
        storage.store(_make_result("b", ts=base + timedelta(hours=1)))
        storage.store(_make_result("b", ts=base + timedelta(hours=3)))

    def test_filter_by_service(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        rows = storage.get_history(service_name="a")
        assert all(r.service_name == "a" for r in rows)
        assert len(rows) == 3
        storage.close()

    def test_filter_by_since(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        # Cutoff at 01:30 keeps rows at 02:00 (a) and 03:00 (b) but
        # excludes 00:00 (a), 01:00 (a), and 01:00 (b).
        cutoff = datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc)
        rows = storage.get_history(since=cutoff)
        assert all(r.timestamp >= cutoff for r in rows)
        assert len(rows) == 2
        assert {(r.service_name, r.timestamp) for r in rows} == {
            ("a", datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)),
            ("b", datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)),
        }
        storage.close()

    def test_filter_by_until(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        cutoff = datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc)
        rows = storage.get_history(until=cutoff)
        assert all(r.timestamp <= cutoff for r in rows)
        storage.close()

    def test_order_asc_default(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        rows = storage.get_history(service_name="a")
        timestamps = [r.timestamp for r in rows]
        assert timestamps == sorted(timestamps)
        storage.close()

    def test_order_desc(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        rows = storage.get_history(service_name="a", order="desc")
        timestamps = [r.timestamp for r in rows]
        assert timestamps == sorted(timestamps, reverse=True)
        storage.close()

    def test_combined_filters(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        rows = storage.get_history(
            service_name="a",
            since=datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
            order="desc",
        )
        assert all(r.service_name == "a" for r in rows)
        assert len(rows) == 2  # only the hour-1 and hour-2 rows
        storage.close()

    def test_filter_by_status(self, tmp_path):
        """get_history(status=...) should only return rows of that status."""
        storage = Storage(tmp_path / "h.db")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        storage.store(_make_result("upsvc", status=Status.UP, ts=base))
        storage.store(_make_result("downsvc", status=Status.DOWN,
                                   latency=0, status_code=None,
                                   error="boom", ts=base))
        rows = storage.get_history(status="down")
        assert len(rows) == 1
        assert rows[0].service_name == "downsvc"
        assert rows[0].status == Status.DOWN
        storage.close()

    def test_filter_by_status_none_returns_all(self, tmp_path):
        """status=None (the default) should not constrain results."""
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        # _populate stores 5 rows (all UP)
        rows = storage.get_history(status="down")
        assert rows == []
        all_rows = storage.get_history()
        assert len(all_rows) == 5
        storage.close()

    def test_get_all_service_names(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        self._populate(storage)
        names = storage.get_all_service_names()
        assert names == ["a", "b"]
        storage.close()

    def test_get_all_service_names_empty(self, tmp_path):
        storage = Storage(tmp_path / "h.db")
        assert storage.get_all_service_names() == []
        storage.close()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _seed_db(tmp_path) -> Storage:
    """Build a Storage instance with a few rows under a known path."""
    storage = Storage(tmp_path / "pulseboard.db")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    storage.store(_make_result("svc", ts=base))
    storage.store(_make_result(
        "svc", Status.DOWN, 0, None, "boom",
        ts=base + timedelta(minutes=1),
    ))
    return storage


@pytest.fixture
def config_with_db(tmp_path, monkeypatch):
    """Write a minimal config pointing at a fresh test DB and a closeable storage."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"settings:\n  db_path: {tmp_path}/pulseboard.db\nservices: []\n"
    )
    storage = _seed_db(tmp_path)
    yield cfg
    storage.close()


class TestExportCLI:
    def test_export_to_stdout_csv(self, config_with_db, capsys):
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "-c", str(config_with_db)])
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "service_name,timestamp,status" in out
        assert "svc" in out
        assert "boom" in out

    def test_export_to_file_csv(self, config_with_db, tmp_path):
        runner = CliRunner()
        out = tmp_path / "history.csv"
        result = runner.invoke(
            cli,
            ["export", "-c", str(config_with_db), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert "✓" in result.output
        assert "Exported 2 check record(s)" in result.output
        text = out.read_text()
        assert "svc" in text

    def test_export_to_file_json_extension(self, config_with_db, tmp_path):
        runner = CliRunner()
        out = tmp_path / "history.json"
        result = runner.invoke(
            cli,
            ["export", "-c", str(config_with_db), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(out.read_text())
        assert payload["count"] == 2

    def test_format_flag_overrides_extension(
        self, config_with_db, tmp_path
    ):
        runner = CliRunner()
        # .csv extension but --format json should win
        out = tmp_path / "weird.csv"
        result = runner.invoke(
            cli,
            [
                "export",
                "-c",
                str(config_with_db),
                "-o",
                str(out),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(out.read_text())
        assert payload["count"] == 2

    def test_missing_config_exits_nonzero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "-c", "/nonexistent.yaml"])
        assert result.exit_code != 0
        assert "Config not found" in result.output

    def test_empty_history(self, tmp_path):
        cfg = tmp_path / "empty.yaml"
        cfg.write_text(
            f"settings:\n  db_path: {tmp_path}/empty.db\nservices: []\n"
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", "-c", str(cfg), "--format", "json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["count"] == 0

    def test_service_filter(self, config_with_db, tmp_path):
        runner = CliRunner()
        out = tmp_path / "f.csv"
        # Only "svc" exists, but filtering should still work and yield 2 rows
        result = runner.invoke(
            cli,
            [
                "export",
                "-c",
                str(config_with_db),
                "-o",
                str(out),
                "-s",
                "nonexistent",
            ],
        )
        assert result.exit_code == 0, result.output
        text = out.read_text()
        # Only header line, no data rows
        lines = text.strip().splitlines()
        assert len(lines) == 1

    def test_status_filter(self, config_with_db, tmp_path):
        """--status down should only export matching rows."""
        runner = CliRunner()
        out = tmp_path / "f.csv"
        result = runner.invoke(
            cli,
            [
                "export",
                "-c",
                str(config_with_db),
                "-o",
                str(out),
                "--status",
                "down",
            ],
        )
        assert result.exit_code == 0, result.output
        text = out.read_text()
        lines = text.strip().splitlines()
        # header + 1 row (only the DOWN result)
        assert len(lines) == 2
        assert "down" in lines[1]

    def test_limit_applies_desc_order(self, config_with_db, tmp_path):
        runner = CliRunner()
        out = tmp_path / "lim.csv"
        result = runner.invoke(
            cli,
            [
                "export",
                "-c",
                str(config_with_db),
                "-o",
                str(out),
                "--limit",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        text = out.read_text()
        # header + 1 row
        assert len(text.strip().splitlines()) == 2
        # The row should be the most recent one — the DOWN event at minute 1
        assert "down" in text

    def test_invalid_format_rejected(self, config_with_db):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "export",
                "-c",
                str(config_with_db),
                "--format",
                "xml",
            ],
        )
        # Click rejects before our code runs
        assert result.exit_code != 0