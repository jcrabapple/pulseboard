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
```

## Commands

| Command | Description |
|---------|-------------|
| `pulseboard init` | Create example config file |
| `pulseboard check` | One-time health check |
| `pulseboard watch` | Continuous monitoring with live output |
| `pulseboard dashboard` | Full TUI dashboard |
| `pulseboard status` | Status summary from history |
| `pulseboard certs` | Check SSL certificate expiry |
| `pulseboard prune` | Clean old records |
| `pulseboard config-path` | Show config file location |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

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
- [ ] DNS monitoring
- [ ] Grafana/Prometheus metrics export
- [ ] Response body content validation (regex/JSON path)
- [ ] Incident timeline view
- [ ] Web UI dashboard
- [ ] Notification channels (Telegram, Discord, Slack, email)
- [ ] Service groups and dependency tracking
- [ ] Configurable alert thresholds (latency, error rate)
- [ ] Export/import history (CSV, JSON)

## License

MIT
