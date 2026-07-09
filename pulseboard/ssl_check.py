"""SSL/TLS certificate expiry checker.

Uses Python's stdlib ``ssl`` + ``socket`` modules so there are no extra
dependencies. Returns a :class:`CheckResult` whose ``latency_ms`` is the TLS
handshake time and whose ``details`` dict carries issuer, subject, and expiry
information.

Status semantics:

- ``UP``       - certificate is valid and not within the warning window
- ``DEGRADED`` - certificate is valid but expires within
                 ``ssl_expiry_warning_days``
- ``DOWN``     - connection failed, TLS handshake failed, or certificate is
                 already expired
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from .models import CheckResult, ServiceConfig, Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_host_port(service: ServiceConfig) -> tuple[str, int]:
    """Resolve ``(host, port)`` from explicit fields or a URL string.

    Exposed for tests and reuse. Prefers explicit ``host``/``port`` fields if
    present; otherwise pulls host:port out of an ``https://host:port/`` URL.
    """
    if service.host and service.port:
        return service.host, service.port
    url = (service.url or "").strip()
    if url.startswith("https://") or url.startswith("http://"):
        url = url.split("://", 1)[1]
    url = url.split("/", 1)[0]
    if ":" in url:
        host, _, port_str = url.partition(":")
        try:
            return host, int(port_str)
        except ValueError:
            return host, 443
    return url or (service.host or ""), 443


def _format_name(name: x509.Name) -> str:
    """Format an X509 Name into a readable 'CN=foo, O=bar' string."""
    try:
        parts = []
        for attr in name:
            parts.append(f"{attr.oid._name}={attr.value}")
        return ", ".join(parts) if parts else "<unknown>"
    except Exception:  # pragma: no cover - defensive
        return "<unknown>"


def _name_short(name: x509.Name) -> str:
    """Pull a short label out of a Name, preferring CN then O."""
    try:
        cn = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn:
            return cn[0].value
    except Exception:
        pass
    try:
        o = name.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        if o:
            return o[0].value
    except Exception:
        pass
    return _format_name(name)


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext suitable for the handshake probe.

    We deliberately disable chain validation here: the checker's job is to
    inspect expiry, not trust. Trust/chain validation would short-circuit
    before we get a chance to report on a real cert. We do still want to know
    whether the cert *would* parse cleanly, which is why we use cryptography
    on the raw DER bytes instead of relying on ``getpeercert()`` (which
    returns an empty dict when ``verify_mode`` is ``CERT_NONE``).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe_cert(
    service: ServiceConfig, host: str, port: int, timeout: float
) -> tuple[dict[str, Any], float]:
    """Open a TLS connection and pull the peer certificate.

    Returns ``(details_dict, elapsed_ms)``. Raises on connection/handshake
    failure. Exposed for testing.
    """
    ctx = _build_ssl_context()
    sni_host = service.ssl_sni or host
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as raw_sock:
        with ctx.wrap_socket(raw_sock, server_hostname=sni_host) as tls_sock:
            der_bytes = tls_sock.getpeercert(binary_form=True)
        elapsed_ms = (time.monotonic() - start) * 1000

    cert = x509.load_der_x509_certificate(der_bytes)

    subject = cert.subject
    issuer = cert.issuer
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    serial = cert.serial_number
    version = cert.version.name

    details: dict[str, Any] = {
        "host": host,
        "port": port,
        "sni_host": sni_host,
        "subject": _name_short(subject),
        "subject_full": _format_name(subject),
        "issuer": _name_short(issuer),
        "issuer_full": _format_name(issuer),
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "serial": str(serial),
        "version": version,
        "signature_algorithm": cert.signature_algorithm_oid._name,
    }

    now = datetime.now(timezone.utc)
    days_left = (not_after - now).total_seconds() / 86400
    details["expires_at"] = not_after.isoformat()
    details["days_until_expiry"] = round(days_left, 2)

    return details, elapsed_ms


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def check_ssl(service: ServiceConfig) -> CheckResult:
    """Run an SSL certificate expiry check against ``host:port``."""
    host, port = parse_host_port(service)
    timeout = float(service.timeout)
    start = time.monotonic()
    try:
        details, elapsed_ms = await asyncio.to_thread(
            probe_cert, service, host, port, timeout
        )
    except (ssl.SSLError, ssl.CertificateError) as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"TLS error: {e}",
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"TLS timeout after {service.timeout}s",
        )
    except OSError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"Connection failed: {e}",
        )
    except Exception as e:  # pragma: no cover - defensive
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=datetime.now(timezone.utc),
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"Unexpected error: {e}",
        )

    days_left = details.get("days_until_expiry")
    if days_left is None:
        status = Status.DOWN
        error = "Could not parse certificate expiry"
    elif days_left <= 0:
        status = Status.DOWN
        error = f"Certificate EXPIRED {abs(days_left):.1f} days ago"
    elif days_left <= service.ssl_expiry_warning_days:
        status = Status.DEGRADED
        error = f"Certificate expires in {days_left:.1f} days"
    else:
        status = Status.UP
        error = None

    return CheckResult(
        service_name=service.name,
        timestamp=datetime.now(timezone.utc),
        status=status,
        latency_ms=elapsed_ms,
        error=error,
        details=details,
    )