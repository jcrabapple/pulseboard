"""Tests for core HTTP monitoring behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pulseboard.models import ServiceConfig, Status
from pulseboard.monitor import check_http


def _response(status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = b"ok"
    response.text = "ok"
    response.url = httpx.URL("https://example.com/health")
    response.headers = {"content-type": "text/plain"}
    return response


@pytest.mark.asyncio
async def test_http_check_verifies_tls_certificates_by_default() -> None:
    service = ServiceConfig(name="secure", url="https://example.com/health")

    with patch("pulseboard.monitor.httpx.AsyncClient") as client_class:
        client = client_class.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_response())

        result = await check_http(service)

    assert result.status == Status.UP
    client_class.assert_called_once_with(
        timeout=service.timeout,
        follow_redirects=True,
        verify=True,
    )
