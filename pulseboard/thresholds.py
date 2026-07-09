"""Latency and error-rate threshold evaluation.

Per-service thresholds let a user downgrade the status of a service based on
how *well* it's responding, not just whether it returned at all.

There are two independent checks, each with warning and critical levels:

* **Latency thresholds** — applied to ``CheckResult.latency_ms`` from the
  current check. ``latency_warning_ms`` → DEGRADED, ``latency_critical_ms``
  → DOWN.
* **Error-rate thresholds** — applied to a rolling window of recent checks
  for the same service (sourced from :class:`~pulseboard.storage.Storage`).
  ``error_rate_warning_pct`` → DEGRADED, ``error_rate_critical_pct`` → DOWN.

A DOWN result from the underlying check is never upgraded by thresholds
(DOWN stays DOWN). The function only downgrades UP → DEGRADED or UP → DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import CheckResult, ServiceConfig, Status


# Worst-severity priority used when combining latency + error-rate findings.
# Higher value = worse.
_SEVERITY_RANK = {Status.UP: 0, Status.DEGRADED: 1, Status.DOWN: 2}


@dataclass
class ThresholdOutcome:
    """The result of applying thresholds to a check.

    Attributes:
        status: The (possibly downgraded) status after threshold evaluation.
        reasons: Human-readable list of which thresholds fired, in stable
            order. Empty when no threshold affected the result.
        latency_violation: One of ``None`` (within bounds), ``"warning"``,
            or ``"critical"``.
        error_rate_violation: Same shape, but for the error-rate check.
        error_rate_pct: The measured error rate in the window, 0-100.
            ``None`` if no error-rate check was performed.
        error_rate_sample_size: How many historical checks were used in
            the window. ``0`` if no error-rate check was performed.
    """

    status: Status
    reasons: list[str]
    latency_violation: str | None
    error_rate_violation: str | None
    error_rate_pct: float | None
    error_rate_sample_size: int

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "latency_violation": self.latency_violation,
            "error_rate_violation": self.error_rate_violation,
            "error_rate_pct": self.error_rate_pct,
            "error_rate_sample_size": self.error_rate_sample_size,
        }


def compute_error_rate(results: Sequence[CheckResult]) -> tuple[float, int]:
    """Return ``(failure_pct, sample_size)`` from a sequence of checks.

    A "failure" is anything that isn't :attr:`Status.UP`. The percentage is
    rounded to 2 decimal places. An empty sequence returns ``(0.0, 0)`` —
    callers should treat zero samples as "not enough data".
    """
    if not results:
        return 0.0, 0
    failures = sum(1 for r in results if r.status != Status.UP)
    return round((failures / len(results)) * 100, 2), len(results)


def _worst(a: Status, b: Status) -> Status:
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def evaluate_thresholds(
    result: CheckResult,
    service: ServiceConfig,
    history: Sequence[CheckResult] | None = None,
) -> ThresholdOutcome:
    """Apply any configured thresholds to ``result`` and return the outcome.

    Args:
        result: The check result just produced. Its status may be
            downgraded — the returned ``ThresholdOutcome.status`` reflects
            the worst of: original status, latency violation, error-rate
            violation.
        service: The service config — supplies threshold values and the
            ``error_rate_window``.
        history: Recent check results for the same service, ordered
            newest-first or oldest-first (we just count). If ``None`` or
            empty, the error-rate check is skipped (not "fails" — there's
            simply not enough data to judge).
    """
    status = result.status
    reasons: list[str] = []
    latency_violation: str | None = None
    error_rate_violation: str | None = None
    error_rate_pct: float | None = None
    error_rate_sample_size = 0

    # Don't bother applying thresholds to a service that didn't ask for them.
    if not service.has_any_threshold():
        return ThresholdOutcome(
            status=status,
            reasons=reasons,
            latency_violation=None,
            error_rate_violation=None,
            error_rate_pct=None,
            error_rate_sample_size=0,
        )

    # DOWN is terminal — never upgraded by a threshold.
    if status == Status.DOWN:
        return ThresholdOutcome(
            status=status,
            reasons=reasons,
            latency_violation=None,
            error_rate_violation=None,
            error_rate_pct=None,
            error_rate_sample_size=0,
        )

    # Latency check
    if service.has_latency_thresholds():
        latency_ms = result.latency_ms
        if (
            service.latency_critical_ms is not None
            and latency_ms >= service.latency_critical_ms
        ):
            latency_violation = "critical"
            reasons.append(
                f"latency {latency_ms:.0f}ms ≥ critical "
                f"{service.latency_critical_ms:.0f}ms"
            )
            status = _worst(status, Status.DOWN)
        elif (
            service.latency_warning_ms is not None
            and latency_ms >= service.latency_warning_ms
        ):
            latency_violation = "warning"
            reasons.append(
                f"latency {latency_ms:.0f}ms ≥ warning "
                f"{service.latency_warning_ms:.0f}ms"
            )
            status = _worst(status, Status.DEGRADED)

    # Error-rate check
    if service.has_error_rate_thresholds() and history:
        window = list(history)[: max(1, service.error_rate_window)]
        pct, n = compute_error_rate(window)
        error_rate_pct = pct
        error_rate_sample_size = n
        if (
            service.error_rate_critical_pct is not None
            and n > 0
            and pct >= service.error_rate_critical_pct
        ):
            error_rate_violation = "critical"
            reasons.append(
                f"error rate {pct:.1f}% ≥ critical "
                f"{service.error_rate_critical_pct:.1f}% (window={n})"
            )
            status = _worst(status, Status.DOWN)
        elif (
            service.error_rate_warning_pct is not None
            and n > 0
            and pct >= service.error_rate_warning_pct
        ):
            error_rate_violation = "warning"
            reasons.append(
                f"error rate {pct:.1f}% ≥ warning "
                f"{service.error_rate_warning_pct:.1f}% (window={n})"
            )
            status = _worst(status, Status.DEGRADED)

    return ThresholdOutcome(
        status=status,
        reasons=reasons,
        latency_violation=latency_violation,
        error_rate_violation=error_rate_violation,
        error_rate_pct=error_rate_pct,
        error_rate_sample_size=error_rate_sample_size,
    )
