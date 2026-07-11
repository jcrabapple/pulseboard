"""Tests for core HTTP monitoring behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pulseboard.models import ServiceConfig, Status
from pulseboard.monitor import check_http


def _response(
    status_code: int = 200,
    retry_after: str | None = None,
    final_url: str = "https://example.com/health",
    history: list[httpx.Response] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = b"ok"
    response.text = "ok"
    response.url = httpx.URL(final_url)
    response.headers = {"content-type": "text/plain"}
    response.history = history if history is not None else []
    if retry_after is not None:
        response.headers = {**response.headers, "retry-after": retry_after}
    return response


@pytest.mark.asyncio
async def test_http_check_verifies_tls_certificates_by_default() -> None:
    service = ServiceConfig(name="secure", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    client_class.assert_called_once_with(
        timeout=service.timeout,
        follow_redirects=True,
        verify=True,
    )


@pytest.mark.asyncio
async def test_http_check_marks_429_as_degraded_and_reports_retry_after() -> None:
    """A 429 should be DEGRADED, surface the Retry-After header, and be flagged."""
    service = ServiceConfig(name="ratelimit-api", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(
            return_value=_response(status_code=429, retry_after="30")
        )

        result = await check_http(service)

    assert result.status == Status.DEGRADED
    assert result.error is not None
    assert "429" in result.error
    assert result.details.get("rate_limited") is True
    assert result.details.get("retry_after_seconds") == 30


@pytest.mark.asyncio
async def test_http_check_429_without_retry_after_header_is_flagged_as_rate_limited() -> None:
    """Even without a Retry-After header, a 429 should be marked rate-limited."""
    service = ServiceConfig(name="ratelimit-api", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response(status_code=429))

        result = await check_http(service)

    assert result.status == Status.DEGRADED
    assert result.details.get("rate_limited") is True
    # No Retry-After header means we don't know when to retry; absent, not 0.
    assert "retry_after_seconds" not in result.details


@pytest.mark.asyncio
async def test_http_check_defaults_to_get_method() -> None:
    """When no method is configured, the HTTP check uses GET by default."""
    service = ServiceConfig(name="default", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    client.request.assert_called_once()
    call = client.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == service.url
    # Headers now include a default PulseBoard User-Agent.
    assert call.kwargs["headers"].get("User-Agent", "").startswith("PulseBoard/")


@pytest.mark.asyncio
async def test_http_check_sends_pulseboard_user_agent() -> None:
    """The HTTP check should identify itself with a PulseBoard/<version> User-Agent.

    Target services can use this to filter firewall / rate-limit rules,
    log the monitor separately from random scrapers, and correlate traffic.
    """
    from pulseboard import __version__

    service = ServiceConfig(name="ua-check", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    # The third positional arg to client.request is ``headers``.
    sent_headers = client.request.call_args.kwargs["headers"]
    assert sent_headers.get("User-Agent") == f"PulseBoard/{__version__}"


@pytest.mark.asyncio
async def test_http_check_respects_user_configured_user_agent() -> None:
    """A user-set ``User-Agent`` header in config overrides the default."""
    service = ServiceConfig(
        name="custom-ua",
        url="https://example.com/health",
        headers={"User-Agent": "my-monitor/1.0"},
    )

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    sent_headers = client.request.call_args.kwargs["headers"]
    # User-configured UA wins; we do not clobber it.
    assert sent_headers.get("User-Agent") == "my-monitor/1.0"


@pytest.mark.asyncio
async def test_http_check_uses_HEAD_method_when_configured() -> None:
    """A service with method='HEAD' should issue a HEAD request, not GET."""
    service = ServiceConfig(
        name="head-check", url="https://example.com/health", method="HEAD"
    )

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    call = client.request.call_args
    assert call.args[0] == "HEAD"
    assert call.args[1] == service.url


@pytest.mark.asyncio
async def test_http_check_uses_POST_method_when_configured() -> None:
    """A service with method='POST' should issue a POST request, not GET."""
    service = ServiceConfig(
        name="post-check", url="https://example.com/health", method="POST"
    )

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    call = client.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == service.url



def _redirect_response(status_code: int, location: str) -> MagicMock:
    """Build a mock 3xx response for a single redirect hop."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = {"location": location}
    resp.url = httpx.URL(location)
    resp.content = b""
    resp.text = ""
    resp.history = []
    return resp


@pytest.mark.asyncio
async def test_http_check_records_redirect_count_in_details() -> None:
    """A redirect chain should surface redirect_count in details for visibility."""
    service = ServiceConfig(name="redirector", url="https://example.com/old")
    final = _response(
        status_code=200,
        final_url="https://example.com/new",
        history=[_redirect_response(301, "https://example.com/new")],
    )
    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=final)
        result = await check_http(service)
    assert result.status == Status.UP
    assert result.details.get("redirect_count") == 1
    assert result.details.get("final_url") == "https://example.com/new"


@pytest.mark.asyncio
async def test_http_check_no_redirects_has_zero_count() -> None:
    """A direct 200 with no redirects should report redirect_count=0."""
    service = ServiceConfig(name="direct", url="https://example.com/health")
    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=_response())
        result = await check_http(service)
    assert result.status == Status.UP
    assert result.details.get("redirect_count") == 0


@pytest.mark.asyncio
async def test_http_check_records_multi_hop_redirect_chain() -> None:
    """A multi-hop redirect chain (A→B→C) should report redirect_count=2."""
    service = ServiceConfig(name="multi-hop", url="https://a.example.com")
    final_url = "https://c.example.com"
    hop1 = _redirect_response(301, "https://b.example.com")
    hop2 = _redirect_response(302, final_url)
    final = _response(
        status_code=200,
        final_url=final_url,
        history=[hop1, hop2],
    )
    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.request = AsyncMock(return_value=final)
        result = await check_http(service)
    assert result.status == Status.UP
    assert result.details.get("redirect_count") == 2
    assert result.details.get("final_url") == final_url
