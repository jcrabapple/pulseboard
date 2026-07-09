"""Data models for PulseBoard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ServiceType(str, Enum):
    HTTP = "http"
    TCP = "tcp"
    SSL = "ssl"
    DNS = "dns"


# Record types we support for DNS checks. Anything outside this list is
# rejected at config-load time with a helpful error.
DNS_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "SRV", "CAA", "PTR")


class Status(str, Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class ServiceConfig:
    """A monitored service/endpoint."""

    name: str
    url: str
    service_type: ServiceType = ServiceType.HTTP
    interval: int = 60  # seconds between checks
    timeout: int = 10  # seconds
    expected_status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    alert_webhook: str | None = None
    # For TCP / SSL checks
    host: str | None = None
    port: int | None = None
    # For SSL certificate checks
    ssl_expiry_warning_days: int = 14
    ssl_sni: str | None = None  # optional SNI override
    # For DNS checks
    dns_record_type: str = "A"  # one of DNS_RECORD_TYPES
    dns_server: str | None = None  # default: system resolver
    dns_expected: list[str] | None = None  # optional expected answers
    dns_match_mode: str = "any"  # "any" | "all" | "exact"
    # For HTTP content validation (all optional, only checked on HTTP services)
    body_contains: str | None = None  # substring that must appear in the response body
    body_not_contains: str | None = None  # substring that must NOT appear (e.g. error markers)
    body_regex: str | None = None  # regex that must match somewhere in the body
    json_path: str | None = None  # dot path like "data.status" or "user.id"
    json_path_expected: str | None = None  # if set, json_path value must equal this literal


@dataclass
class CheckResult:
    """Result of a single health check."""

    service_name: str
    timestamp: datetime
    status: Status
    latency_ms: float
    status_code: int | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_up(self) -> bool:
        return self.status == Status.UP

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 2),
            "status_code": self.status_code,
            "error": self.error,
        }


@dataclass
class ServiceSummary:
    """Aggregated stats for a service over a time window."""

    service_name: str
    total_checks: int
    successful_checks: int
    failed_checks: int
    uptime_pct: float
    avg_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    last_status: Status
    last_check: datetime | None
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0

    @property
    def status_emoji(self) -> str:
        return {"up": "🟢", "down": "🔴", "degraded": "🟡", "unknown": "⚪"}.get(
            self.last_status.value, "⚪"
        )
