"""Tests for HTTP body content validation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pulseboard.content_check import (
    ValidationReport,
    _resolve_json_path,
    has_content_checks,
    validate_body,
)
from pulseboard.models import CheckResult, ServiceConfig, ServiceType, Status
from pulseboard.monitor import check_http


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_http_service(
    name: str = "API",
    url: str = "https://example.com/api",
    body_contains: str | None = None,
    body_not_contains: str | None = None,
    body_regex: str | None = None,
    json_path: str | None = None,
    json_path_expected: str | None = None,
) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        url=url,
        service_type=ServiceType.HTTP,
        body_contains=body_contains,
        body_not_contains=body_not_contains,
        body_regex=body_regex,
        json_path=json_path,
        json_path_expected=json_path_expected,
    )


# ---------------------------------------------------------------------------
# has_content_checks
# ---------------------------------------------------------------------------


class TestHasContentChecks:
    def test_no_checks_returns_false(self) -> None:
        svc = make_http_service()
        assert has_content_checks(svc) is False

    def test_body_contains_alone(self) -> None:
        assert has_content_checks(make_http_service(body_contains="ok")) is True

    def test_body_not_contains_alone(self) -> None:
        assert has_content_checks(make_http_service(body_not_contains="error")) is True

    def test_body_regex_alone(self) -> None:
        assert has_content_checks(make_http_service(body_regex=r"\d+")) is True

    def test_json_path_alone(self) -> None:
        assert has_content_checks(make_http_service(json_path="a.b")) is True

    def test_empty_strings_count_as_unset(self) -> None:
        # Empty string is falsy — should NOT count as a configured check.
        svc = make_http_service(body_contains="", body_regex="", json_path="")
        assert has_content_checks(svc) is False


# ---------------------------------------------------------------------------
# body_contains
# ---------------------------------------------------------------------------


class TestBodyContains:
    def test_pass_when_substring_present(self) -> None:
        svc = make_http_service(body_contains="hello")
        report = validate_body(svc, "say hello world")
        assert report.passed is True
        assert report.failures == []
        assert report.checks[0]["passed"] is True

    def test_fail_when_substring_missing(self) -> None:
        svc = make_http_service(body_contains="expected_marker")
        report = validate_body(svc, "totally different body")
        assert report.passed is False
        assert any("expected_marker" in f for f in report.failures)

    def test_case_sensitive(self) -> None:
        svc = make_http_service(body_contains="Hello")
        # Substring search is intentionally case-sensitive.
        assert validate_body(svc, "hello").passed is False
        assert validate_body(svc, "Hello").passed is True


# ---------------------------------------------------------------------------
# body_not_contains
# ---------------------------------------------------------------------------


class TestBodyNotContains:
    def test_pass_when_forbidden_absent(self) -> None:
        svc = make_http_service(body_not_contains="stack trace")
        assert validate_body(svc, "all good here").passed is True

    def test_fail_when_forbidden_present(self) -> None:
        svc = make_http_service(body_not_contains="error")
        assert validate_body(svc, "internal error: oops").passed is False

    def test_combined_with_body_contains(self) -> None:
        svc = make_http_service(
            body_contains="healthy",
            body_not_contains="degraded",
        )
        assert validate_body(svc, "status: healthy").passed is True
        assert validate_body(svc, "status: healthy but degraded").passed is False
        assert validate_body(svc, "status: down").passed is False


# ---------------------------------------------------------------------------
# body_regex
# ---------------------------------------------------------------------------


class TestBodyRegex:
    def test_match(self) -> None:
        svc = make_http_service(body_regex=r'"status"\s*:\s*"ok"')
        body = '{"status": "ok", "code": 200}'
        report = validate_body(svc, body)
        assert report.passed is True
        # The matched text is captured for debugging.
        assert report.checks[0]["matched"] == '"status": "ok"'

    def test_no_match(self) -> None:
        svc = make_http_service(body_regex=r"version=\d+\.\d+\.\d+")
        report = validate_body(svc, "no version info here")
        assert report.passed is False
        assert "matched" not in report.checks[0]

    def test_invalid_regex_marked_as_failure(self) -> None:
        svc = make_http_service(body_regex=r"[unclosed")
        report = validate_body(svc, "anything")
        assert report.passed is False
        # The error is recorded so users can debug their config.
        assert "invalid regex" in report.checks[0].get("error", "")
        assert any("invalid regex" in f for f in report.failures)

    def test_anchors(self) -> None:
        svc = make_http_service(body_regex=r"^OK$")
        assert validate_body(svc, "OK").passed is True
        assert validate_body(svc, "not OK").passed is False


# ---------------------------------------------------------------------------
# json_path
# ---------------------------------------------------------------------------


class TestJsonPath:
    def test_resolve_simple_key(self) -> None:
        svc = make_http_service(json_path="status")
        report = validate_body(svc, '{"status": "ok"}')
        assert report.passed is True
        assert report.checks[0]["value"] == "ok"

    def test_resolve_nested_key(self) -> None:
        svc = make_http_service(json_path="data.user.id")
        report = validate_body(svc, '{"data": {"user": {"id": 42}}}')
        assert report.passed is True
        assert report.checks[0]["value"] == 42

    def test_resolve_list_index(self) -> None:
        svc = make_http_service(json_path="items.0.name")
        report = validate_body(svc, '{"items": [{"name": "first"}, {"name": "second"}]}')
        assert report.passed is True
        assert report.checks[0]["value"] == "first"

    def test_missing_path_returns_failure(self) -> None:
        svc = make_http_service(json_path="data.user.id")
        report = validate_body(svc, '{"data": {"other": 1}}')
        assert report.passed is False
        assert "did not resolve" in report.checks[0].get("error", "")

    def test_non_json_body_fails(self) -> None:
        svc = make_http_service(json_path="status")
        report = validate_body(svc, "<html>not json</html>")
        assert report.passed is False

    def test_empty_body_fails(self) -> None:
        svc = make_http_service(json_path="status")
        assert validate_body(svc, "").passed is False
        assert validate_body(svc, "   ").passed is False

    def test_array_root_resolves_to_value(self) -> None:
        svc = make_http_service(json_path="0")
        report = validate_body(svc, '[1, 2, 3]')
        assert report.passed is True
        assert report.checks[0]["value"] == 1

    def test_deeply_nested_array_index(self) -> None:
        svc = make_http_service(json_path="data.0.items.2")
        body = '{"data": [{"items": ["a", "b", "c"]}]}'
        report = validate_body(svc, body)
        assert report.passed is True
        assert report.checks[0]["value"] == "c"


# ---------------------------------------------------------------------------
# json_path_expected
# ---------------------------------------------------------------------------


class TestJsonPathExpected:
    def test_value_matches_expected(self) -> None:
        svc = make_http_service(json_path="status", json_path_expected="ok")
        assert validate_body(svc, '{"status": "ok"}').passed is True

    def test_value_does_not_match_expected(self) -> None:
        svc = make_http_service(json_path="status", json_path_expected="ok")
        report = validate_body(svc, '{"status": "degraded"}')
        assert report.passed is False
        assert report.checks[0]["value_matches_expected"] is False

    def test_int_value_compared_as_string(self) -> None:
        svc = make_http_service(json_path="code", json_path_expected="200")
        assert validate_body(svc, '{"code": 200}').passed is True
        assert validate_body(svc, '{"code": 500}').passed is False

    def test_bool_value_compared(self) -> None:
        svc = make_http_service(json_path="ok", json_path_expected="true")
        assert validate_body(svc, '{"ok": true}').passed is True
        assert validate_body(svc, '{"ok": false}').passed is False

    def test_null_value_compared(self) -> None:
        svc = make_http_service(json_path="error", json_path_expected="null")
        assert validate_body(svc, '{"error": null}').passed is True

    def test_missing_path_with_expected_still_fails(self) -> None:
        svc = make_http_service(json_path="missing.key", json_path_expected="anything")
        assert validate_body(svc, '{"other": 1}').passed is False


# ---------------------------------------------------------------------------
# _resolve_json_path edge cases (lower-level sanity)
# ---------------------------------------------------------------------------


class TestResolveJsonPathHelper:
    def test_empty_path_returns_full_doc(self) -> None:
        assert _resolve_json_path('{"a": 1}', "") == {"a": 1}

    def test_invalid_list_index_returns_missing(self) -> None:
        from pulseboard.content_check import _MISSING
        # Non-numeric segment against a list — can't navigate.
        assert _resolve_json_path('[1, 2]', "abc") is _MISSING

    def test_out_of_bounds_index(self) -> None:
        from pulseboard.content_check import _MISSING
        assert _resolve_json_path('[1, 2]', "5") is _MISSING

    def test_navigation_into_scalar_fails(self) -> None:
        from pulseboard.content_check import _MISSING
        # Once cur is a string, can't navigate further.
        assert _resolve_json_path('{"a": "string"}', "a.b") is _MISSING


# ---------------------------------------------------------------------------
# check_http integration — verify the checker wires validation in correctly
# ---------------------------------------------------------------------------


class TestCheckHttpIntegration:
    """End-to-end tests that check_http correctly downgrades UP → DEGRADED
    when body validation fails, and reports content_checks in details."""

    def _mock_response(self, status_code: int, body: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = body.encode("utf-8")
        resp.text = body
        resp.url = httpx.URL("https://example.com/api")
        resp.headers = {"content-type": "application/json"}
        return resp

    def test_passing_body_check_keeps_status_up(self) -> None:
        svc = make_http_service(
            body_contains="ok",
            json_path="status",
            json_path_expected="healthy",
        )
        resp = self._mock_response(200, '{"status": "healthy", "msg": "all ok"}')

        async def _run() -> CheckResult:
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
                return await check_http(svc)

        result = asyncio.run(_run())
        assert result.status == Status.UP
        assert result.error is None
        assert "content_checks" in result.details
        assert all(c["passed"] for c in result.details["content_checks"])

    def test_failing_body_check_downgrades_up_to_degraded(self) -> None:
        svc = make_http_service(
            json_path="status",
            json_path_expected="healthy",
        )
        # 200 OK but body says status=degraded
        resp = self._mock_response(200, '{"status": "degraded"}')

        async def _run() -> CheckResult:
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
                return await check_http(svc)

        result = asyncio.run(_run())
        assert result.status == Status.DEGRADED
        # The per-check entry explains *why* it failed, which is what
        # users will see in dashboards and alerts.
        json_check = next(
            c for c in result.details["content_checks"] if c["check"] == "json_path"
        )
        assert json_check["value_matches_expected"] is False
        assert "json_path" in (result.error or "")

    def test_empty_body_fails_required_content_check(self) -> None:
        svc = make_http_service(body_contains="healthy")
        resp = self._mock_response(200, "")

        async def _run() -> CheckResult:
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
                return await check_http(svc)

        result = asyncio.run(_run())

        assert result.status == Status.DEGRADED
        assert result.details["content_checks"] == [
            {"check": "body_contains", "expected": "healthy", "passed": False}
        ]
        assert "missing required substring" in (result.error or "")

    def test_500_response_stays_down_but_records_body_checks(self) -> None:
        # A 5xx is a hard DOWN, but the body check is still recorded in
        # details so users can see *why* the server failed (e.g. "outage"
        # substring match tells you it's a known failure page).
        svc = make_http_service(body_contains="healthy")
        resp = self._mock_response(500, "internal server error")

        async def _run() -> CheckResult:
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
                return await check_http(svc)

        result = asyncio.run(_run())
        assert result.status == Status.DOWN
        # content_checks is recorded for context but doesn't change status
        # of an already-DOWN response.
        assert "content_checks" in result.details
        assert not result.details["content_checks"][0]["passed"]

    def test_no_checks_configured_unchanged(self) -> None:
        svc = make_http_service()  # no body_*, no json_path
        resp = self._mock_response(200, '{"status": "anything"}')

        async def _run() -> CheckResult:
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
                return await check_http(svc)

        result = asyncio.run(_run())
        assert result.status == Status.UP
        assert "content_checks" not in result.details


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_content_check_fields_parsed(self) -> None:
        from pulseboard.config import parse_services

        raw = {
            "services": [
                {
                    "name": "StatusAPI",
                    "url": "https://status.example.com",
                    "body_contains": "all good",
                    "body_not_contains": "outage",
                    "body_regex": r"version=\d+",
                    "json_path": "data.status",
                    "json_path_expected": "ok",
                }
            ]
        }
        services = parse_services(raw)
        assert len(services) == 1
        svc = services[0]
        assert svc.body_contains == "all good"
        assert svc.body_not_contains == "outage"
        assert svc.body_regex == r"version=\d+"
        assert svc.json_path == "data.status"
        assert svc.json_path_expected == "ok"

    def test_content_check_fields_default_to_none(self) -> None:
        from pulseboard.config import parse_services

        services = parse_services({"services": [{"name": "x", "url": "https://x"}]})
        assert services[0].body_contains is None
        assert services[0].body_not_contains is None
        assert services[0].body_regex is None
        assert services[0].json_path is None
        assert services[0].json_path_expected is None

    def test_json_path_expected_coerced_to_string(self) -> None:
        # Users sometimes write `json_path_expected: 200` — coerce to string.
        from pulseboard.config import parse_services

        raw = {
            "services": [
                {
                    "name": "x",
                    "url": "https://x",
                    "json_path": "code",
                    "json_path_expected": 200,
                }
            ]
        }
        services = parse_services(raw)
        assert services[0].json_path_expected == "200"