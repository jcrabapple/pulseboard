"""Tests for auto-pruning of old check history during the watch loop.

The ``history_days`` setting (default 30) is loaded from config but was
never consumed by the watch loop — the SQLite database grew unbounded
during long-term monitoring. These tests cover the auto-prune helper
that the watch loop calls after storing each batch of results.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pulseboard.models import CheckResult, Status
from pulseboard.storage import Storage


def _store_sample(storage: Storage, name: str, ts: datetime) -> None:
    """Store a single check result at the given timestamp."""
    storage.store(
        CheckResult(
            service_name=name,
            timestamp=ts,
            status=Status.UP,
            latency_ms=10.0,
        )
    )


def test_auto_prune_with_history_days_deletes_old_records(tmp_path: Path):
    """auto_prune() removes records older than history_days."""
    db = tmp_path / "test.db"
    storage = Storage(db)

    now = datetime.now(timezone.utc)

    # An "old" record 60 days ago — should be pruned with history_days=30.
    _store_sample(storage, "svc-old", now - timedelta(days=60))
    # A "recent" record — should survive.
    _store_sample(storage, "svc-new", now - timedelta(days=1))

    deleted = storage.auto_prune(history_days=30)
    assert deleted == 1

    # Old record gone, recent record kept.
    assert len(storage.get_recent("svc-old", limit=10)) == 0
    assert len(storage.get_recent("svc-new", limit=10)) == 1

    storage.close()


def test_auto_prune_with_zero_history_days_is_noop(tmp_path: Path):
    """When history_days is 0, auto_prune should delete nothing (disabled)."""
    db = tmp_path / "test.db"
    storage = Storage(db)

    now = datetime.now(timezone.utc)
    _store_sample(storage, "svc", now - timedelta(days=365))

    deleted = storage.auto_prune(history_days=0)
    assert deleted == 0
    assert len(storage.get_recent("svc", limit=10)) == 1

    storage.close()


def test_auto_prune_preserves_recent_records(tmp_path: Path):
    """auto_prune keeps all records within the history_days window."""
    db = tmp_path / "test.db"
    storage = Storage(db)

    now = datetime.now(timezone.utc)
    # Records just inside the 7-day window.
    _store_sample(storage, "a", now - timedelta(days=6))
    _store_sample(storage, "b", now - timedelta(days=1))
    _store_sample(storage, "c", now)

    deleted = storage.auto_prune(history_days=7)
    assert deleted == 0
    assert len(storage.get_all_service_names()) == 3

    storage.close()


def test_auto_prune_also_prunes_old_incidents(tmp_path: Path):
    """auto_prune should also clean up incidents older than history_days."""
    db = tmp_path / "test.db"
    storage = Storage(db)

    now = datetime.now(timezone.utc)

    # Store an old check and an old incident (60 days ago).
    _store_sample(storage, "svc", now - timedelta(days=60))
    storage.record_incident(
        service_name="svc",
        started_at=now - timedelta(days=60),
        ended_at=now - timedelta(days=59),
        from_status=Status.UP,
        to_status=Status.DOWN,
        error="old outage",
    )

    # With history_days=30, the old incident should be pruned too.
    deleted_checks = storage.auto_prune(history_days=30)
    assert deleted_checks >= 1

    # Incident should be gone (prune_incidents uses 90-day default,
    # but auto_prune should pass history_days to it).
    incidents = storage.get_incidents()
    assert len(incidents) == 0

    storage.close()
