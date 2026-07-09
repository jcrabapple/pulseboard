"""DNS record checker.

Uses ``dnspython`` for proper DNS protocol support across record types (A,
AAAA, CNAME, MX, NS, TXT, SRV, CAA, PTR). The checker can either just confirm
the name resolves or optionally validate that the answers match an expected
list.

Status semantics:

- ``UP``       - query succeeded (and expected-answer check passed if set)
- ``DEGRADED`` - query succeeded but the expected-answer check partially matched
                 (only happens in ``any`` mode with no full match; if you need
                 strict matching use ``match_mode: all`` or ``exact``)
- ``DOWN``     - resolver failure, NXDOMAIN, timeout, or expected-answer
                 mismatch (in ``all``/``exact`` modes)

The ``latency_ms`` field is the time spent waiting on the resolver reply.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import dns.exception
import dns.rdatatype
import dns.resolver

from .models import DNS_RECORD_TYPES, CheckResult, ServiceConfig, Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_rdtype(rdtype: str | int) -> dns.rdatatype.RdataType:
    """Coerce a record-type label (string) into an ``RdataType``.

    Accepts both strings ("A", "aaaa") and ``dns.rdatatype`` constants.
    """
    if isinstance(rdtype, int):
        return dns.rdatatype.RdataType(rdtype)
    label = str(rdtype).strip().upper()
    return dns.rdatatype.from_text(label)


def _extract_answers(rdataset) -> list[str]:
    """Render an ``Answer.rrset`` (or ``ResolverResponse``) into strings.

    Each record type renders differently:

    - A / AAAA    -> the IP address
    - CNAME       -> the target hostname (no trailing dot)
    - MX          -> "preference exchange"
    - NS          -> the nameserver hostname
    - TXT         -> the joined text payload
    - SRV         -> "priority weight port target"
    - CAA        -> "flags tag value"
    - PTR         -> the target name
    """
    out: list[str] = []
    for rdata in rdataset:
        rt = rdata.rdtype
        if rt == dns.rdatatype.A or rt == dns.rdatatype.AAAA:
            out.append(rdata.address)
        elif rt == dns.rdatatype.CNAME or rt == dns.rdatatype.PTR or rt == dns.rdatatype.NS:
            out.append(str(rdata.target).rstrip("."))
        elif rt == dns.rdatatype.MX:
            out.append(f"{rdata.preference} {str(rdata.exchange).rstrip('.')}")
        elif rt == dns.rdatatype.TXT:
            # TXT records come in chunks; join them.
            try:
                out.append("".join(s.decode("utf-8", "replace") for s in rdata.strings))
            except Exception:  # pragma: no cover - defensive
                out.append(rdata.to_text())
        elif rt == dns.rdatatype.SRV:
            out.append(
                f"{rdata.priority} {rdata.weight} {rdata.port} "
                f"{str(rdata.target).rstrip('.')}"
            )
        elif rt == dns.rdatatype.CAA:
            out.append(f"{rdata.flags} {rdata.tag.decode()} {rdata.value.decode()}")
        else:
            out.append(rdata.to_text())
    return out


def _match_answers(
    answers: list[str], expected: list[str], mode: str
) -> tuple[bool, bool]:
    """Compare answers against an expected list.

    Returns ``(full_match, partial_match)`` so the caller can distinguish
    a complete hit from a partial hit (used to choose ``UP`` vs
    ``DEGRADED`` in ``any`` mode).
    """
    if not expected:
        return True, True

    norm_expected = {e.strip().lower() for e in expected}
    norm_answers = {a.strip().lower() for a in answers}

    if mode == "exact":
        return norm_answers == norm_expected, bool(norm_answers & norm_expected)
    if mode == "all":
        # every expected answer must appear at least once
        full = norm_expected.issubset(norm_answers)
        return full, bool(norm_expected & norm_answers)
    # default: "any"
    full = bool(norm_expected & norm_answers)
    return full, full


def _query_blocking(
    name: str, rdtype_str: str, server: str | None, timeout: float
) -> list[str]:
    """Synchronous resolver call, executed via ``asyncio.to_thread``.

    Raises ``dns.resolver.NXDOMAIN``, ``dns.resolver.NoAnswer``,
    ``dns.resolver.Timeout``, or ``dns.exception.DNSException`` on failure.
    """
    resolver = dns.resolver.Resolver()
    if server:
        # Replace the nameservers list rather than appending so misconfigurations
        # are obvious.
        resolver.nameservers = [server]
    resolver.lifetime = timeout
    rdtype = _normalize_rdtype(rdtype_str)
    answer = resolver.resolve(name, rdtype)
    return _extract_answers(answer)


def _query_ptr_blocking(
    address: str, server: str | None, timeout: float
) -> list[str]:
    """PTR queries want a name. Accept either an IP or a name in-addr.arpa."""
    name = address.strip()
    if not name.endswith(("in-addr.arpa", "ip6.arpa")):
        # Reverse the IP. dnspython handles v4 and v6 here.
        import dns.reversename

        name = str(dns.reversename.from_address(name))
    resolver = dns.resolver.Resolver()
    if server:
        resolver.nameservers = [server]
    resolver.lifetime = timeout
    answer = resolver.resolve(name, dns.rdatatype.PTR)
    return _extract_answers(answer)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def check_dns(service: ServiceConfig) -> CheckResult:
    """Run a DNS record check against the configured name + record type."""
    name = (service.host or service.url or "").strip()
    rdtype = (service.dns_record_type or "A").upper()
    timeout = float(service.timeout)
    server = service.dns_server or None
    expected = list(service.dns_expected or [])
    mode = (service.dns_match_mode or "any").lower()

    started = time.monotonic()
    ts = datetime.now(timezone.utc)

    if rdtype not in DNS_RECORD_TYPES:
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=0.0,
            error=f"Unsupported DNS record type: {rdtype}",
        )

    if mode not in {"any", "all", "exact"}:
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=0.0,
            error=f"Invalid dns_match_mode: {service.dns_match_mode}",
        )

    if not name:
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=0.0,
            error="No DNS name configured (set host or url)",
        )

    try:
        if rdtype == "PTR":
            answers = await asyncio.to_thread(
                _query_ptr_blocking, name, server, timeout
            )
        else:
            answers = await asyncio.to_thread(
                _query_blocking, name, rdtype, server, timeout
            )
        elapsed_ms = (time.monotonic() - started) * 1000
    except dns.resolver.NXDOMAIN:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"NXDOMAIN: {name}",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except dns.resolver.NoAnswer:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"No {rdtype} records found for {name}",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except dns.resolver.NoNameservers:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error="No nameservers available",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except dns.resolver.Timeout:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"DNS timeout after {service.timeout}s",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except dns.exception.DNSException as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"DNS error: {e}",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"DNS timeout after {service.timeout}s",
            details={"query": name, "record_type": rdtype, "server": server},
        )
    except Exception as e:  # pragma: no cover - defensive
        elapsed_ms = (time.monotonic() - started) * 1000
        return CheckResult(
            service_name=service.name,
            timestamp=ts,
            status=Status.DOWN,
            latency_ms=elapsed_ms,
            error=f"Unexpected error: {e}",
            details={"query": name, "record_type": rdtype, "server": server},
        )

    details: dict[str, Any] = {
        "query": name,
        "record_type": rdtype,
        "server": server,
        "answers": answers,
        "answer_count": len(answers),
    }

    full_match, partial_match = _match_answers(answers, expected, mode)

    if not expected:
        status = Status.UP
        error = None
    elif full_match:
        status = Status.UP
        error = None
    elif mode == "any" and partial_match:
        # "any" mode: at least one expected answer appeared, but not all → degraded
        status = Status.DEGRADED
        missing = [e for e in expected if e.lower() not in {a.lower() for a in answers}]
        error = f"Partial match: missing {missing}"
    else:
        # "all" or "exact" mode, or "any" with zero matches
        status = Status.DOWN
        error = f"No expected answer matched (got {answers})"

    return CheckResult(
        service_name=service.name,
        timestamp=ts,
        status=status,
        latency_ms=elapsed_ms,
        error=error,
        details=details,
    )