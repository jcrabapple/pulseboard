"""Tests for PulseBoard configuration validation."""

import pytest

from pulseboard.config import ConfigError, parse_services


def test_parse_services_rejects_duplicate_names() -> None:
    config = {
        "services": [
            {"name": "API", "url": "https://api.example.com"},
            {"name": "API", "url": "https://backup.example.com"},
        ]
    }

    with pytest.raises(ConfigError, match="Duplicate service name 'API'"):
        parse_services(config)
