"""HTTP response body content validation.

A standalone module so the validation logic is testable without going through
httpx. The HTTP checker calls :func:`validate_body` after a successful
response and merges the resulting :class:`ValidationReport` into the
:class:`CheckResult` it returns.

Four checks, all optional, all composable:

- ``body_contains``     — case-sensitive substring must appear in the body
- ``body_not_contains`` — substring must NOT appear (e.g. "Error", "stack trace")
- ``body_regex``        — regex pattern must match somewhere in the body
- ``json_path``         — dot path into a JSON body; resolves to a scalar value
                          and (if ``json_path_expected`` is set) must equal it

Status semantics:

- Returns ``UP`` when all configured checks pass (or none configured)
- Returns ``DEGRADED`` when one or more checks fail (the service is still
  reachable and returning the expected status code, but its body indicates a
  problem — e.g. an API returning 200 OK with ``{"status": "degraded"}``)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import ServiceConfig


# Sentinel for "json_path is set but doesn't resolve" — distinct from None
# which means "json_path wasn't configured at all".
_MISSING = object()


@dataclass
class ValidationReport:
    """Outcome of running all configured body validations against a response.

    ``checks`` lists per-check outcome strings (one per configured check) so
    dashboards and JSON output can show *what* failed, not just that something
    did.
    """

    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return "up" if self.passed else "degraded"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def has_content_checks(service: ServiceConfig) -> bool:
    """True if the service has at least one body validation configured."""
    return any(
        getattr(service, attr, None)
        for attr in (
            "body_contains",
            "body_not_contains",
            "body_regex",
            "json_path",
        )
    )


def validate_body(
    service: ServiceConfig, body: str, content_type: str | None = None
) -> ValidationReport:
    """Run all configured validations against ``body``.

    ``content_type`` is informational — it controls whether json_path is
    attempted (we still try parsing if it's missing rather than silently
    skipping, because most APIs forget to send the right header).
    """
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    # 1. body_contains
    if service.body_contains:
        ok = service.body_contains in body
        checks.append(
            {
                "check": "body_contains",
                "expected": service.body_contains,
                "passed": ok,
            }
        )
        if not ok:
            failures.append(f"body missing required substring: {service.body_contains!r}")

    # 2. body_not_contains
    if service.body_not_contains:
        ok = service.body_not_contains not in body
        checks.append(
            {
                "check": "body_not_contains",
                "expected": service.body_not_contains,
                "passed": ok,
            }
        )
        if not ok:
            failures.append(
                f"body contains forbidden substring: {service.body_not_contains!r}"
            )

    # 3. body_regex
    if service.body_regex:
        try:
            pattern = re.compile(service.body_regex)
        except re.error as e:
            checks.append(
                {
                    "check": "body_regex",
                    "expected": service.body_regex,
                    "passed": False,
                    "error": f"invalid regex: {e}",
                }
            )
            failures.append(f"invalid regex {service.body_regex!r}: {e}")
        else:
            match = pattern.search(body)
            ok = match is not None
            entry: dict[str, Any] = {
                "check": "body_regex",
                "expected": service.body_regex,
                "passed": ok,
            }
            if match:
                entry["matched"] = match.group(0)
            checks.append(entry)
            if not ok:
                failures.append(f"regex did not match: {service.body_regex!r}")

    # 4. json_path (+ optional expected value match)
    if service.json_path:
        value = _resolve_json_path(body, service.json_path)
        ok_present = value is not _MISSING
        entry = {
            "check": "json_path",
            "path": service.json_path,
            "passed": ok_present,
        }
        if ok_present:
            entry["value"] = value
        else:
            entry["error"] = "path did not resolve"

        # If an expected value is configured, also enforce equality
        if ok_present and service.json_path_expected is not None:
            eq_ok = _values_equal(value, service.json_path_expected)
            entry["expected"] = service.json_path_expected
            entry["value_matches_expected"] = eq_ok
            if not eq_ok:
                ok_present = False
                entry["passed"] = False
                entry["error"] = (
                    f"value {value!r} does not match expected "
                    f"{service.json_path_expected!r}"
                )
        checks.append(entry)
        if not ok_present:
            failures.append(
                f"json_path {service.json_path!r} check failed"
            )

    return ValidationReport(
        passed=len(failures) == 0,
        checks=checks,
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _values_equal(actual: Any, expected: str) -> bool:
    """Compare a JSON-resolved value to a literal expected string.

    We coerce both sides to strings for comparison so that users can write
    ``json_path_expected: "200"`` and match against a JSON int ``200``.
    Booleans and None are matched as their string forms ("true"/"false"/"null").
    """
    if actual is None:
        return expected.lower() in ("null", "none", "")
    if isinstance(actual, bool):
        return str(actual).lower() == expected.lower()
    return str(actual) == expected


def _resolve_json_path(body: str, path: str) -> Any:
    """Resolve a dot path like ``"data.user.id"`` into a value from JSON ``body``.

    Returns the ``_MISSING`` sentinel when the body isn't JSON, when parsing
    fails, or when any path segment can't be traversed. Index segments like
    ``items.0.name`` are supported (digits are treated as list indices).
    """
    if not body or not body.strip():
        return _MISSING
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return _MISSING

    if not path:
        return data

    cur: Any = data
    for raw_segment in path.split("."):
        if raw_segment == "":
            return _MISSING
        if isinstance(cur, dict):
            if raw_segment not in cur:
                return _MISSING
            cur = cur[raw_segment]
        elif isinstance(cur, list):
            try:
                idx = int(raw_segment)
            except ValueError:
                return _MISSING
            if idx < 0 or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur