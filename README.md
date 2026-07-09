# ⚡ PulseBoard

A personal uptime monitor and service dashboard CLI. Track any URL or endpoint, store history in SQLite, get alerts when things go down, and see a beautiful TUI dashboard.

```
⚡ PulseBoard — Service Status

┌ Status ┬────────── Service ──────────┬ Uptime ┬──────────── Latency ────────────┬ Checks ┬── Last Check ──┐
│   🟢   │ GitHub                      │  99.8% │ ████░░░░░░░░░░░░░░░░ 142ms      │    120 │ 14:32:01       │
│   🟢   │ Home Assistant              │ 100.0% │ ██░░░░░░░░░░░░░░░░░░  23ms      │    240 │ 14:32:00       │
│   🔴   │ Router                      │  87.3% │ ░░░░░░░░░░░░░░░░░░░░ timeout   │    120 │ 14:31:58       │
│   🟢   │ Prose.sh Blog               │  99.9% │ ██████░░░░░░░░░░░░░░ 198ms      │     24 │ 14:30:00       │
└────────┴─────────────────────────────┴────────┴──────────────────────────────────┴────────┴────────────────┘
```

## Features

- **HTTP & TCP monitoring** — check any URL or TCP port
- **SSL certificate expiry monitoring** — track cert validity with configurable warning window
- **DNS monitoring** — resolve any record type (A, AAAA, MX, TXT, …) with optional answer validation
- **HTTP body content validation** — substring, regex, and JSON-path checks on response bodies
- **Latency & error-rate thresholds** — per-service SLOs that downgrade UP→DEGRADED or UP→DOWN
- **SQLite history** — every check stored, queryable, prunable, and exportable
- **CSV/JSON export** — pipe-friendly history export with rich filters
- **Live TUI dashboard** — Rich-powered terminal UI with latency bars, P95/P99 stats
- **Alerting** — webhook notifications + terminal bell on status changes
- **Notification channels** — Slack, Discord, Telegram, and generic JSON webhooks
- **YAML config** — simple, human-readable service definitions
- **Percentile latency** — P95, P99, min, max tracked per service
- **Auto-pruning** — configurable history retention

## Install

```bash
pip install -e .
# or
pip install pulseboard  # (once published)
```

## Quick Start

```bash
# 1. Create a config file
pulseboard init

# 2. Edit ~/.config/pulseboard/config.yaml with your services

# 3. Run a one-time check
pulseboard check

# 4. Watch continuously (stores history)
pulseboard watch

# 5. Launch the TUI dashboard
pulseboard dashboard

# 6. View status from history
pulseboard status --hours 24

# 8. Prune old records
pulseboard prune --days 30

# 9. Check SSL certificate expiry for SSL services
pulseboard certs                    # all SSL services
pulseboard certs --days 30          # only show certs expiring within 30 days
pulseboard certs --json             # JSON output

# 10. Validate HTTP response bodies (substr/regex/jsonpath)
pulseboard validate
pulseboard validate --json

# 11. Export check history (defaults to CSV on stdout)
pulseboard export
pulseboard export -o history.csv
pulseboard export -o history.json
pulseboard export -s "GitHub" --hours 24
```

## Configuration

```yaml
settings:
  db_path: ~/.local/share/pulseboard/pulseboard.db
  check_interval: 60
  dashboard_refresh: 5
  alert_on_recovery: true
  history_days: 30

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
    tags: [network]

  - name: GitHub SSL
    type: ssl
    url: https://github.com
    interval: 86400
    ssl_expiry_warning_days: 30

  # HTTP body content validation — confirm the response really means OK
  - name: GitHub Status
    url: https://www.githubstatus.com/api/v2/status.json
    interval: 60
    body_contains: "\"indicator\""
    body_not_contains: "\"major\""
    json_path: status.indicator
    json_path_expected: none
```

### HTTP Body Content Validation

For HTTP services, optional body checks run after the status code is
confirmed. Any combination of the following may be set on a service:

| Field | Type | Meaning |
|-------|------|---------|
| `body_contains` | string | Substring that must appear in the response body |
| `body_not_contains` | string | Substring that must NOT appear (e.g. error markers) |
| `body_regex` | string | Regex pattern that must match somewhere in the body |
| `json_path` | string | Dot path into a JSON body, e.g. `status.indicator` or `data.0.id` |
| `json_path_expected` | string | If set, the resolved JSON value must equal this literal |

