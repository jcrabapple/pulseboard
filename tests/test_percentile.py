"""Tests for the percentile helper (p50 / p95 / p99 latency etc.)."""

from __future__ import annotations

from pulseboard.storage import percentile


# ---------------------------------------------------------------------------
# Core definition
# ---------------------------------------------------------------------------


def test_percentile_empty_list_returns_zero() -> None:
    """An empty sample set has no percentile — return 0.0."""
    assert percentile([], 50) == 0.0


def test_percentile_single_element_returns_that_element() -> None:
    """The single data point is every percentile."""
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 95) == 42.0
    assert percentile([42.0], 99) == 42.0


# ---------------------------------------------------------------------------
# Even-count interpolation
# ---------------------------------------------------------------------------


def test_percentile_p50_even_count_interpolates_median() -> None:
    """p50 of [10, 20, 30, 40] should be 25.0 (midpoint of the two middles)."""
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0


def test_percentile_p50_two_elements_averages() -> None:
    """p50 of [100, 200] is the average — 150.0."""
    assert percentile([100.0, 200.0], 50) == 150.0


# ---------------------------------------------------------------------------
# Odd-count behaviour
# ---------------------------------------------------------------------------


def test_percentile_p50_odd_count_returns_exact_median() -> None:
    """p50 of [10, 20, 30, 40, 50] should be 30.0."""
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 50) == 30.0


def test_percentile_p50_twenty_elements_returns_interpolated_median() -> None:
    """p50 of [1..20] should be 10.5, not 11 (the old truncation bug)."""
    lats = [float(i) for i in range(1, 21)]
    assert percentile(lats, 50) == 10.5


# ---------------------------------------------------------------------------
# High-percentile behaviour
# ---------------------------------------------------------------------------


def test_percentile_p95_beats_p50() -> None:
    """Sanity: a higher percentile must be >= a lower one."""
    lats = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0]
    assert percentile(lats, 95) >= percentile(lats, 50)


def test_percentile_p99_on_nine_samples() -> None:
    """p99 of [1..9]: index 7.92 -> interpolate between 8 and 9 -> 8.92."""
    lats = [float(i) for i in range(1, 10)]
    assert round(percentile(lats, 99), 2) == 8.92


def test_percentile_p95_twenty_elements() -> None:
    """p95 of [1..20] -> index 18.05 -> interpolate 19 and 20 (weight 0.05) -> 19.05."""
    lats = [float(i) for i in range(1, 21)]
    assert round(percentile(lats, 95), 2) == 19.05


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_percentile_zero_percentile_returns_min() -> None:
    """Percentile 0 is the minimum."""
    assert percentile([10.0, 20.0, 30.0], 0) == 10.0


def test_percentile_hundred_percentile_returns_max() -> None:
    """Percentile 100 is the maximum."""
    assert percentile([10.0, 20.0, 30.0], 100) == 30.0


def test_percentile_handles_unsorted_input() -> None:
    """Input should not need to be pre-sorted."""
    assert percentile([30.0, 10.0, 50.0, 20.0, 40.0], 50) == 30.0


def test_percentile_handles_duplicates() -> None:
    """Repeated values should work correctly."""
    assert percentile([5.0, 5.0, 5.0, 5.0], 50) == 5.0


def test_percentile_handles_integers() -> None:
    """ integers should also work (type-agnostic)."""
    assert percentile([10, 20, 30, 40], 50) == 25.0
