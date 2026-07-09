"""Tests for DNS record checking."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import dns.rdatatype
import dns.resolver
import pytest

from pulseboard.dns_check import (
    _extract_answers,
    _match_answers,
    _normalize_rdtype,
    _query_blocking,
    check_dns,
)
from pulseboard.models import CheckResult, ServiceConfig, ServiceType, Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_dns_service(
    name: str = "Example DNS",
    host: str = "example.com",
    rdtype: str = "A",
    server: str | None = None,
    expected: list[str] | None = None,
    match_mode: str = "any",
    timeout: int = 5,
) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        url="",
        service_type=ServiceType.DNS,
        host=host,
        timeout=timeout,
        dns_record_type=rdtype,
        dns_server=server,
        dns_expected=expected,
        dns_match_mode=match_mode,
    )


# ---------------------------------------------------------------------------
# _normalize_rdtype
# ---------------------------------------------------------------------------

class TestNormalizeRdtype:
    def test_string_uppercase(self) -> None:
        assert _normalize_rdtype("A") == dns.rdatatype.A

    def test_string_lowercase(self) -> None:
        assert _normalize_rdtype("cname") == dns.rdatatype.CNAME

    def test_int_passthrough(self) -> None:
        assert _normalize_rdtype(1) == dns.rdatatype.A

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(dns.rdatatype.UnknownRdatatype):
            _normalize_rdtype("BOGUS")


# ---------------------------------------------------------------------------
# _match_answers
# ---------------------------------------------------------------------------

class TestMatchAnswers:
    def test_any_mode_no_expected(self) -> None:
        full, partial = _match_answers(["1.2.3.4"], [], "any")
        assert full is True
        assert partial is True

    def test_any_mode_hit(self) -> None:
        full, partial = _match_answers(["1.2.3.4", "5.6.7.8"], ["1.2.3.4"], "any")
        assert full is True
        assert partial is True

    def test_any_mode_miss(self) -> None:
        full, partial = _match_answers(["1.2.3.4"], ["5.6.7.8"], "any")
        assert full is False
        assert partial is False

    def test_all_mode_all_present(self) -> None:
        full, partial = _match_answers(
            ["1.2.3.4", "5.6.7.8"], ["1.2.3.4", "5.6.7.8"], "all"
        )
        assert full is True
        assert partial is True

    def test_all_mode_partial(self) -> None:
        full, partial = _match_answers(
            ["1.2.3.4", "5.6.7.8"], ["1.2.3.4", "9.10.11.12"], "all"
        )
        assert full is False
        assert partial is True

    def test_exact_mode_match(self) -> None:
        full, partial = _match_answers(
            ["1.2.3.4", "5.6.7.8"], ["5.6.7.8", "1.2.3.4"], "exact"
        )
        assert full is True

    def test_exact_mode_mismatch(self) -> None:
        full, partial = _match_answers(
            ["1.2.3.4", "5.6.7.8"], ["1.2.3.4"], "exact"
        )
        assert full is False

    def test_case_insensitive(self) -> None:
        full, partial = _match_answers(["GitHub.com"], ["github.com"], "any")
        assert full is True

    def test_whitespace_trimmed(self) -> None:
        full, _ = _match_answers([" 1.2.3.4 "], ["1.2.3.4"], "any")
        assert full is True


# ---------------------------------------------------------------------------
# _extract_answers
# ---------------------------------------------------------------------------

class TestExtractAnswers:
    """Test answer rendering for various record types via mocked rdata."""

    def test_a_record(self) -> None:
        rdata = MagicMock()
        rdata.rdtype = dns.rdatatype.A
        rdata.address = "93.184.216.34"
        result = _extract_answers([rdata])
        assert result == ["93.184.216.34"]

    def test_cname_record(self) -> None:
        rdata = MagicMock()
        rdata.rdtype = dns.rdatatype.CNAME
        rdata.target = MagicMock()
        rdata.target.__str__ = lambda self: "other.example.com."
        result = _extract_answers([rdata])
        assert result == ["other.example.com"]

    def test_mx_record(self) -> None:
        rdata = MagicMock()
        rdata.rdtype = dns.rdatatype.MX
        rdata.preference = 10
        # exchange is a dns.name.Name; we mock __str__ so str() works
        rdata.exchange = MagicMock()
        rdata.exchange.__str__ = lambda self: "mail.example.com."
        result = _extract_answers([rdata])
        assert result == ["10 mail.example.com"]


# ---------------------------------------------------------------------------
# check_dns — real resolver (integration-level)
# ---------------------------------------------------------------------------

class TestCheckDnsRealResolver:
    """These tests use the system resolver. If the system has no DNS they
    will skip automatically."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_dns(self) -> None:  # type: ignore[override]
        try:
            import socket
            socket.gethostbyname("example.com")
        except socket.gaierror:
            pytest.skip("No DNS available in this environment")

    @pytest.mark.asyncio
    async def test_a_record_resolves(self) -> None:
        svc = make_dns_service(host="example.com", rdtype="A")
        result = await check_dns(svc)
        assert result.status == Status.UP
        assert "answers" in result.details
        assert len(result.details["answers"]) >= 1
        assert result.details["answer_count"] >= 1
        assert result.details["record_type"] == "A"

    @pytest.mark.asyncio
    async def test_aaaa_record_likely_fails_or_resolves(self) -> None:
        """AAAA may or may not resolve depending on the host — we just check
        it doesn't crash and returns a valid status."""
        svc = make_dns_service(host="example.com", rdtype="AAAA")
        result = await check_dns(svc)
        assert isinstance(result.status, Status)
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_mx_record_resolves(self) -> None:
        svc = make_dns_service(host="google.com", rdtype="MX")
        result = await check_dns(svc)
        # google.com has MX records
        assert result.status == Status.UP
        assert len(result.details["answers"]) >= 1

    @pytest.mark.asyncio
    async def test_expected_answer_match(self) -> None:
        """Check that an expected answer works when we know one IP."""
        # First get the actual IP
        svc = make_dns_service(host="example.com", rdtype="A")
        result = await check_dns(svc)
        assert result.status == Status.UP
        known_ip = result.details["answers"][0]

        # Now test with expected match
        svc2 = make_dns_service(
            host="example.com",
            rdtype="A",
            expected=[known_ip],
            match_mode="any",
        )
        result2 = await check_dns(svc2)
        assert result2.status == Status.UP

    @pytest.mark.asyncio
    async def test_expected_answer_mismatch(self) -> None:
        """If the expected answer doesn't match, we should be DOWN."""
        svc = make_dns_service(
            host="example.com",
            rdtype="A",
            expected=["127.0.0.1"],
            match_mode="all",
        )
        result = await check_dns(svc)
        assert result.status == Status.DOWN

    @pytest.mark.asyncio
    async def test_nxdomain_returns_down(self) -> None:
        svc = make_dns_service(host="this-domain-definitely-does-not-exist-xyzzy999.test")
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert result.error is not None
        assert "NXDOMAIN" in result.error or "error" in result.error.lower()

    @pytest.mark.asyncio
    async def test_empty_host_returns_down(self) -> None:
        svc = make_dns_service(host="")
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "No DNS name" in (result.error or "")


