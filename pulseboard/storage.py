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
            # check_same_thread=False so the metrics exporter can
            # safely read this Storage from the ThreadingHTTPServer's
            # worker threads. WAL mode + a single writer (the monitor)
            # means concurrent reads are safe.
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
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

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                peak_status TEXT,
                error TEXT,
                UNIQUE(service_name, started_at)
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_service_started
                ON incidents(service_name, started_at DESC);

            CREATE INDEX IF NOT EXISTS idx_incidents_open
                ON incidents(service_name) WHERE ended_at IS NULL;
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

    def get_history(
        self,
        service_name: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        order: str = "asc",
    ) -> list[CheckResult]:
        """Query historical check results with optional filters.

        Args:
            service_name: If provided, only return rows for that service.
            since: If provided, only return rows at or after this time (UTC).
            until: If provided, only return rows at or before this time (UTC).
            order: "asc" (oldest first, default) or "desc" (newest first).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if service_name is not None:
            clauses.append("service_name = ?")
            params.append(service_name)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        direction = "DESC" if order.lower() == "desc" else "ASC"
        query = (
            f"SELECT * FROM checks {where} ORDER BY timestamp {direction}"
        )
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_result(r) for r in rows]

    def get_all_service_names(self) -> list[str]:
        """Return distinct service names that have at least one check record."""
        rows = self.conn.execute(
            "SELECT DISTINCT service_name FROM checks ORDER BY service_name"
        ).fetchall()
        return [r["service_name"] for r in rows]

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

    def count_checks(self, service_name: str | None = None) -> int:
        """Return the total number of stored check rows.

        When ``service_name`` is provided, count only that service's
        rows. Used by the metrics exporter for lifetime counters.
        """
        if service_name is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM checks"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM checks WHERE service_name = ?",
                (service_name,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_checks_by_service(self) -> dict[str, int]:
        """Return a mapping of ``service_name -> lifetime check count``.

        Used by the metrics exporter for ``pulseboard_checks_total``.
        """
        rows = self.conn.execute(
            """SELECT service_name, COUNT(*) AS n FROM checks
               GROUP BY service_name"""
        ).fetchall()
        return {r["service_name"]: int(r["n"]) for r in rows}

    def count_open_incidents_by_service(self) -> dict[str, int]:
        """Return a mapping of ``service_name -> open incident count``.

        Used by the metrics exporter for ``pulseboard_open_incidents``.
        """
        rows = self.conn.execute(
            """SELECT service_name, COUNT(*) AS n FROM incidents
               WHERE ended_at IS NULL
               GROUP BY service_name"""
        ).fetchall()
        return {r["service_name"]: int(r["n"]) for r in rows}

    def count_incidents(self, service_name: str | None = None) -> int:
        """Return the total number of stored incidents.

        When ``service_name`` is provided, count only that service's
        incidents.
        """
        if service_name is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM incidents"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM incidents WHERE service_name = ?",
                (service_name,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def prune(self, days: int = 30) -> int:
        """Delete check results older than N days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM checks WHERE timestamp < ?", (cutoff,)
        )
        self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Incidents
    # ------------------------------------------------------------------

    def record_incident(
        self,
        *,
        service_name: str,
        started_at: datetime,
        ended_at: datetime | None,
        from_status: Status | str,
        to_status: Status | str,
        error: str | None = None,
        peak_status: Status | str | None = None,
    ) -> int:
        """Insert a new incident row.

        The (service_name, started_at) UNIQUE constraint means re-recording
        the same incident is a silent no-op — useful when ``watch`` is
        restarted mid-outage and replays the same alert.

        Returns the row id of the inserted (or existing) incident.
        """
        from_s = from_status.value if isinstance(from_status, Status) else from_status
        to_s = to_status.value if isinstance(to_status, Status) else to_status
        peak_s = (
            peak_status.value
            if isinstance(peak_status, Status)
            else peak_status
        )
        try:
            cur = self.conn.execute(
                """INSERT INTO incidents
                       (service_name, started_at, ended_at, from_status,
                        to_status, peak_status, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    service_name,
                    started_at.isoformat(),
                    ended_at.isoformat() if ended_at else None,
                    from_s,
                    to_s,
                    peak_s,
                    error,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # Already recorded — return the existing row's id.
            row = self.conn.execute(
                """SELECT id FROM incidents
                   WHERE service_name = ? AND started_at = ?""",
                (service_name, started_at.isoformat()),
            ).fetchone()
            return int(row["id"]) if row else 0

    def close_open_incident(
        self,
        *,
        service_name: str,
        ended_at: datetime,
        peak_status: Status | str | None = None,
    ) -> int:
        """Close the oldest open incident for ``service_name``.

        Returns the number of rows updated (0 or 1). Used by the
        watcher when a service transitions back to UP.
        """
        peak_s = (
            peak_status.value
            if isinstance(peak_status, Status)
            else peak_status
        )
        if peak_s is not None:
            cur = self.conn.execute(
                """UPDATE incidents
                   SET ended_at = ?, peak_status = ?
                   WHERE id = (
                       SELECT id FROM incidents
                       WHERE service_name = ? AND ended_at IS NULL
                       ORDER BY started_at ASC LIMIT 1
                   )""",
                (ended_at.isoformat(), peak_s, service_name),
            )
        else:
            cur = self.conn.execute(
                """UPDATE incidents
                   SET ended_at = ?
                   WHERE id = (
                       SELECT id FROM incidents
                       WHERE service_name = ? AND ended_at IS NULL
                       ORDER BY started_at ASC LIMIT 1
                   )""",
                (ended_at.isoformat(), service_name),
            )
        self.conn.commit()
        return cur.rowcount

    def get_incidents(
        self,
        service_name: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        types: set[Status] | None = None,
        open_only: bool = False,
        order: str = "desc",
        limit: int | None = None,
    ) -> list["Incident"]:
        """Query stored incidents with optional filters.

        Args:
            service_name: If provided, only rows for that service.
            since: Only incidents whose ``started_at`` >= this time (UTC).
            until: Only incidents whose ``started_at`` <= this time (UTC).
            types: If provided, only incidents whose ``peak_status`` (or
                ``to_status`` if peak is unset) is in this set.
            open_only: If True, only return incidents with ``ended_at IS NULL``.
            order: ``"asc"`` (oldest first) or ``"desc"`` (newest first,
                the default — the timeline view expects most-recent-first).
            limit: If set, return at most this many rows after filtering.
        """
        from .incidents import Incident  # local import to avoid cycle

        clauses: list[str] = []
        params: list[Any] = []
        if service_name is not None:
            clauses.append("service_name = ?")
            params.append(service_name)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until.isoformat())
        if open_only:
            clauses.append("ended_at IS NULL")
        if types:
            # Filter on peak_status if set, otherwise to_status.
            placeholders = ",".join("?" for _ in types)
            clauses.append(
                f"COALESCE(peak_status, to_status) IN ({placeholders})"
            )
            params.extend(t.value if isinstance(t, Status) else t for t in types)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        direction = "DESC" if order.lower() == "desc" else "ASC"
        query = (
            f"SELECT * FROM incidents {where} "
            f"ORDER BY started_at {direction}"
        )
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = self.conn.execute(query, params).fetchall()
        out: list[Incident] = []
        for r in rows:
            peak_raw = r["peak_status"]
            to_raw = r["to_status"]
            out.append(
                Incident(
                    service_name=r["service_name"],
                    started_at=datetime.fromisoformat(r["started_at"]),
                    ended_at=(
                        datetime.fromisoformat(r["ended_at"])
                        if r["ended_at"]
                        else None
                    ),
                    from_status=Status(r["from_status"]),
                    to_status=Status(to_raw),
                    peak_status=Status(peak_raw) if peak_raw else None,
                    error=r["error"],
                    start_check_id=None,
                    end_check_id=None,
                )
            )
        return out

    def prune_incidents(self, days: int = 90) -> int:
        """Delete incidents older than ``days``. Returns count deleted.

        Operates on ``started_at`` so that long-running incidents whose
        ``ended_at`` is also in the past get cleaned up cleanly.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM incidents WHERE started_at < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount

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
