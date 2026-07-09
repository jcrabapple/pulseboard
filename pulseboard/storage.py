"""SQLite storage layer for check results and history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import CheckResult, ServiceSummary, Status


class Storage:
    """SQLite-backed persistence for PulseBoard check results."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                status_code INTEGER,
                error TEXT,
                details TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_checks_service_ts
                ON checks(service_name, timestamp DESC);

            CREATE INDEX IF NOT EXISTS idx_checks_status
                ON checks(status, timestamp DESC);
        """)

    def store(self, result: CheckResult) -> None:
        """Store a check result."""
        self.conn.execute(
            """INSERT INTO checks (service_name, timestamp, status, latency_ms, status_code, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result.service_name,
                result.timestamp.isoformat(),
                result.status.value,
                result.latency_ms,
                result.status_code,
                result.error,
            ),
        )
        self.conn.commit()

    def store_many(self, results: list[CheckResult]) -> None:
        """Batch store check results."""
        self.conn.executemany(
            """INSERT INTO checks (service_name, timestamp, status, latency_ms, status_code, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    r.service_name,
                    r.timestamp.isoformat(),
                    r.status.value,
                    r.latency_ms,
                    r.status_code,
                    r.error,
                )
                for r in results
            ],
        )
        self.conn.commit()

    def get_recent(
        self, service_name: str, limit: int = 50
    ) -> list[CheckResult]:
        """Get recent check results for a service."""
        rows = self.conn.execute(
            """SELECT * FROM checks
               WHERE service_name = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (service_name, limit),
        ).fetchall()
        return [self._row_to_result(r) for r in rows]

    def get_summary(
        self, service_name: str, hours: int = 24
    ) -> ServiceSummary:
        """Compute summary stats for a service over a time window."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            """SELECT status, latency_ms FROM checks
               WHERE service_name = ? AND timestamp >= ?
               ORDER BY timestamp""",
            (service_name, since),
        ).fetchall()

        if not rows:
            return ServiceSummary(
                service_name=service_name,
                total_checks=0,
                successful_checks=0,
                failed_checks=0,
                uptime_pct=0.0,
                avg_latency_ms=0.0,
                min_latency_ms=0.0,
                max_latency_ms=0.0,
                last_status=Status.UNKNOWN,
                last_check=None,
            )

        total = len(rows)
        up_count = sum(1 for r in rows if r["status"] == "up")
        latencies = [r["latency_ms"] for r in rows if r["status"] == "up"]
        latencies_sorted = sorted(latencies) if latencies else [0]

        last_row = self.conn.execute(
            """SELECT timestamp, status FROM checks
               WHERE service_name = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (service_name,),
        ).fetchone()

        p95_idx = int(len(latencies_sorted) * 0.95) if latencies_sorted else 0
        p99_idx = int(len(latencies_sorted) * 0.99) if latencies_sorted else 0

        return ServiceSummary(
            service_name=service_name,
            total_checks=total,
            successful_checks=up_count,
            failed_checks=total - up_count,
            uptime_pct=round((up_count / total) * 100, 2) if total else 0,
            avg_latency_ms=round(sum(latencies) / len(latencies), 2) if latencies else 0,
            min_latency_ms=round(min(latencies_sorted), 2),
            max_latency_ms=round(max(latencies_sorted), 2),
            last_status=Status(last_row["status"]) if last_row else Status.UNKNOWN,
            last_check=datetime.fromisoformat(last_row["timestamp"]) if last_row else None,
            p95_latency_ms=round(latencies_sorted[min(p95_idx, len(latencies_sorted) - 1)], 2),
            p99_latency_ms=round(latencies_sorted[min(p99_idx, len(latencies_sorted) - 1)], 2),
        )

    def get_all_summaries(self, hours: int = 24) -> list[ServiceSummary]:
        """Get summaries for all tracked services."""
        rows = self.conn.execute(
            "SELECT DISTINCT service_name FROM checks"
        ).fetchall()
        return [self.get_summary(r["service_name"], hours) for r in rows]

    def prune(self, days: int = 30) -> int:
        """Delete check results older than N days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM checks WHERE timestamp < ?", (cutoff,)
        )
        self.conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> CheckResult:
        return CheckResult(
            service_name=row["service_name"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            status=Status(row["status"]),
            latency_ms=row["latency_ms"],
            status_code=row["status_code"],
            error=row["error"],
        )
