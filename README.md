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
- **SQLite history** — every check stored, queryable, prunable
- **Live TUI dashboard** — Rich-powered terminal UI with latency bars, P95/P99 stats
- **Alerting** — webhook notifications + terminal bell on status changes
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

# 7. Prune old records
pulseboard prune --days 30

# 8. Check SSL certificate expiry for SSL services
pulseboard certs                    # all SSL services
pulseboard certs --days 30          # only show certs expiring within 30 days
pulseboard certs --json             # JSON output

# 9. Validate HTTP response bodies (substr/regex/jsonpath)
pulseboard validate
pulseboard validate --json
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
| `pulseboard prune` | Clean old records |
| `pulseboard config-path` | Show config file location |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

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
- [ ] Grafana/Prometheus metrics export
- [ ] Incident timeline view
- [ ] Web UI dashboard
- [ ] Notification channels (Telegram, Discord, Slack, email)
- [ ] Service groups and dependency tracking
- [ ] Configurable alert thresholds (latency, error rate)
- [ ] Export/import history (CSV, JSON)

## License

MIT
