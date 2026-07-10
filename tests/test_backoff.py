"""Tests for the rate-limit backoff tracker.

PulseBoard already detects HTTP 429 responses and surfaces the
Retry-After hint in ``CheckResult.details`` (see monitor.py). The
``RateLimitBackoff`` class consumes those results to decide whether an
upcoming check for a service should be skipped because the target is
actively rate-limiting us.

These tests are written first and must fail before the implementation
exists (RED).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulseboard.backoff import RateLimitBackoff
from pulseboard.models import CheckResult, Status


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _result(
    service: str = "api",
    *,
    status: Status = Status.DEGRADED,
    rate_limited: bool = True,
    retry_after: int | None = 30,
    timestamp: datetime | None = None,
) -> CheckResult:
    """Build a CheckResult mimicking the shape monitor.check_http produces."""
    details: dict = {}
    if rate_limited and status != Status.UP:
        details["rate_limited"] = True
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after
    error = "HTTP 429 Too Many Requests" if rate_limited and status != Status.UP else None
    return CheckResult(
        service_name=service,
        timestamp=timestamp or datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
        status=status,
        latency_ms=50.0,
        status_code=429 if rate_limited else 200,
        error=error,
        details=details,
    )


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


class TestRateLimitBackoffConstruction:
    def test_construction_with_no_args_does_not_raise(self):
        """RateLimitBackoff() should be constructible without arguments."""
        tracker = RateLimitBackoff()
        assert tracker is not None


# --------------------------------------------------------------------------- #
# should_skip with no observed results
# --------------------------------------------------------------------------- #


class TestShouldSkipNoData:
    def test_should_skip_returns_none_when_no_429_observed(self):
        """No prior 429 means no skip — backoff should not be active."""
        tracker = RateLimitBackoff()
        assert tracker.should_skip("api") is None


# --------------------------------------------------------------------------- #
# observe(429 with retry_after)
# --------------------------------------------------------------------------- #


class TestObserve429WithRetryAfter:
    def test_should_skip_returns_seconds_remaining_after_observe(self):
        """After observing a 429 with Retry-After=30, the remaining window is 30s."""
        tracker = RateLimitBackoff()
        result = _result(retry_after=30)
        tracker.observe(result)
        remaining = tracker.should_skip("api")
        assert remaining is not None
        # freshly observed → close to 30, allow small clock margin
        assert 29 <= remaining <= 30

    def test_should_skip_uses_custom_clock(self):
        """When a custom clock is injected, time advances per the clock."""
        # We freeze the observe time, then advance the clock 10s.
        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return now

        def later() -> datetime:
            return now + timedelta(seconds=10)

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_result(retry_after=30))
        # still at observe-time
        r1 = tracker.should_skip("api")
        assert r1 is not None and 29 <= r1 <= 30

        tracker._clock = later  # advance time
        r2 = tracker.should_skip("api")
        assert r2 is not None
        assert 19 <= r2 <= 20


# --------------------------------------------------------------------------- #
# observe(429 without retry_after) falls back to default
# --------------------------------------------------------------------------- #


class TestObserve429WithoutRetryAfter:
    def test_should_skip_uses_default_backoff(self):
        """A 429 without a numeric Retry-After falls back to the configured default."""
        tracker = RateLimitBackoff(default_backoff_seconds=60.0)
        result = _result(retry_after=None)  # no retry_after_seconds in details
        tracker.observe(result)
        remaining = tracker.should_skip("api")
        assert remaining is not None
        assert 59 <= remaining <= 60

    def test_observe_with_custom_default(self):
        tracker = RateLimitBackoff(default_backoff_seconds=120.0)
        tracker.observe(_result(retry_after=None))
        remaining = tracker.should_skip("api")
        assert remaining is not None
        assert 119 <= remaining <= 120


# --------------------------------------------------------------------------- #
# Non-429 results do not set backoff
# --------------------------------------------------------------------------- #


class TestNon429Results:
    def test_observe_up_result_does_not_set_backoff(self):
        """A UP result (200 OK) must not start a backoff window."""
        tracker = RateLimitBackoff()
        result = _result(status=Status.UP, rate_limited=False, retry_after=None)
        tracker.observe(result)
        assert tracker.should_skip("api") is None

    def test_observe_down_result_without_rate_limit_flag_does_not_set_backoff(self):
        """A 500 (DOWN, not rate-limited) must not start a backoff window."""
        tracker = RateLimitBackoff()
        result = _result(status=Status.DOWN, rate_limited=False, retry_after=None)
        tracker.observe(result)
        assert tracker.should_skip("api") is None

    def test_observe_degraded_non_rate_limited_does_not_set_backoff(self):
        """DEGRADED from content mismatch (not 429) must not back off."""
        tracker = RateLimitBackoff()
        result = CheckResult(
            service_name="api",
            timestamp=datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
            status=Status.DEGRADED,
            latency_ms=100.0,
            status_code=200,
            error="content check failed",
            details={},
        )
        tracker.observe(result)
        assert tracker.should_skip("api") is None


# --------------------------------------------------------------------------- #
# Expiry / backoff ends
# --------------------------------------------------------------------------- #


class TestBackoffExpiry:
    def test_should_skip_returns_none_after_window_expires(self):
        """After enough time passes, the backoff window is over."""
        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return now

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_result(retry_after=30))

        # Advance well past the window.
        now = now + timedelta(seconds=31)
        assert tracker.should_skip("api") is None

    def test_should_skip_returns_zero_at_exact_expiry(self):
        """At the boundary, the remaining window is ~0."""
        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return now

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_result(retry_after=30))

        now = now + timedelta(seconds=30)
        remaining = tracker.should_skip("api")
        assert remaining is not None
        assert remaining <= 0


# --------------------------------------------------------------------------- #
# separate services stay independent
# --------------------------------------------------------------------------- #


class TestMultipleServices:
    def test_backoff_is_per_service(self):
        """A 429 for one service doesn't block another."""
        tracker = RateLimitBackoff()
        tracker.observe(_result(service="api", retry_after=30))
        assert tracker.should_skip("api") is not None
        assert tracker.should_skip("other") is None