When any check fails, a service that would otherwise be UP is downgraded to
DEGRADED — the HTTP request succeeded, but the body indicates a problem
(useful for catching "200 OK with `{"status": "down"}`" APIs).

Use the `pulseboard validate` command to run only the HTTP services that
have content checks configured.

### Latency & Error-Rate Thresholds

Sometimes the HTTP request returns 200 and the body is fine, but the
service is *still* unhealthy — it's slow, or it's been flaking out for
the last hour. Per-service thresholds let you capture that.

| Field | Type | Meaning |
|-------|------|---------|
| `latency_warning_ms` | number | When current latency ≥ this, downgrade UP → DEGRADED |
| `latency_critical_ms` | number | When current latency ≥ this, downgrade UP → DOWN |
| `error_rate_window` | int | How many recent checks to use for the error-rate check (default 50) |
| `error_rate_warning_pct` | number (0-100) | When the failure rate in the window ≥ this, downgrade UP → DEGRADED |
| `error_rate_critical_pct` | number (0-100) | When the failure rate in the window ≥ this, downgrade UP → DOWN |

```yaml
- name: Slow API
  url: https://api.example.com/health
  interval: 60
  latency_warning_ms: 500
  latency_critical_ms: 2000

- name: Flaky Service
  url: https://flaky.example.com
  interval: 30
  error_rate_window: 50
  error_rate_warning_pct: 10
  error_rate_critical_pct: 50
```

Thresholds are applied by the `watch` and `dashboard` loops after each
check. The structured outcome (which threshold fired, the measured error
rate, the sample size) is attached to `CheckResult.details["thresholds"]`
so dashboards, alerts, and exports can surface *why* a status changed.
A DOWN status from the underlying check is never upgraded by thresholds.

## Commands

| Command | Description |
|---------|-------------|
| `pulseboard init` | Create example config file |
| `pulseboard check` | One-time health check |
| `pulseboard watch` | Continuous monitoring with live output |
| `pulseboard dashboard` | Full TUI dashboard |
| `pulseboard status` | Status summary from history |
| `pulseboard certs` | Check SSL certificate expiry |
| `pulseboard dns` | Run DNS queries for configured services |
| `pulseboard validate` | Run HTTP checks and report body content validation |
| `pulseboard export` | Export stored check history to CSV or JSON |
| `pulseboard notify-test` | Send a synthetic alert through every configured notification channel |
| `pulseboard notify-list` | List configured notification channels |
| `pulseboard prune` | Clean old records |
| `pulseboard config-path` | Show config file location |

## Notification Channels

PulseBoard can fan alerts out to multiple backends in parallel. Define channels
under `settings.notification_channels` in your config; each service can opt
into a subset of those channels via `alert_channels:`, or fall back to the
global set if it has none.

```yaml
settings:
  notification_channels:
    - name: ops-slack
      type: slack
      webhook_url: https://hooks.slack.com/services/T0/B0/XXX
    - name: oncall-discord
      type: discord
      webhook_url: https://discord.com/api/webhooks/1/abc
    - name: oncall-tg
      type: telegram
      telegram_token: "123456:abcdef"
      telegram_chat_id: "-1001234567890"
    - name: pager-webhook
      type: webhook
      webhook_url: https://pagerduty.example/incoming/abc

services:
  - name: api
    url: https://api.example.com/health
    alert_channels: [ops-slack, pager-webhook]   # this service only uses 2
  - name: blog
    url: https://blog.example.com                 # no override → all 4 channels fire
```

Verify your setup with `pulseboard notify-test` (sends a synthetic DOWN
alert) and inspect what's wired up with `pulseboard notify-list`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

### v0.7.0 — Notification Channels (2026-07-09)
- New `pulseboard.notifications` module with `NotificationDispatcher` for fan-out to multiple channels in parallel
- New channel types: Slack (attachments + color), Discord (embeds + fields), Telegram (Bot API + Markdown), and generic JSON webhook
- New `ChannelType` enum and `NotificationChannel` dataclass in `pulseboard.models` with up-front validation
- Per-service `alert_channels:` list routes alerts for a service to a subset of the global channels
- Legacy `alert_webhook` field continues to work — a webhook channel is synthesized on the fly
- New `pulseboard notify-test` command sends a synthetic alert through all (or one) channels, with rich table and JSON output
- New `pulseboard notify-list` command shows configured channels (webhook URLs masked in human output)
- 36 notification tests cover payload renderers, dispatcher routing, async dispatch, and error handling (no live network — uses `httpx.MockTransport`)

