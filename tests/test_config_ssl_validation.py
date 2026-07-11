"""Config validation for the ``ssl_expiry_warning_days`` field.

A bad value for this SSL-specific field currently sails through config
loading and either silently disables the cert-expiry warning window (when
negative) or crashes with an opaque ``TypeError`` at check time (when a
non-numeric string is supplied). It should instead be rejected at
config-load time so the operator sees a clear, fast error message —
mirroring how ``interval``, ``timeout``, ``port``, ``expected_status``,
and ``body_regex`` are already handled.

Tests written FIRST (RED) — the feature does not exist yet.
"""

from __future__ import annotations

import pytest

from pulseboard.config import ConfigError, parse_services


def _svc_entry(**overrides) -> dict:
    """A minimal SSL service entry used by the validation tests."""
    base = {
        "name": "cert-check",
        "type": "ssl",
        "url": "https://example.com",
    }
    base.update(overrides)
    return base


class TestSslExpiryWarningDaysValidation:
    """``parse_services`` should reject an invalid
    ``ssl_expiry_warning_days`` at load time."""

    def test_negative_value_rejected(self) -> None:
        """A negative warning window is nonsensical: it would silently treat
        every certificate as non-expiring until after the expiry date."""
        raw = {"services": [_svc_entry(ssl_expiry_warning_days=-1)]}
        with pytest.raises(ConfigError, match="ssl_expiry_warning_days"):
            parse_services(raw)

    def test_zero_is_accepted(self) -> None:
        """A warning window of 0 days is a legitimate (if aggressive)
        configuration: it means DEGRADED is only set once the certificate
        has expired. ``check_ssl`` handles the boundary correctly, so we
        must not reject it here."""
        raw = {"services": [_svc_entry(ssl_expiry_warning_days=0)]}
        services = parse_services(raw)
        assert services[0].ssl_expiry_warning_days == 0

    def test_non_numeric_string_rejected(self) -> None:
        """A string like 'two weeks' should fail, not silently become a
        truthy value that breaks comparison in ``check_ssl``."""
        raw = {
            "services": [_svc_entry(ssl_expiry_warning_days="two weeks")]
        }
        with pytest.raises(ConfigError, match="ssl_expiry_warning_days"):
            parse_services(raw)

    def test_boolean_rejected(self) -> None:
        """A boolean must be rejected explicitly: ``True`` would otherwise
        be coerced to 1 (one day) and ``False`` to 0, both silently wrong."""
        raw = {"services": [_svc_entry(ssl_expiry_warning_days=True)]}
        with pytest.raises(ConfigError, match="ssl_expiry_warning_days"):
            parse_services(raw)

    def test_omitted_defaults_to_fourteen(self) -> None:
        """When ``ssl_expiry_warning_days`` is absent, the documented
        default of 14 days must be applied unchanged."""
        raw = {"services": [_svc_entry()]}
        services = parse_services(raw)
        assert services[0].ssl_expiry_warning_days == 14

    def test_error_message_includes_service_name(self) -> None:
        """The error message should name the service so operators can
        locate the bad entry quickly."""
        raw = {
            "services": [
                _svc_entry(name="prod-cert", ssl_expiry_warning_days=-7)
            ]
        }
        with pytest.raises(ConfigError, match="prod-cert"):
            parse_services(raw)

    def test_valid_positive_value_accepted(self) -> None:
        """A normal positive integer must pass through verbatim."""
        raw = {"services": [_svc_entry(ssl_expiry_warning_days=30)]}
        services = parse_services(raw)
        assert services[0].ssl_expiry_warning_days == 30
