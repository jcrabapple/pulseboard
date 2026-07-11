"""Rate-limit backoff tracker.

When an HTTP check receives a 429 (Too Many Requests) response,
PulseBoard's :func:`pulseboard.monitor.check_http` surfaces the
Retry-After hint as ``result.details["retry_after_seconds"]`` and
marks the result with ``result.details["rate_limited"] = True``.

:class:`RateLimitBackoff` consumes those results and answers the
question, *should we skip the next check for this service?* The answer
is ``None`` when no backoff is active, or the (positive) number of
seconds remaining until the target's rate-limit window expires.

Design
------

- **Pure state container.** No I/O and no clock side effects — all time
  comes from an injectable ``clock`` callable, which makes the window
  logic trivially testable by advancing a fake clock.
- **Per-service.** Each service name maps to its own backoff deadline.
- **Resilient to malformed Retry-After.** When a 429 arrives without a
  parseable numeric hint, the configured ``default_backoff_seconds`` is
  used instead of assuming a value that could hammer the target.
- **Idempotent.** Observing the same 429 result twice simply moves the
  deadline forward to the newer one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from .models import CheckResult, Status

if TYPE_CHECKING:
    from .models import ServiceConfig


class RateLimitBackoff:
    """Track HTTP-429 rate-limit windows per service.

    Parameters
    ----------
    default_backoff_seconds:
        Fallback used when a 429 is observed without a numeric
        Retry-After hint. Defaults to 30s — a conservative choice that
        avoids hammering the target, per the backlog item
        "Rate-limit handling: back off when a target returns 429".
    clock:
        Callable returning a timezone-aware ``datetime``. Defaults to
        ``datetime.now(timezone.utc)``.
    """

    def __init__(
        self,
        *,
        default_backoff_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.default_backoff_seconds = default_backoff_seconds
        self._clock: Callable[[], datetime] = clock
        # service_name → datetime when backoff expires
        self._backoff_until: dict[str, datetime] = {}

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc)

    def observe(self, result: CheckResult) -> None:
        """Update backoff state from a check result.

        If ``result`` is a rate-limited check (429 with
        ``details["rate_limited"] == True``), the backoff window for
        that service is extended based on the Retry-After hint (or the
        configured default). Non-rate-limited results are ignored — they
        do not cancel a backoff, but they also do not start one.
        """
        details = result.details or {}
        if not details.get("rate_limited"):
            return

        retry_after = details.get("retry_after_seconds")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            backoff = float(retry_after)
        else:
            backoff = self.default_backoff_seconds

        now = self._now()
        expires_at = now + timedelta(seconds=backoff)
        self._backoff_until[result.service_name] = expires_at

    def should_skip(self, service_name: str) -> float | None:
        """Return seconds remaining in backoff, or ``None`` if not active.

        When the return value is positive, the caller should skip the
        upcoming check for ``service_name``. When ``None``, no backoff
        is active or the window has already expired.
        """
        self._prune()
        deadline = self._backoff_until.get(service_name)
        if deadline is None:
            return None
        remaining = (deadline - self._now()).total_seconds()
        if remaining < 0:
            # Stale entry — drop it so it doesn't linger.
            self._backoff_until.pop(service_name, None)
            return None
        return remaining

    def clear(self, service_name: str) -> None:
        """Immediately end backoff for ``service_name`` if present."""
        self._backoff_until.pop(service_name, None)

    def reset(self) -> None:
        """Clear all per-service backoff state."""
        self._backoff_until.clear()

    def active_services(self) -> dict[str, float]:
        """Return a snapshot of ``service_name → seconds_remaining``.

        Expired entries are pruned and not included.
        """
        self._prune()
        now = self._now()
        out: dict[str, float] = {}
        for name, deadline in self._backoff_until.items():
            remaining = (deadline - now).total_seconds()
            if remaining > 0:
                out[name] = remaining
        return out

    # ------------------------------------------------------------------ #
    # Watch-loop integration
    # ------------------------------------------------------------------ #

    def filter_active(
        self, services: list["ServiceConfig"]
    ) -> tuple[list["ServiceConfig"], list[tuple[str, float]]]:
        """Partition services into those to check vs those to skip.

        Returns ``(to_check, to_skip)``:
        - ``to_check``: services whose backoff has expired or never started.
        - ``to_skip``: ``(service_name, backoff_seconds_remaining)`` pairs for
          services that are still in an active backoff window.

        Callers should:

        1. Run checks only on ``to_check``.
        2. Feed the results into :meth:`observe`.
        3. For each entry in ``to_skip``, build a synthetic result via
           :func:`synthesize_backoff_result` instead of a real HTTP request.
        """
        to_check: list[ServiceConfig] = []
        to_skip: list[tuple[str, float]] = []
        for svc in services:
            remaining = self.should_skip(svc.name)
            if remaining is not None:
                to_skip.append((svc.name, remaining))
            else:
                to_check.append(svc)
        return to_check, to_skip

    def _prune(self) -> None:
        """Remove expired backoff entries (remaining < 0)."""
        now = self._now()
        expired = [
            name
            for name, deadline in self._backoff_until.items()
            if (deadline - now).total_seconds() < 0
        ]
        for name in expired:
            self._backoff_until.pop(name, None)


def synthesize_backoff_result(
    service_name: str, backoff_remaining: float
) -> CheckResult:
    """Build a DEGRADED CheckResult for a rate-limited service that was skipped.

    This avoids a real HTTP request while ensuring storage, alerting, and
    dashboard display still see a result. The ``details`` dict carries the
    backoff state so consumers can inspect it:

    - ``rate_limited`` → ``True`` (the check was skipped because of a prior 429)
    - ``backoff_seconds_remaining`` → time left in the cooldown window

    Latency is reported as ``0.0`` (no network round-trip); ``status_code``
    is ``None`` (no HTTP response was received).
    """
    return CheckResult(
        service_name=service_name,
        timestamp=datetime.now(timezone.utc),
        status=Status.DEGRADED,
        latency_ms=0.0,
        error=f"HTTP 429 rate-limit backoff: {backoff_remaining:.0f}s remaining",
        details={
            "rate_limited": True,
            "backoff_seconds_remaining": backoff_remaining,
        },
    )
