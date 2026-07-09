"""Tests for latency / error-rate threshold evaluation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulseboard.models import CheckResult, ServiceConfig, ServiceType, Status
from pulseboard.thresholds import (
    ThresholdOutcome,
    compute_error_rate,
    evaluate_thresholds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(
    status: Status = Status.UP,
    latency_ms: float = 50.0,
    error: str | None = None,
    when: datetime | None = None,
) -> CheckResult:
    return CheckResult(
        service_name="svc",
        timestamp=when or datetime.now(timezone.utc),
        status=status,
        latency_ms=latency_ms,
        error=error,
    )


def make_service(
    *,
    lat_warn: float | None = None,
    lat_crit: float | None = None,
    er_warn: float | None = None,
    er_crit: float | None = None,
    window: int = 50,
) -> ServiceConfig:
    return ServiceConfig(
        name="svc",
        url="https://example.com",
        service_type=ServiceType.HTTP,
        latency_warning_ms=lat_warn,
        latency_critical_ms=lat_crit,
        error_rate_warning_pct=er_warn,
        error_rate_critical_pct=er_crit,
        error_rate_window=window,
    )


# ---------------------------------------------------------------------------
# compute_error_rate
# ---------------------------------------------------------------------------


class TestComputeErrorRate:
    def test_empty_returns_zero(self):
        assert compute_error_rate([]) == (0.0, 0)

    def test_all_up_is_zero_percent(self):
        history = [make_result(Status.UP) for _ in range(5)]
        assert compute_error_rate(history) == (0.0, 5)

    def test_all_down_is_one_hundred_percent(self):
        history = [make_result(Status.DOWN) for _ in range(4)]
        assert compute_error_rate(history) == (100.0, 4)

    def test_mixed_counts_non_up_as_failure(self):
        history = [
            make_result(Status.UP),
            make_result(Status.DOWN),
            make_result(Status.DEGRADED),
            make_result(Status.UP),
        ]
        # 2 failures out of 4 = 50%
        assert compute_error_rate(history) == (50.0, 4)

    def test_rounds_to_two_decimals(self):
        history = [make_result(Status.UP)] * 2 + [make_result(Status.DOWN)]
        # 1/3 = 33.333... -> 33.33
        pct, n = compute_error_rate(history)
        assert n == 3
        assert pct == 33.33


# ---------------------------------------------------------------------------
# evaluate_thresholds — latency
# ---------------------------------------------------------------------------


class TestLatencyThresholds:
    def test_no_thresholds_means_no_change(self):
        svc = make_service()
        result = make_result(Status.UP, latency_ms=99999.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.UP
        assert outcome.reasons == []
        assert outcome.latency_violation is None

    def test_below_warning_is_up(self):
        svc = make_service(lat_warn=100.0)
        result = make_result(Status.UP, latency_ms=50.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.UP
        assert outcome.latency_violation is None

    def test_at_warning_is_degraded(self):
        svc = make_service(lat_warn=100.0)
        result = make_result(Status.UP, latency_ms=100.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DEGRADED
        assert outcome.latency_violation == "warning"
        assert any("latency" in r for r in outcome.reasons)

    def test_between_warning_and_critical_is_degraded(self):
        svc = make_service(lat_warn=100.0, lat_crit=500.0)
        result = make_result(Status.UP, latency_ms=250.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DEGRADED
        assert outcome.latency_violation == "warning"

    def test_at_critical_is_down(self):
        svc = make_service(lat_warn=100.0, lat_crit=500.0)
        result = make_result(Status.UP, latency_ms=500.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DOWN
        assert outcome.latency_violation == "critical"

    def test_above_critical_is_down(self):
        svc = make_service(lat_warn=100.0, lat_crit=500.0)
        result = make_result(Status.UP, latency_ms=9999.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DOWN
        assert outcome.latency_violation == "critical"

    def test_critical_only_skips_warning(self):
        # When only critical is set, the check uses ">= critical" directly.
        svc = make_service(lat_crit=1000.0)
        result_low = make_result(Status.UP, latency_ms=100.0)
        assert evaluate_thresholds(result_low, svc).latency_violation is None
        result_high = make_result(Status.UP, latency_ms=1500.0)
        out = evaluate_thresholds(result_high, svc)
        assert out.latency_violation == "critical"
        assert out.status == Status.DOWN


# ---------------------------------------------------------------------------
# evaluate_thresholds — error rate
# ---------------------------------------------------------------------------


class TestErrorRateThresholds:
    def test_no_history_means_skip(self):
        svc = make_service(er_warn=10.0)
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=[])
        assert outcome.status == Status.UP
        assert outcome.error_rate_violation is None
        assert outcome.error_rate_sample_size == 0

    def test_below_warning_is_up(self):
        svc = make_service(er_warn=20.0, er_crit=80.0)
        history = (
            [make_result(Status.UP) for _ in range(9)]
            + [make_result(Status.DOWN)]
        )
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=history)
        assert outcome.status == Status.UP
        assert outcome.error_rate_violation is None
        assert outcome.error_rate_pct == 10.0
        assert outcome.error_rate_sample_size == 10

    def test_at_warning_is_degraded(self):
        svc = make_service(er_warn=20.0, er_crit=80.0, window=10)
        history = (
            [make_result(Status.UP) for _ in range(8)]
            + [make_result(Status.DOWN) for _ in range(2)]
        )
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=history)
        assert outcome.status == Status.DEGRADED
        assert outcome.error_rate_violation == "warning"
        assert outcome.error_rate_pct == 20.0

    def test_at_critical_is_down(self):
        svc = make_service(er_warn=20.0, er_crit=80.0, window=10)
        history = (
            [make_result(Status.UP) for _ in range(2)]
            + [make_result(Status.DOWN) for _ in range(8)]
        )
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=history)
        assert outcome.status == Status.DOWN
        assert outcome.error_rate_violation == "critical"

    def test_window_truncates_history(self):
        svc = make_service(er_warn=10.0, window=5)
        # First 5 are all failures; the rest are up. Window should be the
        # most recent ones — but order doesn't matter for the percentage.
        history = (
            [make_result(Status.DOWN) for _ in range(5)]
            + [make_result(Status.UP) for _ in range(5)]
        )
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=history)
        # If we slice from the front, we get the 5 DOWNS -> 100% -> > warning.
        assert outcome.error_rate_sample_size == 5
        assert outcome.error_rate_pct == 100.0
        assert outcome.status == Status.DEGRADED

    def test_history_none_is_treated_as_empty(self):
        svc = make_service(er_warn=10.0)
        result = make_result(Status.UP)
        outcome = evaluate_thresholds(result, svc, history=None)
        assert outcome.error_rate_violation is None
        assert outcome.error_rate_sample_size == 0


# ---------------------------------------------------------------------------
# evaluate_thresholds — combined behavior
# ---------------------------------------------------------------------------


class TestCombined:
    def test_down_status_never_upgraded(self):
        svc = make_service(lat_warn=10.0, lat_crit=100.0)
        result = make_result(Status.DOWN, latency_ms=1.0, error="boom")
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DOWN
        assert outcome.latency_violation is None
        assert outcome.reasons == []

    def test_critical_latency_plus_high_error_rate(self):
        svc = make_service(
            lat_warn=100.0, lat_crit=500.0, er_warn=20.0, er_crit=80.0,
        )
        history = [make_result(Status.DOWN) for _ in range(9)] + [
            make_result(Status.UP)
        ]
        result = make_result(Status.UP, latency_ms=900.0)
        outcome = evaluate_thresholds(result, svc, history=history)
        assert outcome.status == Status.DOWN
        assert outcome.latency_violation == "critical"
        assert outcome.error_rate_violation == "critical"
        assert len(outcome.reasons) == 2

    def test_degraded_plus_warning_stays_degraded(self):
        # Status is already DEGRADED (from the underlying check); latency
        # warning should not escalate.
        svc = make_service(lat_warn=100.0)
        result = make_result(Status.DEGRADED, latency_ms=200.0)
        outcome = evaluate_thresholds(result, svc)
        assert outcome.status == Status.DEGRADED
        assert outcome.latency_violation == "warning"

    def test_to_dict_round_trip(self):
        svc = make_service(lat_warn=100.0)
        result = make_result(Status.UP, latency_ms=200.0)
        outcome = evaluate_thresholds(result, svc)
        d = outcome.to_dict()
        assert d["status"] == "degraded"
        assert d["latency_violation"] == "warning"
        assert isinstance(d["reasons"], list)
        assert d["error_rate_pct"] is None


# ---------------------------------------------------------------------------
# ServiceConfig convenience methods
# ---------------------------------------------------------------------------


class TestServiceConfigHelpers:
    def test_has_latency_thresholds(self):
        assert make_service(lat_warn=100.0).has_latency_thresholds()
        assert make_service(lat_crit=100.0).has_latency_thresholds()
        assert not make_service().has_latency_thresholds()

    def test_has_error_rate_thresholds(self):
        assert make_service(er_warn=10.0).has_error_rate_thresholds()
        assert make_service(er_crit=10.0).has_error_rate_thresholds()
        assert not make_service().has_error_rate_thresholds()

    def test_has_any_threshold(self):
        assert make_service(lat_warn=1.0).has_any_threshold()
        assert make_service(er_warn=1.0).has_any_threshold()
        assert not make_service().has_any_threshold()


# ---------------------------------------------------------------------------
# Config parsing of threshold fields
# ---------------------------------------------------------------------------


class TestConfigThresholds:
    def test_parses_latency_thresholds(self):
        from pulseboard.config import parse_services

        raw = {
            "services": [
                {
                    "name": "slow",
                    "url": "https://example.com",
                    "latency_warning_ms": 250,
                    "latency_critical_ms": 1000,
                }
            ]
        }
        services = parse_services(raw)
        assert services[0].latency_warning_ms == 250.0
        assert services[0].latency_critical_ms == 1000.0

    def test_parses_error_rate_thresholds(self):
        from pulseboard.config import parse_services

        raw = {
            "services": [
                {
                    "name": "flaky",
                    "url": "https://example.com",
                    "error_rate_window": 25,
                    "error_rate_warning_pct": 5,
                    "error_rate_critical_pct": 25,
                }
            ]
        }
        services = parse_services(raw)
        assert services[0].error_rate_window == 25
        assert services[0].error_rate_warning_pct == 5.0
        assert services[0].error_rate_critical_pct == 25.0

    def test_warning_must_be_le_critical_latency(self):
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {
                    "name": "bad",
                    "url": "https://example.com",
                    "latency_warning_ms": 1000,
                    "latency_critical_ms": 100,
                }
            ]
        }
        with pytest.raises(ConfigError, match="latency_warning_ms"):
            parse_services(raw)

    def test_warning_must_be_le_critical_error_rate(self):
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {
                    "name": "bad",
                    "url": "https://example.com",
                    "error_rate_warning_pct": 50,
                    "error_rate_critical_pct": 10,
                }
            ]
        }
        with pytest.raises(ConfigError, match="error_rate_warning_pct"):
            parse_services(raw)

    def test_error_rate_out_of_range_rejected(self):
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {
                    "name": "bad",
                    "url": "https://example.com",
                    "error_rate_warning_pct": 150,
                }
            ]
        }
        with pytest.raises(ConfigError, match="between 0 and 100"):
            parse_services(raw)

    def test_negative_latency_rejected(self):
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {
                    "name": "bad",
                    "url": "https://example.com",
                    "latency_warning_ms": -10,
                }
            ]
        }
        with pytest.raises(ConfigError, match="must be >= 0"):
            parse_services(raw)

    def test_window_must_be_positive(self):
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {
                    "name": "bad",
                    "url": "https://example.com",
                    "error_rate_window": 0,
                }
            ]
        }
        with pytest.raises(ConfigError, match="error_rate_window"):
            parse_services(raw)


# ---------------------------------------------------------------------------
# Monitor integration: run_check_with_thresholds
# ---------------------------------------------------------------------------


class TestRunCheckWithThresholds:
    def test_no_thresholds_returns_result_unchanged(self):
        """When the service has no thresholds configured, the wrapper is a no-op."""
        import asyncio

        from pulseboard.monitor import run_check_with_thresholds

        svc = make_service()
        result = make_result(Status.UP, latency_ms=42.0)
        # run_check is async; monkeypatch it by passing through evaluate only
        # is the only path. We assert the no-threshold path by calling
        # evaluate_thresholds directly — but here we also verify the
        # no-history-provider path works with a real service.
        async def fake_run_check(s):
            return result

        # Replace at runtime by calling evaluate_thresholds on a result that
        # already has the no-threshold path implied: the wrapper short-
        # circuits because has_any_threshold() is False. We need run_check,
        # though — use a service that won't actually be hit (no thresholds).
        # Easier: drive the public function with a stub via patching.
        from pulseboard import monitor as monitor_mod

        original = monitor_mod.run_check
        monitor_mod.run_check = fake_run_check  # type: ignore[assignment]
        try:
            out = asyncio.run(run_check_with_thresholds(svc))
        finally:
            monitor_mod.run_check = original  # type: ignore[assignment]
        assert out.status == Status.UP
        assert "thresholds" not in out.details

    def test_threshold_outcome_attached_to_details(self):
        import asyncio

        from pulseboard import monitor as monitor_mod
        from pulseboard.monitor import run_check_with_thresholds

        svc = make_service(lat_warn=10.0, lat_crit=100.0)
        result = make_result(Status.UP, latency_ms=500.0)
        history = [make_result(Status.UP) for _ in range(3)]

        async def fake_run_check(s):
            return result

        original = monitor_mod.run_check
        monitor_mod.run_check = fake_run_check  # type: ignore[assignment]
        try:
            out = asyncio.run(
                run_check_with_thresholds(
                    svc, history_provider=lambda name: history
                )
            )
        finally:
            monitor_mod.run_check = original  # type: ignore[assignment]
        # Latency 500ms >= critical 100ms -> DOWN
        assert out.status == Status.DOWN
        assert "thresholds" in out.details
        thr = out.details["thresholds"]
        assert thr["latency_violation"] == "critical"
        assert any("critical" in r for r in thr["reasons"])