### v0.6.0 — Latency & Error-Rate Thresholds (2026-07-09)
- Per-service thresholds: `latency_warning_ms`, `latency_critical_ms`, `error_rate_warning_pct`, `error_rate_critical_pct`
- New `pulseboard.thresholds` module with `evaluate_thresholds()` and `compute_error_rate()` helpers
- New `monitor.run_check_with_thresholds()` and `run_all_checks_with_thresholds()` apply thresholds after each check
- `pulseboard watch` and `pulseboard dashboard` loops now apply thresholds using a rolling window of stored history
- Threshold outcome (which fired, measured error rate, sample size) attached to `CheckResult.details["thresholds"]`
- DOWN status from the underlying check is never upgraded by a threshold
- Config validation: warning ≤ critical, error-rate bounds 0-100, window ≥ 1
- Worked examples in `pulseboard init` config and README

### v0.5.0 — History Export (CSV / JSON) (2026-07-09)
- New `pulseboard export` command with rich filter set: service, hours, ISO since/until, limit
- Defaults to CSV on stdout (pipe-friendly); file extension selects format, or pass `--format`
- New `Storage.get_history()` and `Storage.get_all_service_names()` for flexible historical queries
- New `pulseboard.export` module with `write_export`, `write_export_stream`, `infer_format`
- 44 export tests covering formats, filters, CLI behavior, and error paths

### v0.4.0 — HTTP Body Content Validation (2026-07-09)
- New optional body checks on HTTP services: `body_contains`, `body_not_contains`, `body_regex`, `json_path`, `json_path_expected`
- `pulseboard validate` command runs only HTTP services with content checks, rich table + JSON output
- Failed body checks downgrade UP → DEGRADED (HTTP request succeeded but body indicates a problem)
- Per-check outcome captured in `CheckResult.details["content_checks"]` for dashboards and history
- Zero new dependencies — uses Python stdlib `re` and `json`
- New `pulseboard.content_check` module with standalone `validate_body()` helper
- Worked example in `pulseboard init` config and README

### v0.3.0 — DNS Monitoring (2026-07-09)
- New `dns` service type for DNS record resolution monitoring
- `pulseboard dns` command with rich table + JSON output
- Supports record types: A, AAAA, CNAME, MX, NS, TXT, SRV, CAA, PTR
- Expected-answer validation with three match modes: `any`, `all`, `exact`
- Optional `dns_server` override (custom nameserver)
- Status semantics: UP (resolves) / DEGRADED (partial expected match) / DOWN (failure or mismatch)
- Per-service `timeout` control for DNS queries
- Config validation: unsupported record types and match modes caught at load time
- Added `dnspython>=2.6` dependency

### v0.2.0 — SSL Certificate Monitoring (2026-07-09)
- New `ssl` service type for certificate expiry monitoring
- `pulseboard certs` command with rich table + JSON output
- Per-service `ssl_expiry_warning_days` config (default 14)
- Optional `ssl_sni` override for virtual-hosted TLS
- Status semantics: UP / DEGRADED (within warning) / DOWN (expired or unreachable)
- Real cert info captured: issuer, subject, expiry, serial, signature algorithm

### v0.1.0 — Initial Release (2026-07-09)
- HTTP and TCP health checking
- SQLite storage with WAL mode
- Rich-powered TUI dashboard with latency bars
- Webhook and terminal alerting with state change detection
- YAML configuration with service tags
- Latency percentile tracking (P95, P99)
- History pruning
- CLI with click

## Roadmap

- [x] SSL certificate expiry checks
- [x] DNS monitoring
- [x] Response body content validation (regex/JSON path)
- [x] Configurable alert thresholds (latency, error rate)
- [x] Export/import history (CSV, JSON)
- [x] Notification channels (Slack, Discord, Telegram, generic webhook)
- [ ] Email notifications (SMTP)
- [ ] Grafana/Prometheus metrics export
- [ ] Incident timeline view
- [ ] Web UI dashboard
- [ ] Service groups and dependency tracking

## License

MIT
