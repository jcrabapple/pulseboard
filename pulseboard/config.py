"""Configuration loading and management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import DNS_RECORD_TYPES, ServiceConfig, ServiceType


class ConfigError(ValueError):
    """Raised when the config file contains invalid values."""

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pulseboard" / "config.yaml"
LEGACY_CONFIG_PATH = Path("pulseboard.yaml")


def find_config(path: str | Path | None = None) -> Path:
    """Locate the config file."""
    if path:
        p = Path(path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")

    for candidate in [LEGACY_CONFIG_PATH, DEFAULT_CONFIG_PATH]:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"No config found. Run 'pulseboard init' or create {DEFAULT_CONFIG_PATH}"
    )


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and parse the YAML config."""
    config_path = find_config(path)
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    return raw


def parse_services(raw: dict[str, Any]) -> list[ServiceConfig]:
    """Parse service definitions from config dict.

    Raises :class:`ConfigError` when mandatory fields for a service type are
    missing or contain invalid values.
    """
    services: list[ServiceConfig] = []
    for entry in raw.get("services", []):
        stype = ServiceType(entry.get("type", "http"))

        # DNS-specific validation
        if stype == ServiceType.DNS:
            dns_rdtype = entry.get("dns_record_type", "A").upper()
            if dns_rdtype not in DNS_RECORD_TYPES:
                raise ConfigError(
                    f"Service '{entry.get('name')}': unsupported dns_record_type "
                    f"'{dns_rdtype}'. Supported: {', '.join(DNS_RECORD_TYPES)}"
                )
            dns_match_mode = entry.get("dns_match_mode", "any").lower()
            if dns_match_mode not in {"any", "all", "exact"}:
                raise ConfigError(
                    f"Service '{entry.get('name')}': dns_match_mode must be "
                    f"'any', 'all', or 'exact', not '{dns_match_mode}'"
                )

        svc = ServiceConfig(
            name=entry["name"],
            url=entry.get("url", ""),
            service_type=stype,
            interval=entry.get("interval", 60),
            timeout=entry.get("timeout", 10),
            expected_status=entry.get("expected_status", 200),
            headers=entry.get("headers", {}),
            tags=entry.get("tags", []),
            alert_webhook=entry.get("alert_webhook"),
            host=entry.get("host"),
            port=entry.get("port"),
            ssl_expiry_warning_days=entry.get("ssl_expiry_warning_days", 14),
            ssl_sni=entry.get("ssl_sni"),
            dns_record_type=entry.get("dns_record_type", "A").upper(),
            dns_server=entry.get("dns_server"),
            dns_expected=entry.get("dns_expected"),
            dns_match_mode=entry.get("dns_match_mode", "any").lower(),
            body_contains=entry.get("body_contains"),
            body_not_contains=entry.get("body_not_contains"),
            body_regex=entry.get("body_regex"),
            json_path=entry.get("json_path"),
            json_path_expected=(
                str(entry["json_path_expected"])
                if entry.get("json_path_expected") is not None
                else None
            ),
        )
        services.append(svc)
    return services


def get_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract global settings with defaults."""
    defaults = {
        "db_path": str(Path.home() / ".local" / "share" / "pulseboard" / "pulseboard.db"),
        "check_interval": 60,
        "alert_on_recovery": True,
        "dashboard_refresh": 5,
        "history_days": 30,
    }
    defaults.update(raw.get("settings", {}))
    return defaults


EXAMPLE_CONFIG = """\
# PulseBoard configuration
# Docs: https://github.com/jcrabapple/pulseboard

settings:
  db_path: ~/.local/share/pulseboard/pulseboard.db
  check_interval: 60        # default seconds between checks
  dashboard_refresh: 5      # TUI refresh interval (seconds)
  alert_on_recovery: true
  history_days: 30          # how long to keep check history

services:
  - name: GitHub
    url: https://github.com
    interval: 120
    tags: [dev-tools]

  - name: Home Assistant
    url: http://192.168.1.100:8123/api/health
    interval: 30
    timeout: 5
    tags: [local, smart-home]

  - name: Router
    type: tcp
    host: 192.168.1.1
    port: 80
    interval: 60
    tags: [network, local]

  - name: Prose.sh Blog
    url: https://prose.sh
    interval: 300
    tags: [web, blog]

  # SSL certificate expiry monitoring
  - name: GitHub SSL
    type: ssl
    url: https://github.com
    interval: 86400  # check once a day
    ssl_expiry_warning_days: 30  # alert when cert is within 30 days of expiry

  # DNS monitoring
  - name: GitHub DNS
    type: dns
    host: github.com
    interval: 300
    dns_record_type: A
    dns_expected: ["140.82.121.3"]  # optional: verify specific answers
    dns_match_mode: any              # any | all | exact
    tags: [dns, web]

  - name: My Mail MX
    type: dns
    host: example.com
    dns_record_type: MX
    interval: 600
    tags: [dns, mail]

  # HTTP body content validation — confirm the response really means "OK"
  # even when the status code is 200. Any/all of these may be combined.
  - name: GitHub Status
    url: https://www.githubstatus.com/api/v2/status.json
    interval: 60
    # Body must contain this substring:
    body_contains: "\"indicator\""
    # Body must NOT contain this substring (e.g. an outage banner):
    body_not_contains: "\"major\""
    # Regex must match somewhere in the body:
    body_regex: '"status"\\s*:\\s*"none"'
    # Resolve a JSON path; optionally require it to equal a literal value:
    json_path: status.indicator
    json_path_expected: none
    tags: [api, status]
"""


def init_config(path: Path | None = None) -> Path:
    """Write an example config file and return its path."""
    target = path or DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")
    target.write_text(EXAMPLE_CONFIG)
    return target
