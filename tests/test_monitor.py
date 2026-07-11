"""Tests for core HTTP monitoring behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pulseboard.models import ServiceConfig, Status
from pulseboard.monitor import check_http


def _response(status_code: int = 200, retry_after: str | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = b"ok"
    response.text = "ok"
    response.url = httpx.URL("https://example.com/health")
    headers = {"content-type": "text/plain"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    response.headers = headers
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
    client.request.assert_called_once_with(
        "GET", service.url, headers=service.headers
    )


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
    client.request.assert_called_once_with(
        "HEAD", service.url, headers=service.headers
    )


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
    client.request.assert_called_once_with(
        "POST", service.url, headers=service.headers
    )