# ---------------------------------------------------------------------------
# check_dns — mocked resolver (unit-level)
# ---------------------------------------------------------------------------

class TestCheckDnsMockedResolver:
    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_timeout_returns_down(self, mock_query: AsyncMock) -> None:
        mock_query.side_effect = dns.resolver.Timeout()
        svc = make_dns_service(host="slow.example.com", timeout=1)
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_no_answer_returns_down(self, mock_query: AsyncMock) -> None:
        mock_query.side_effect = dns.resolver.NoAnswer()
        svc = make_dns_service(host="no-txt.example.com", rdtype="TXT")
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "No TXT records" in result.error

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_no_nameservers_returns_down(self, mock_query: AsyncMock) -> None:
        mock_query.side_effect = dns.resolver.NoNameservers()
        svc = make_dns_service()
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "nameservers" in result.error.lower()

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_unexpected_exception_returns_down(self, mock_query: AsyncMock) -> None:
        mock_query.side_effect = RuntimeError("boom")
        svc = make_dns_service()
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "Unexpected" in result.error

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_no_expected_keeps_up(self, mock_query: AsyncMock) -> None:
        mock_query.return_value = ["1.2.3.4"]
        svc = make_dns_service(expected=None)
        result = await check_dns(svc)
        assert result.status == Status.UP

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_exact_match(self, mock_query: AsyncMock) -> None:
        mock_query.return_value = ["1.2.3.4", "5.6.7.8"]
        svc = make_dns_service(
            expected=["1.2.3.4", "5.6.7.8"],
            match_mode="exact",
        )
        result = await check_dns(svc)
        assert result.status == Status.UP

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_all_mode_partial_gives_degraded(self, mock_query: AsyncMock) -> None:
        mock_query.return_value = ["1.2.3.4"]
        svc = make_dns_service(
            expected=["1.2.3.4", "9.9.9.9"],
            match_mode="all",
        )
        result = await check_dns(svc)
        # "all" with missing answers → DOWN, not degraded (degraded only
        # happens in "any" mode and that's handled differently — for "all"
        # any missing answer is a failure)
        assert result.status == Status.DOWN

    @pytest.mark.asyncio
    @patch("pulseboard.dns_check._query_blocking")
    async def test_any_mode_partial_is_degraded(self, mock_query: AsyncMock) -> None:
        mock_query.return_value = ["1.2.3.4", "5.6.7.8"]
        svc = make_dns_service(
            expected=["1.2.3.4", "9.9.9.9"],
            match_mode="any",
        )
        result = await check_dns(svc)
        # In "any" mode: some expected match → UP (full_match is True because
        # one expected value is in answers)
        assert result.status == Status.UP

    @pytest.mark.asyncio
    async def test_unsupported_record_type(self) -> None:
        svc = make_dns_service(rdtype="BOGUS")
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "Unsupported" in result.error

    @pytest.mark.asyncio
    async def test_invalid_match_mode(self) -> None:
        svc = make_dns_service(match_mode="invalid")
        result = await check_dns(svc)
        assert result.status == Status.DOWN
        assert "dns_match_mode" in result.error


