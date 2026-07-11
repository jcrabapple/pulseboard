"""Tests for RateLimitBackoff.filter_active() and synthesize_backoff_result().

These helpers partition services into those that should be checked vs
skipped (due to active rate-limit backoff), and build a synthetic
CheckResult for skipped services so the rest of the watch loop
(storage, alerting, dashboard) can proceed without an actual HTTP
request.

Written first (RED) — the methods do not exist yet.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulseboard.backoff import RateLimitBackoff, synthesize_backoff_result
from pulseboard.models import CheckResult, ServiceConfig, ServiceType, Status


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _svc(name: str = "api") -> ServiceConfig:
    return ServiceConfig(name=name, url=f"https://{name}.example.com")


def _limited_result(
    service: str = "api",
    *,
    retry_after: int | None = 30,
) -> CheckResult:
    details: dict = {"rate_limited": True}
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return CheckResult(
        service_name=service,
        timestamp=datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
        status=Status.DEGRADED,
        latency_ms=50.0,
        status_code=429,
        error="HTTP 429 Too Many Requests",
        details=details,
    )


# --------------------------------------------------------------------------- #
# filter_active — no backoff active
# --------------------------------------------------------------------------- #


class TestFilterActiveNoBackoff:
    def test_returns_all_services_when_no_backoff(self):
        """With no prior 429, every service should be in the to_check list."""
        tracker = RateLimitBackoff()
        services = [_svc("api"), _svc("web"), _svc("db")]
        to_check, to_skip = tracker.filter_active(services)
        assert len(to_check) == 3
        assert to_skip == []

    def test_empty_list_returns_empty(self):
        tracker = RateLimitBackoff()
        to_check, to_skip = tracker.filter_active([])
        assert to_check == []
        assert to_skip == []


# --------------------------------------------------------------------------- #
# filter_active — one service in backoff
# --------------------------------------------------------------------------- #


class TestFilterActiveWithBackoff:
    def test_backed_off_service_excluded_from_to_check(self):
        tracker = RateLimitBackoff()
        tracker.observe(_limited_result("api", retry_after=30))
        services = [_svc("api"), _svc("web")]
        to_check, to_skip = tracker.filter_active(services)
        assert [s.name for s in to_check] == ["web"]
        assert len(to_skip) == 1
        assert to_skip[0][0] == "api"
        assert 29 <= to_skip[0][1] <= 30

    def test_multiple_backed_off_services(self):
        tracker = RateLimitBackoff()
        tracker.observe(_limited_result("api", retry_after=30))
        tracker.observe(_limited_result("web", retry_after=60))
        services = [_svc("api"), _svc("web"), _svc("db")]
        to_check, to_skip = tracker.filter_active(services)
        assert [s.name for s in to_check] == ["db"]
        skip_names = {name for name, _ in to_skip}
        assert skip_names == {"api", "web"}


# --------------------------------------------------------------------------- #
# filter_active — backoff expired mid-flight
# --------------------------------------------------------------------------- #


class TestFilterActiveExpiry:
    def test_expired_backoff_is_not_skipped(self):
        """After the backoff window expires, the service is back in to_check."""
        from datetime import timedelta

        now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

        def clock():
            return now

        tracker = RateLimitBackoff(clock=clock)
        tracker.observe(_limited_result("api", retry_after=30))
        # advance past the window
        now_value = now + timedelta(seconds=31)
        tracker._clock = lambda: now_value

        services = [_svc("api")]
        to_check, to_skip = tracker.filter_active(services)
        assert len(to_check) == 1
        assert to_skip == []


# --------------------------------------------------------------------------- #
# synthesize_backoff_result
# --------------------------------------------------------------------------- #


class TestSynthesizeBackoffResult:
    def test_creates_degraded_result_with_backoff_info(self):
        result = synthesize_backoff_result("api", 25.0)
        assert result.service_name == "api"
        assert result.status == Status.DEGRADED
        assert result.details.get("rate_limited") is True
        assert result.details.get("backoff_seconds_remaining") == 25.0
        assert "25" in (result.error or "")

    def test_latency_is_zero(self):
        result = synthesize_backoff_result("api", 10.0)
        assert result.latency_ms == 0.0

    def test_status_code_is_none(self):
        """No HTTP request was made — there is no status code."""
        result = synthesize_backoff_result("api", 10.0)
        assert result.status_code is None

    def test_timestamp_is_set(self):
        result = synthesize_backoff_result("api", 10.0)
        assert result.timestamp is not None
