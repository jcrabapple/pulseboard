"""Tests for PulseBoard configuration validation."""

import pytest
from click.testing import CliRunner

from pulseboard.cli import cli
from pulseboard.config import ConfigError, parse_services


def test_validate_config_reports_valid_service_count(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text(
        "services:\n"
        "  - name: API\n"
        "    url: https://api.example.com/health\n"
    )

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert "Config valid" in result.output
    assert "1 service" in result.output


def test_validate_config_reports_invalid_yaml_without_traceback(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text("services: [\n")

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 1
    assert "Invalid YAML" in result.output
    assert "Traceback" not in result.output


def test_validate_config_rejects_non_mapping_root(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text("- services\n")

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 1
    assert "top level must be a mapping" in result.output
    assert "Traceback" not in result.output


def test_validate_config_rejects_non_list_services(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text("services: API\n")

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 1
    assert "services must be a list" in result.output
    assert "Traceback" not in result.output


def test_validate_config_rejects_unknown_alert_channel(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text(
        "settings:\n"
        "  notification_channels:\n"
        "    - name: ops\n"
        "      type: webhook\n"
        "      webhook_url: https://alerts.example.com/hook\n"
        "services:\n"
        "  - name: API\n"
        "    url: https://api.example.com/health\n"
        "    alert_channels: [missing]\n"
    )

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 1
    assert "unknown notification" in result.output
    assert "channel 'missing'" in result.output


def test_validate_config_rejects_duplicate_notification_channel_names(tmp_path) -> None:
    config = tmp_path / "pulseboard.yaml"
    config.write_text(
        "settings:\n"
        "  notification_channels:\n"
        "    - name: ops\n"
        "      type: webhook\n"
        "      webhook_url: https://alerts.example.com/one\n"
        "    - name: ops\n"
        "      type: webhook\n"
        "      webhook_url: https://alerts.example.com/two\n"
        "services: []\n"
    )

    result = CliRunner().invoke(cli, ["validate-config", "-c", str(config)])

    assert result.exit_code == 1
    assert "Duplicate notification channel name 'ops'" in result.output


def test_parse_services_rejects_duplicate_names() -> None:
    config = {
        "services": [
            {"name": "API", "url": "https://api.example.com"},
            {"name": "API", "url": "https://backup.example.com"},
        ]
    }

    with pytest.raises(ConfigError, match="Duplicate service name 'API'"):
        parse_services(config)


# ---------------------------------------------------------------------------
# Fail-fast validation: bad URLs (no scheme or non-http scheme)
# ---------------------------------------------------------------------------


def test_parse_services_rejects_http_service_url_without_scheme() -> None:
    """An HTTP service URL missing a http:// or https:// scheme must fail."""
    config = {"services": [{"name": "API", "url": "api.example.com/health"}]}
    with pytest.raises(ConfigError, match="url must start with http:// or https://"):
        parse_services(config)


def test_parse_services_rejects_http_service_url_with_ftp_scheme() -> None:
    """A non-http scheme like ftp:// must be rejected for http services."""
    config = {"services": [{"name": "FTP", "url": "ftp://files.example.com"}]}
    with pytest.raises(ConfigError, match="url must start with http:// or https://"):
        parse_services(config)


def test_parse_services_rejects_http_service_with_empty_url() -> None:
    """An empty URL string must fail at config-load time."""
    config = {"services": [{"name": "Empty", "url": ""}]}
    with pytest.raises(ConfigError, match="url must start with http:// or https://"):
        parse_services(config)


# ---------------------------------------------------------------------------
# Fail-fast validation: bad intervals
# ---------------------------------------------------------------------------


def test_parse_services_rejects_interval_less_than_one_second() -> None:
    """An interval of 0 is not a valid check schedule."""
    config = {
        "services": [
            {"name": "Fast", "url": "https://api.example.com", "interval": 0}
        ]
    }
    with pytest.raises(ConfigError, match="interval must be >= 1"):
        parse_services(config)


def test_parse_services_rejects_negative_interval() -> None:
    """A negative interval must be rejected."""
    config = {
        "services": [
            {"name": "Neg", "url": "https://api.example.com", "interval": -10}
        ]
    }
    with pytest.raises(ConfigError, match="interval must be >= 1"):
        parse_services(config)
