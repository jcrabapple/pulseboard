"""Configuration loading and management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import ServiceConfig, ServiceType

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
    """Parse service definitions from config dict."""
    services = []
    for entry in raw.get("services", []):
        stype = ServiceType(entry.get("type", "http"))
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
"""


def init_config(path: Path | None = None) -> Path:
    """Write an example config file and return its path."""
    target = path or DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")
    target.write_text(EXAMPLE_CONFIG)
    return target
