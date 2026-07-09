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

        # Threshold validation
        sname = entry.get("name", "<unnamed>")
        lat_warn = entry.get("latency_warning_ms")
        lat_crit = entry.get("latency_critical_ms")
        if lat_warn is not None and float(lat_warn) < 0:
            raise ConfigError(
                f"Service '{sname}': latency_warning_ms must be >= 0"
            )
        if lat_crit is not None and float(lat_crit) < 0:
            raise ConfigError(
                f"Service '{sname}': latency_critical_ms must be >= 0"
            )
        if (
            lat_warn is not None
            and lat_crit is not None
            and float(lat_warn) > float(lat_crit)
        ):
            raise ConfigError(
                f"Service '{sname}': latency_warning_ms ({lat_warn}) must be "
                f"<= latency_critical_ms ({lat_crit})"
            )
        er_warn = entry.get("error_rate_warning_pct")
        er_crit = entry.get("error_rate_critical_pct")
        for label, val in (("error_rate_warning_pct", er_warn),
                           ("error_rate_critical_pct", er_crit)):
            if val is not None and not (0.0 <= float(val) <= 100.0):
                raise ConfigError(
                    f"Service '{sname}': {label} must be between 0 and 100"
                )
        if (
            er_warn is not None
            and er_crit is not None
            and float(er_warn) > float(er_crit)
        ):
            raise ConfigError(
                f"Service '{sname}': error_rate_warning_pct ({er_warn}) must "
                f"be <= error_rate_critical_pct ({er_crit})"
            )
        window = entry.get("error_rate_window")
        if window is not None and int(window) < 1:
            raise ConfigError(
                f"Service '{sname}': error_rate_window must be >= 1"
            )

        # alert_channels must be a list of strings; reject other shapes
        # loudly so the user doesn't discover the typo on the first alert.
        ac_raw = entry.get("alert_channels", [])
        if not isinstance(ac_raw, list) or not all(
            isinstance(x, str) for x in ac_raw
        ):
            raise ConfigError(
                f"Service '{sname}': alert_channels must be a list of strings"
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
            alert_channels=list(ac_raw),
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
            latency_warning_ms=(
                float(entry["latency_warning_ms"])
                if entry.get("latency_warning_ms") is not None
                else None
            ),
            latency_critical_ms=(
                float(entry["latency_critical_ms"])
                if entry.get("latency_critical_ms") is not None
                else None
            ),
            error_rate_window=entry.get("error_rate_window", 50),
            error_rate_warning_pct=(
                float(entry["error_rate_warning_pct"])
                if entry.get("error_rate_warning_pct") is not None
                else None
            ),
            error_rate_critical_pct=(
                float(entry["error_rate_critical_pct"])
                if entry.get("error_rate_critical_pct") is not None
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
        "notification_channels": [],
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
    body_contains: '"indicator"'
    # Body must NOT contain this substring (e.g. an outage banner):
    body_not_contains: '"major"'
    # Regex must match somewhere in the body:
    body_regex: '"status"\\s*:\\s*"none"'
    # Resolve a JSON path; optionally require it to equal a literal value:
    json_path: status.indicator
    json_path_expected: none
    tags: [api, status]

  # Latency & error-rate thresholds — downgrade a service when it gets slow
  # or starts failing too often, even if the HTTP request "succeeds".
  - name: Slow API
    url: https://api.example.com/health
    interval: 60
    # If latency >= 500ms, status becomes DEGRADED.
    latency_warning_ms: 500
    # If latency >= 2000ms, status becomes DOWN.
    latency_critical_ms: 2000
    tags: [api, slo]

  # Error-rate thresholds use a rolling window of recent stored checks.
  - name: Flaky Service
    url: https://flaky.example.com
    interval: 30
    error_rate_window: 50          # consider the last 50 checks
    error_rate_warning_pct: 10     # >= 10% failures -> DEGRADED
    error_rate_critical_pct: 50    # >= 50% failures -> DOWN
    tags: [slo, error-rate]

  # Notification channels (uncomment to enable). Channels are defined once
  # under settings and routed per-service via ``alert_channels:``. Without
  # that override, every channel fires for every service.
  #
  # settings:
  #   notification_channels:
  #     - name: ops-slack
  #       type: slack
  #       webhook_url: https://hooks.slack.com/services/T0/B0/XXX
  #     - name: oncall-discord
  #       type: discord
  #       webhook_url: https://discord.com/api/webhooks/1/abc
  #     - name: oncall-telegram
  #       type: telegram
  #       telegram_token: "123456:abcdef"
  #       telegram_chat_id: "-1001234567890"
  #     # Email channel -- uses stdlib smtplib, no extra dependency.
  #     - name: oncall-email
  #       type: email
  #       smtp_host: smtp.gmail.com
  #       smtp_port: 587              # defaults to 587 if omitted
  #       smtp_username: alerts@gmail.com
  #       smtp_password: app-password  # use an app password, not your real one
  #       smtp_use_tls: true           # STARTTLS (strongly recommended)
  #       smtp_from_addr: alerts@gmail.com
  #       smtp_to_addrs:
  #         - oncall@example.com
  #         - manager@example.com
  #       smtp_subject_prefix: "[Oncall]"  # defaults to "[PulseBoard]"
  #
  # Run ``pulseboard notify-test`` to verify a channel config without
  # waiting for a real outage.
"""


def init_config(path: Path | None = None) -> Path:
    """Write an example config file and return its path."""
    target = path or DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")
    target.write_text(EXAMPLE_CONFIG)
    return target