# --------------------------------------------------------------------------- #
# clear()
# --------------------------------------------------------------------------- #


class TestClear:
    def test_clear_removes_backoff(self):
        """clear() immediately ends the backoff for a service."""
        tracker = RateLimitBackoff()
        tracker.observe(_result(retry_after=30))
        assert tracker.should_skip("api") is not None
        tracker.clear("api")
        assert tracker.should_skip("api") is None


# --------------------------------------------------------------------------- #
# active_services()
# --------------------------------------------------------------------------- #


class TestActiveServices:
    def test_active_services_empty_when_no_backoff(self):
        tracker = RateLimitBackoff()
        assert tracker.active_services() == {}

    def test_active_services_lists_in_backoff_services(self):
        tracker = RateLimitBackoff()
        tracker.observe(_result(service="api", retry_after=30))
        tracker.observe(_result(service="other", retry_after=60))
        active = tracker.active_services()
        assert set(active.keys()) == {"api", "other"}
        assert 29 <= active["api"] <= 30
        assert 59 <= active["other"] <= 60

    def test_active_services_expires_old_entries(self):
        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return now

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_result(service="api", retry_after=30))
        tracker.observe(_result(service="other", retry_after=60))
        now = now + timedelta(seconds=35)
        active = tracker.active_services()
        assert "api" not in active
        assert "other" in active


# --------------------------------------------------------------------------- #
# reset()
# --------------------------------------------------------------------------- #


class TestReset:
    def test_reset_clears_all_backoff(self):
        tracker = RateLimitBackoff()
        tracker.observe(_result(service="api", retry_after=30))
        tracker.observe(_result(service="other", retry_after=60))
        tracker.reset()
        assert tracker.active_services() == {}


# --------------------------------------------------------------------------- #
# Repeated 429s extend the window
# --------------------------------------------------------------------------- #


class TestRepeated429Extends:
    def test_new_429_during_backoff_extends_window(self):
        """A fresh 429 resets the backoff window from the current time."""
        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock() -> datetime:
            return now

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_result(retry_after=30))
        now = now + timedelta(seconds=20)
        # Second 429, also retry-after=30
        tracker.observe(_result(retry_after=30))
        remaining = tracker.should_skip("api")
        assert remaining is not None
        # Reset from now → ~30s, not 10 (the remainder of the old window).
        assert 29 <= remaining <= 30
