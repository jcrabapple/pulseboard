"""Core monitoring engine — runs health checks against services."""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx

from .models import CheckResult, ServiceConfig, ServiceType, Status
from .content_check import has_content_checks, validate_body
from .dns_check import check_dns
from .ssl_check import check_ssl
from .thresholds import evaluate_thresholds
from .groups import apply_dependency_impact


async def check_http(service: ServiceConfig) -> CheckResult:
    """Run an HTTP health check."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=service.timeout,
            follow_redirects=True,
            verify=True,
        ) as client:
            resp = await client.request(
                service.method,
                service.url,
                headers=service.headers,
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            is_rate_limited = resp.status_code == 429
            if resp.status_code == service.expected_status:
                status = Status.UP
                error = None
            elif is_rate_limited:
                # HTTP 429 Too Many Requests — the target is rate-limiting
                # us. Treat this as DEGRADED (not DOWN): the service is
                # healthy, but our polling is being throttled. Surface the
                # Retry-After hint so callers (watch/dashboard) can back off
                # in a future iteration.
                status = Status.DEGRADED
                retry_after_raw = resp.headers.get("retry-after")
                error = "HTTP 429 Too Many Requests"
                if retry_after_raw is not None:
                    error += f" (retry after {retry_after_raw}s)"
            elif 500 <= resp.status_code < 600:
                status = Status.DOWN
                error = f"HTTP {resp.status_code}"
            else:
                status = Status.DEGRADED
                error = f"Unexpected status {resp.status_code} (expected {service.expected_status})"

            details: dict[str, Any] = {
                "content_length": len(resp.content),
                "url": str(resp.url),
                # Redirect visibility — surface how many 3xx hops led to
                # the final response and where we landed. ``history`` is
                # populated by httpx when ``follow_redirects=True``.
                "redirect_count": len(getattr(resp, "history", [])),
                "final_url": str(resp.url),
            }

            if is_rate_limited:
                details["rate_limited"] = True
                retry_after_raw = resp.headers.get("retry-after")
                if retry_after_raw is not None:
                    try:
                        details["retry_after_seconds"] = int(retry_after_raw)
                    except ValueError:
                        # Non-integer Retry-After (e.g. HTTP-date) — skip
                        # the numeric hint but still flag as rate-limited.
                        pass

            # Body content validation runs whenever checks are configured.
            # An empty or undecodable body is still meaningful: required
            # substrings, regexes, and JSON paths must fail rather than be
            # silently skipped.
            body_text = ""
            try:
                body_text = resp.text
            except Exception:  # pragma: no cover - decoding rarely fails
                body_text = ""

            if has_content_checks(service):
                report = validate_body(service, body_text, resp.headers.get("content-type"))
                details["content_checks"] = report.checks
                if not report.passed and status == Status.UP:
                    # HTTP says OK but the body indicates a problem —
                    # downgrade to DEGRADED with the validation failures
                    # as the user-visible error.
                    status = Status.DEGRADED
                    error = "; ".join(report.failures)

            return CheckResult(
                service_name=service.name,
                timestamp=datetime.now(timezone.utc),
                status=status,
                latency_ms=elapsed_ms,
                status_code=resp.status_code,
                error=error,
                details=details,
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
    if service.service_type == ServiceType.SSL:
        return await check_ssl(service)
    if service.service_type == ServiceType.DNS:
        return await check_dns(service)
    return await check_http(service)


async def run_all_checks(services: list[ServiceConfig]) -> list[CheckResult]:
    """Run checks against all services concurrently."""
    tasks = [run_check(svc) for svc in services]
    return await asyncio.gather(*tasks)


# A history provider takes a service name and returns recent CheckResults
# (any order) used to evaluate error-rate thresholds.
HistoryProvider = Callable[[str], Iterable[CheckResult]]


async def run_check_with_thresholds(
    service: ServiceConfig,
    history_provider: HistoryProvider | None = None,
) -> CheckResult:
    """Run ``run_check`` and then apply configured latency / error-rate thresholds.

    The returned :class:`CheckResult` reflects the worst of the underlying
    status and any threshold violations, and its ``details`` dict gains a
    ``"thresholds"`` key with the structured :class:`ThresholdOutcome`.
    """
    result = await run_check(service)
    if not service.has_any_threshold():
        return result

    history = list(history_provider(service.name)) if history_provider else []
    outcome = evaluate_thresholds(result, service, history)
    if outcome.status != result.status:
        result.status = outcome.status
        # Keep the original error if there was one; otherwise surface the
        # threshold reasons so dashboards / alerts see *why* the status changed.
        if not result.error and outcome.reasons:
            result.error = "; ".join(outcome.reasons)
    result.details["thresholds"] = outcome.to_dict()
    return result


async def run_all_checks_with_thresholds(
    services: list[ServiceConfig],
    history_provider: HistoryProvider | None = None,
) -> list[CheckResult]:
    """Run checks against all services concurrently, applying thresholds.

    After all checks complete, :func:`pulseboard.groups.apply_dependency_impact`
    is run so that dependent services whose dependencies are not UP are
    annotated and (conservatively) downgraded. Services are gathered
    concurrently, so dependency ordering does not need to be enforced at
    scheduling time — ``apply_dependency_impact`` reads statuses from
    the fully-populated result set in a single pass.

    The returned list preserves the caller's original ``services`` order
    so callers don't need to sort by name when iterating.
    """
    if not services:
        return []
    tasks = [
        run_check_with_thresholds(svc, history_provider) for svc in services
    ]
    results = await asyncio.gather(*tasks)
    apply_dependency_impact(services, results)
    return results
