"""Tests for PulseBoard models and storage."""

from datetime import datetime, timezone

from pulseboard.models import CheckResult, ServiceConfig, ServiceType, Status
from pulseboard.storage import Storage


def test_check_result_to_dict():
    r = CheckResult(
        service_name="test",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=Status.UP,
        latency_ms=42.567,
        status_code=200,
    )
    d = r.to_dict()
    assert d["service_name"] == "test"
    assert d["status"] == "up"
    assert d["latency_ms"] == 42.57
    assert d["status_code"] == 200


def test_check_result_to_dict_includes_diagnostic_details():
    details = {
        "answers": ["192.0.2.1"],
        "content_checks": [{"check": "body_contains", "passed": True}],
    }
    result = CheckResult(
        service_name="diagnostic",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=Status.UP,
        latency_ms=12.5,
        details=details,
    )

    assert result.to_dict()["details"] == details


def test_check_result_is_up():
    assert CheckResult("x", datetime.now(timezone.utc), Status.UP, 10).is_up
    assert not CheckResult("x", datetime.now(timezone.utc), Status.DOWN, 10).is_up


def test_service_config_defaults():
    svc = ServiceConfig(name="test", url="https://example.com")
    assert svc.service_type == ServiceType.HTTP
    assert svc.interval == 60
    assert svc.timeout == 10
    assert svc.expected_status == 200


def test_storage_store_and_retrieve(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)

    result = CheckResult(
        service_name="example",
        timestamp=datetime.now(timezone.utc),
        status=Status.UP,
        latency_ms=100.0,
        status_code=200,
    )
    storage.store(result)

    results = storage.get_recent("example", limit=10)
    assert len(results) == 1
    assert results[0].service_name == "example"
    assert results[0].status == Status.UP

    storage.close()


def test_storage_summary(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)

    # Store some results
    for i in range(10):
        storage.store(
            CheckResult(
                service_name="svc",
                timestamp=datetime.now(timezone.utc),
                status=Status.UP if i < 8 else Status.DOWN,
                latency_ms=50.0 + i,
                status_code=200 if i < 8 else 500,
            )
        )

    summary = storage.get_summary("svc", hours=1)
    assert summary.total_checks == 10
    assert summary.successful_checks == 8
    assert summary.failed_checks == 2
    assert summary.uptime_pct == 80.0
    assert summary.avg_latency_ms > 0

    storage.close()


def test_storage_prune(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)

    storage.store(
        CheckResult(
            service_name="old",
            timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
            status=Status.UP,
            latency_ms=10.0,
        )
    )
    storage.store(
        CheckResult(
            service_name="new",
            timestamp=datetime.now(timezone.utc),
            status=Status.UP,
            latency_ms=10.0,
        )
    )

    deleted = storage.prune(days=30)
    assert deleted == 1

    results = storage.get_recent("old", limit=10)
    assert len(results) == 0
    results = storage.get_recent("new", limit=10)
    assert len(results) == 1

    storage.close()


def test_storage_all_summaries(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)

    storage.store(CheckResult("a", datetime.now(timezone.utc), Status.UP, 10))
    storage.store(CheckResult("b", datetime.now(timezone.utc), Status.DOWN, 100))

    summaries = storage.get_all_summaries(hours=1)
    names = {s.service_name for s in summaries}
    assert names == {"a", "b"}

    storage.close()
