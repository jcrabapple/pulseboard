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
from typing import Callable

from .models import CheckResult


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
