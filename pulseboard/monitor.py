"""Core monitoring engine — runs health checks against services."""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import CheckResult, ServiceConfig, ServiceType, Status


async def check_http(service: ServiceConfig) -> CheckResult:
    """Run an HTTP health check."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=service.timeout,
            follow_redirects=True,
            verify=False,
        ) as client:
            resp = await client.get(service.url, headers=service.headers)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code == service.expected_status:
                status = Status.UP
                error = None
            elif 500 <= resp.status_code < 600:
                status = Status.DOWN
                error = f"HTTP {resp.status_code}"
            else:
                status = Status.DEGRADED
                error = f"Unexpected status {resp.status_code} (expected {service.expected_status})"

            return CheckResult(
                service_name=service.name,
                timestamp=datetime.now(timezone.utc),
                status=status,
                latency_ms=elapsed_ms,
                status_code=resp.status_code,
                error=error,
                details={
                    "content_length": len(resp.content),
                    "url": str(resp.url),
                },
            )
    except httpx.TimeoutException:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"Timeout after {service.timeout}s",
        )
    except httpx.ConnectError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"Connection failed: {e}",
        )
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=str(e),
        )


async def check_tcp(service: ServiceConfig) -> CheckResult:
    """Run a TCP connectivity check."""
    host = service.host or service.url
    port = service.port or 80
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=service.timeout,
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        writer.close()
        await writer.wait_closed()
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.UP,
            latency_ms=elapsed_ms,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"TCP timeout after {service.timeout}s",
        )
    except OSError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"TCP error: {e}",
        )


async def run_check(service: ServiceConfig) -> CheckResult:
    """Dispatch to the right checker based on service type."""
    if service.service_type == ServiceType.TCP:
        return await check_tcp(service)
    return await check_http(service)


async def run_all_checks(services: list[ServiceConfig]) -> list[CheckResult]:
    """Run checks against all services concurrently."""
    tasks = [run_check(svc) for svc in services]
    return await asyncio.gather(*tasks)
