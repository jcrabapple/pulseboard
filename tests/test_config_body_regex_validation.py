"""Config validation for the ``body_regex`` field.

A mis-typed regex in the config file currently sails through config
loading and fails later — inside :func:`pulseboard.content_check.validate_body`
— with an opaque ``re.error`` during an actual check run.  It should
instead be rejected at config-load time so the operator sees a clear,
fast error message (mirroring how ``bad URLs`` / ``bad intervals`` are
already handled).

Tests written FIRST (RED) — the feature does not exist yet.
"""

from __future__ import annotations

import pytest

from pulseboard.config import ConfigError, parse_services


def _svc_entry(**overrides) -> dict:
    """A minimal HTTP service entry with body_regex validation in mind."""
    base = {
        "name": "api",
        "url": "https://example.com",
        "type": "http",
    }
    base.update(overrides)
    return base


class TestBodyRegexValidation:
    """``parse_services`` should reject an invalid ``body_regex`` at load time."""

    def test_valid_regex_is_accepted(self) -> None:
        raw = {"services": [_svc_entry(body_regex=r"\"status\"\s*:\s*\"none\"")]}
        services = parse_services(raw)
        assert services[0].body_regex == r"\"status\"\s*:\s*\"none\""

    def test_unclosed_bracket_rejected(self) -> None:
        """An unbalanced character class is an invalid regex."""
        raw = {"services": [_svc_entry(body_regex="[unclosed")]}
        with pytest.raises(ConfigError, match="body_regex"):
            parse_services(raw)

    def test_unbalanced_paren_rejected(self) -> None:
        """An unbalanced group is an invalid regex."""
        raw = {"services": [_svc_entry(body_regex="(oops")]}
        with pytest.raises(ConfigError, match="body_regex"):
            parse_services(raw)

    def test_dup_quantifier_rejected(self) -> None:
        """``**`` is not a valid quantifier."""
        raw = {"services": [_svc_entry(body_regex="a**")]}
        with pytest.raises(ConfigError, match="body_regex"):
            parse_services(raw)

    def test_error_message_includes_service_name(self) -> None:
        """The error message should name the service so users can locate it."""
        raw = {"services": [_svc_entry(name="prod-api", body_regex="[bad")]}
        with pytest.raises(ConfigError, match="prod-api"):
            parse_services(raw)

    def test_error_message_includes_pattern_snippet(self) -> None:
        """The error message should include the offending pattern for easy fixing."""
        raw = {"services": [_svc_entry(body_regex="(oops")]}
        # Match a substring of the pattern that doesn't need regex escaping.
        with pytest.raises(ConfigError, match=r"oops"):
            parse_services(raw)

    def test_empty_body_regex_is_ignored_not_validated(self) -> None:
        """An empty/whitespace ``body_regex`` means \"no check\" and should
        be accepted (matching the semantics of the other optional body fields)."""
        raw = {"services": [_svc_entry(body_regex="")]}
        services = parse_services(raw)
        assert services[0].body_regex == ""

    def test_none_body_regex_is_accepted(self) -> None:
        """Omitting ``body_regex`` entirely should pass validation."""
        raw = {"services": [_svc_entry()]}
        services = parse_services(raw)
        assert services[0].body_regex is None