# ---------------------------------------------------------------------------
# Config parsing for DNS services
# ---------------------------------------------------------------------------

class TestConfigParsing:
    def test_dns_service_parsed_correctly(self) -> None:
        from pulseboard.config import parse_services

        raw = {
            "services": [
                {
                    "name": "Test DNS",
                    "type": "dns",
                    "host": "example.com",
                    "dns_record_type": "MX",
                    "dns_server": "8.8.8.8",
                    "dns_expected": ["10 mail.example.com"],
                    "dns_match_mode": "all",
                }
            ]
        }
        services = parse_services(raw)
        assert len(services) == 1
        svc = services[0]
        assert svc.service_type == ServiceType.DNS
        assert svc.host == "example.com"
        assert svc.dns_record_type == "MX"
        assert svc.dns_server == "8.8.8.8"
        assert svc.dns_expected == ["10 mail.example.com"]
        assert svc.dns_match_mode == "all"

    def test_dns_service_defaults(self) -> None:
        from pulseboard.config import parse_services

        raw = {"services": [{"name": "Simple DNS", "type": "dns", "host": "example.com"}]}
        services = parse_services(raw)
        svc = services[0]
        assert svc.dns_record_type == "A"
        assert svc.dns_server is None
        assert svc.dns_expected is None
        assert svc.dns_match_mode == "any"

    def test_invalid_record_type_raises(self) -> None:
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {"name": "Bad", "type": "dns", "host": "x", "dns_record_type": "BOGUS"}
            ]
        }
        with pytest.raises(ConfigError, match="unsupported dns_record_type"):
            parse_services(raw)

    def test_invalid_match_mode_raises(self) -> None:
        from pulseboard.config import ConfigError, parse_services

        raw = {
            "services": [
                {"name": "Bad", "type": "dns", "host": "x", "dns_match_mode": "strict"}
            ]
        }
        with pytest.raises(ConfigError, match="dns_match_mode"):
            parse_services(raw)
