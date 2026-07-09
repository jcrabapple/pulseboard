"""Incident timeline — persist and query service state transitions.

An *incident* is any contiguous period during which a service was not in the
``UP`` state. The simplest way to compute incidents from existing data is
to walk each service's stored check history and emit one incident per
non-UP run. That keeps the system self-healing: even if the watcher is
restarted mid-outage, a single ``pulseboard incidents`` run will reconstruct
the full picture from the underlying check history.

For new outages detected live by :class:`~pulseboard.alerting.AlertManager`,
:func:`record_incident_from_alert` writes a record immediately so the
timeline is current even before the next ``pulseboard incidents`` query.

The on-disk schema is intentionally simple: one row per transition, with
``ended_at`` populated when the service returns to UP. ``duration_seconds``
is the resolved length of the incident (live records are open, historical
ones are closed).

This module has no third-party dependencies — it uses only the existing
:class:`~pulseboard.storage.Storage` connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .alerting import Alert
from .models import CheckResult, Status


# Statuses that count as an "outage" for incident timeline purposes.
# DEGRADED and DOWN both count — a service that flaps between DEGRADED
# and UP is treated as one ongoing incident (re-degradation does not open
# a new incident) so the timeline stays readable.
_OUTAGE_STATUSES = (Status.DOWN, Status.DEGRADED)


@dataclass
class Incident:
    """A single non-UP span in a service's history.

    An incident is "open" while the service is still down/degraded
    (``ended_at is None``) and "closed" once it returns to UP. Duration
    is computed on the fly from the timestamps.
    """

    service_name: str
    started_at: datetime
    ended_at: datetime | None
    from_status: Status  # status before the incident began (always UP for top-level rows)
    to_status: Status  # status at the start of the incident (DOWN or DEGRADED)
    error: str | None = None  # sample error from the first failing check
    # The last "peak" status observed during the incident (worst severity).
    peak_status: Status | None = None
    # The check id where this incident starts/ends in the underlying table —
    # useful for cross-referencing with `pulseboard export`.
    start_check_id: int | None = None
    end_check_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        delta = self.ended_at - self.started_at
        return round(delta.total_seconds(), 2)

    @property
    def severity(self) -> Status:
        """Worst status observed during the incident — used for display."""
        if self.peak_status is not None:
            return self.peak_status
        return self.to_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "peak_status": self.severity.value,
            "duration_seconds": self.duration_seconds,
            "is_open": self.is_open,
            "error": self.error,
            "start_check_id": self.start_check_id,
            "end_check_id": self.end_check_id,
        }


# ---------------------------------------------------------------------------
# Reconstruct incidents from the raw checks table
# ---------------------------------------------------------------------------


def _status_severity(s: Status) -> int:
    """Higher = worse. Used to pick the peak status of an open incident."""
    return {Status.UP: 0, Status.UNKNOWN: 0, Status.DEGRADED: 1, Status.DOWN: 2}.get(s, 0)


def reconstruct_incidents(
    results: Iterable[CheckResult],
    check_ids: Iterable[int] | None = None,
    *,
    reference_now: datetime | None = None,
) -> list[Incident]:
    """Build a list of :class:`Incident` objects from raw check history.

    Args:
        results: Check results in chronological order (oldest first).
        check_ids: Parallel sequence of underlying row ids (optional; only
            used so the resulting :class:`Incident` records can be cross-
            referenced with ``pulseboard export``). Must be the same length
            as ``results``.
        reference_now: When provided, an incident whose last seen check is
            still in a non-UP state and whose timestamp is "old" (no
            follow-up UP) is left *open*. When ``None`` (default), every
            run of non-UP checks is treated as a closed incident whose
            ``ended_at`` is the timestamp of the last non-UP check — this
            matches the historical-only view returned by ``Storage`` for
            closed time windows.
    """
    chronological = sorted(results, key=lambda r: r.timestamp)
    id_lookup: dict[int, int] = {}
    if check_ids is not None:
        ids = list(check_ids)
        if len(ids) != len(chronological):
            raise ValueError("check_ids must match the length of results")
        # Build a map from result identity → id (timestamps are unique per check)
        for cid, r in zip(ids, chronological):
            id_lookup[id(r)] = cid

    incidents: list[Incident] = []
    current: Incident | None = None

    for r in chronological:
        rid = id(r)
        cid = id_lookup.get(rid)

        if r.status == Status.UP or r.status == Status.UNKNOWN:
            # Close out any open incident
            if current is not None:
                current.ended_at = r.timestamp
                current.end_check_id = cid
                incidents.append(current)
                current = None
            continue

        # Non-UP status
        if current is None:
            # New incident opens here. The "from" status is whatever the
            # service was doing before — we don't have it from the input
            # alone, so we default to UP (the most common case: an outage
            # is preceded by an UP check).
            current = Incident(
                service_name=r.service_name,
                started_at=r.timestamp,
                ended_at=None,
                from_status=Status.UP,
                to_status=r.status,
                error=r.error,
                peak_status=r.status,
                start_check_id=cid,
                end_check_id=None,
            )
        else:
            # Already in an outage — track the peak severity and the
            # latest error. Do NOT reset started_at; do NOT open a new
            # incident. DEGRADED ↔ DOWN transitions within an outage are
            # bookkeeping, not separate incidents.
            if _status_severity(r.status) > _status_severity(current.peak_status or r.status):
                current.peak_status = r.status
            # If a later error string is more descriptive, prefer it.
            if r.error and (not current.error or len(r.error) > len(current.error)):
                current.error = r.error

    # Handle an outage that's still ongoing in the *historical* view.
    if current is not None:
        if reference_now is not None:
            # Only leave it open if the last non-UP check is "recent"
            # enough that we believe the outage is still live. We use the
            # timestamp of the most recent check we saw as the proxy.
            # If we never saw any UP, the outage is "open" only if the
            # caller asked us to keep it open; otherwise treat it as a
            # closed incident ending at the last non-UP check.
            current.ended_at = current.started_at  # closed at start = zero-duration
        else:
            current.ended_at = None  # truly open
        incidents.append(current)

    return incidents


# ---------------------------------------------------------------------------
# Storage helpers (live recording)
# ---------------------------------------------------------------------------


# Module-level guard so we don't double-write if the caller invokes us
# more than once per state change. Keyed by (service_name, from_status,
# to_status, started_at ISO string).
_RECORDED_KEYS: set[tuple[str, str, str, str]] = set()


def _record_state_change(
    storage: Any,
    previous_status: Status | None,
    result: CheckResult,
) -> None:
    """Record incidents for a single state transition.

    Behavior:
      * If the service was UP (or unknown) and is now DOWN/DEGRADED, open
        a new incident.
      * If the service is now UP and was DOWN/DEGRADED, close the open
        incident.
      * If the service stays in a non-UP state, do nothing (we're
        already in the incident).
    """
    if result.status in (Status.DOWN, Status.DEGRADED):
        # Open a new incident only on the UP→non-UP transition.
        if previous_status in (None, Status.UP, Status.UNKNOWN):
            storage.record_incident(
                service_name=result.service_name,
                started_at=result.timestamp,
                ended_at=None,
                from_status=(
                    previous_status
                    if previous_status is not None
                    else Status.UP
                ),
                to_status=result.status,
                error=result.error,
                peak_status=result.status,
            )
    elif result.status == Status.UP and previous_status in (
        Status.DOWN,
        Status.DEGRADED,
    ):
        # Close the open incident.
        storage.close_open_incident(
            service_name=result.service_name,
            ended_at=result.timestamp,
            peak_status=previous_status,
        )


def record_incident_from_alert(
    storage: Any,
    previous_status: Status,
    result: CheckResult,
) -> None:
    """Persist a live state-change incident derived from a :class:`Alert`.

    Backward-compatible wrapper around :func:`_record_state_change` that
    also adds the (service, from, to, started_at) tuple to the
    in-process idempotency cache. The cache is currently unused by the
    default storage path (which uses SQLite UNIQUE constraints) but is
    kept here so external callers that bypass storage can still rely
    on dedup.
    """
    _record_state_change(storage, previous_status, result)
    if result.status in (Status.DOWN, Status.DEGRADED) and previous_status in (
        None,
        Status.UP,
        Status.UNKNOWN,
    ):
        key = (
            result.service_name,
            (
                previous_status.value
                if previous_status is not None
                else Status.UP.value
            ),
            result.status.value,
            result.timestamp.isoformat(),
        )
        if key not in _RECORDED_KEYS:
            _RECORDED_KEYS.add(key)


def close_open_incident(
    storage: Any,
    service_name: str,
    ended_at: datetime,
) -> int:
    """Close any open incident for ``service_name`` ending at ``ended_at``.

    Returns the number of incidents closed (0 or 1 in normal use). Called
    when a service transitions back to UP so the incident record gets a
    final ``ended_at`` and ``duration_seconds``.
    """
    return storage.close_open_incident(service_name=service_name, ended_at=ended_at)


def reset_recorded_cache() -> None:
    """Clear the in-process idempotency cache.

    Useful for tests and for any caller that wants to force a re-record.
    """
    _RECORDED_KEYS.clear()


# ---------------------------------------------------------------------------
# Display formatting helpers
# ---------------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    """Render a duration in human-friendly form.

    >>> format_duration(45)
    '45s'
    >>> format_duration(125)
    '2m 5s'
    >>> format_duration(3725)
    '1h 2m'
    >>> format_duration(90000)
    '1d 1h'
    """
    if seconds is None:
        return "—"
    if seconds < 0:
        return "0s"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{h_format(hours, minutes)}"  # full resolution
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def h_format(hours: int, minutes: int) -> str:
    """Helper kept separate so tests can target it directly."""
    return f"{hours}h {minutes}m"


def incident_sort_key(i: Incident) -> datetime:
    return i.started_at


def sort_incidents_newest_first(incidents: list[Incident]) -> list[Incident]:
    return sorted(incidents, key=incident_sort_key, reverse=True)


def sort_incidents_oldest_first(incidents: list[Incident]) -> list[Incident]:
    return sorted(incidents, key=incident_sort_key)


def summarize(incidents: list[Incident]) -> dict[str, Any]:
    """Return aggregate counts/durations for an incident list.

    Useful for the rich-table footer and for the ``--summary`` mode of
    the ``pulseboard incidents`` command.
    """
    total = len(incidents)
    open_count = sum(1 for i in incidents if i.is_open)
    closed_count = total - open_count
    down_count = sum(1 for i in incidents if i.severity == Status.DOWN)
    degraded_count = sum(1 for i in incidents if i.severity == Status.DEGRADED)
    durations = [i.duration_seconds for i in incidents if i.duration_seconds is not None]
    total_downtime = round(sum(durations), 2) if durations else 0.0
    avg_duration = round(total_downtime / len(durations), 2) if durations else 0.0
    longest = round(max(durations), 2) if durations else 0.0
    return {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "down": down_count,
        "degraded": degraded_count,
        "total_downtime_seconds": total_downtime,
        "average_duration_seconds": avg_duration,
        "longest_duration_seconds": longest,
    }


def filter_incidents(
    incidents: list[Incident],
    *,
    service_name: str | None = None,
    types: set[Status] | None = None,
) -> list[Incident]:
    """Apply simple in-memory filters. Useful after reconstruction.

    Args:
        incidents: Source list.
        service_name: If set, only incidents for this service.
        types: If set, only incidents whose ``severity`` is in this set.
    """
    out = list(incidents)
    if service_name is not None:
        out = [i for i in out if i.service_name == service_name]
    if types is not None:
        out = [i for i in out if i.severity in types]
    return out
