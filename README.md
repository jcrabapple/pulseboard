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
- **Alerting** — webhook notifications + terminal bell on status changes, with configurable cooldown deduplication
- **Notification channels** — Slack, Discord, Telegram, email (SMTP), and generic JSON webhooks
- **Incident timeline** — durable, queryable history of every state-change outage with duration tracking
- **Service groups** — tag services with logical groups, roll up worst-case status per group
- **Dependency tracking** — declare `depends_on` to suppress misleading "down" alerts when the real failure is upstream
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

# Validate YAML, service definitions, dependencies, and alert routing
pulseboard validate-config

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

# 11. Check SSL certificate expiry for SSL services
pulseboard certs                    # all SSL services
pulseboard certs --days 30          # only show certs expiring within 30 days
pulseboard certs --json             # JSON output

# 12. Validate HTTP response bodies (substr/regex/jsonpath)
pulseboard validate
pulseboard validate --json

# 13. Export check history (defaults to CSV on stdout)
pulseboard export
pulseboard export -o history.csv
pulseboard export -o history.json
pulseboard export -s "GitHub" --hours 24

# 14. View the incident timeline (durable across restarts)
pulseboard incidents                # last 24h of outages (default sort: newest first)
pulseboard incidents --hours 168    # last 7 days
pulseboard incidents -s "GitHub"    # filter to a service
pulseboard incidents --type down    # only DOWN incidents (skip DEGRADED)
pulseboard incidents --open         # only show ongoing outages
pulseboard incidents --summary      # aggregate counts / total downtime / MTTR
pulseboard incidents --json         # machine-readable

# 15. View service groups and the dependency graph
pulseboard groups                   # roll-up table of all groups
pulseboard groups --json            # JSON payload (groups + member lists)
pulseboard groups --graph           # print the dependency graph (topo order)
pulseboard groups --group production  # focus on one group

# 16. Export Prometheus metrics (see "Prometheus Metrics Export" below)
pulseboard metrics                              # render to stdout
pulseboard metrics -o /var/node_exporter/pulseboard.prom   # textfile mode
pulseboard metrics --serve --port 9464          # scrape endpoint
pulseboard metrics --hours 168 > pulseboard.prom
```

## Configuration

Run `pulseboard validate-config` after editing the file to catch malformed
YAML, invalid service definitions, dependency errors, duplicate notification
channel names, and unknown names in `alert_channels` without making network
requests. Use `-c path/to/config.yaml` to validate a non-default file.

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

  # Group membership — multiple groups per service, group names are free-form
  - name: API
    url: https://api.example.com
    groups: [production, backend]
    interval: 30

  # Dependency tracking — a service is downgraded when its dependency fails
  - name: Admin UI
    url: https://admin.example.com
    groups: [production, frontend]
    depends_on: [API]                          # fails when API is DOWN
    interval: 60
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
| `pulseboard incidents` | View the incident timeline (durable state-change history) |
| `pulseboard groups` | Show service-group roll-up and the dependency graph |
| `pulseboard metrics` | Export check history as Prometheus metrics (stdout, textfile, or HTTP) |
| `pulseboard prune` | Clean old records |
| `pulseboard config-path` | Show config file location |
| `pulseboard validate-config` | Validate config and notification routing without running checks |

## Incident Timeline

The `pulseboard incidents` command shows every outage that has been
recorded for your services — a durable counterpart to the in-memory
`AlertManager` that survives watcher restarts. An *incident* is any
contiguous period during which a service was not in the `UP` state.
DEGRADED↔DOWN flapping inside the same outage is folded into a single
incident so the timeline stays readable.

Incidents are written automatically by `pulseboard watch` and
`pulseboard dashboard`. The `pulseboard incidents` command then lets
you slice the timeline however you want:

```bash
# Last 24h of outages, newest first
pulseboard incidents

# Last 7 days, JSON output for piping into other tools
pulseboard incidents --hours 168 --json

# Only show ongoing outages
pulseboard incidents --open

# Filter to a single service and skip DEGRADED-only incidents
pulseboard incidents -s "api" --type down

# Aggregate stats: total downtime, MTTR, longest incident, etc.
pulseboard incidents --summary
```

The JSON output includes both the per-incident list and an aggregate
`summary` block (`total`, `open`, `closed`, `down`, `degraded`,
`total_downtime_seconds`, `average_duration_seconds`,
`longest_duration_seconds`) so a downstream dashboard or pager system
can pull MTTR without re-implementing the math.

The `incidents` table is schema-migrated automatically on first
launch. The schema also includes a partial index on
`(service_name) WHERE ended_at IS NULL` so the "what's still
broken right now" query stays fast as the history grows.

## Prometheus Metrics Export

PulseBoard can render its check history into the
[Prometheus text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/#text-format-example),
so any OpenMetrics-compatible scraper (Prometheus, VictoriaMetrics,
Thanos, Mimir, node_exporter's textfile collector) can ingest it
without an exporter sidecar. Three modes are supported:

| Mode | Command | When to use |
|------|---------|-------------|
| **stdout** | `pulseboard metrics` | Pipe to `curl --data-binary @-` against a [Pushgateway](https://github.com/prometheus/pushgateway), or capture into a file |
| **textfile** | `pulseboard metrics -o /path/to/file.prom` | Drop into node_exporter's `--collector.textfile.directory` for batch-style metrics on a cron |
| **serve** | `pulseboard metrics --serve --host 0.0.0.0 --port 9464` | Run a dedicated scrape endpoint — Prometheus scrapes it directly |

The textfile mode writes atomically (writes to `*.tmp` then `os.replace`
into place), so node_exporter never scrapes a half-written file.

### Metric families

All families carry the `pulseboard_` prefix and are labeled with
`service` (configured service name) plus `type` (resolved service type:
`http`, `tcp`, `dns`, `ssl`, or `unknown`).

| Metric | Type | Meaning |
|--------|------|---------|
| `pulseboard_up` | gauge | `1` if the most recent check was `UP`, else `0` — the classic "is it up?" signal |
| `pulseboard_status` | gauge | Numeric encoding: `1`=UP, `2`=DEGRADED, `3`=DOWN, `4`=UNKNOWN |
| `pulseboard_status_code` | gauge | Last HTTP status code observed (0 when not applicable) |
| `pulseboard_latency_seconds` | gauge | Most recent check latency, in seconds |
| `pulseboard_checks_total` | counter | Lifetime number of checks performed for this service |
| `pulseboard_incidents_total` | counter | Lifetime number of incidents opened for this service |
| `pulseboard_open_incidents` | gauge | Currently-open incidents for this service |
| `pulseboard_uptime_ratio` | gauge | Uptime fraction over the requested window (0.0–1.0) |
| `pulseboard_avg_latency_ms` / `_min_` / `_max_` / `_p95_` / `_p99_` | gauge | Aggregate latency statistics over the window |
| `pulseboard_last_check_timestamp_seconds` | gauge | Unix timestamp of the most recent check — `time() - pulseboard_last_check_timestamp_seconds` is "staleness" in seconds |
| `pulseboard_scrape_duration_seconds` | gauge | Self-metric: how long the exporter took to render |
| `pulseboard_services_exported` | gauge | Self-metric: number of services in this scrape payload |

The numeric `pulseboard_status` encoding is intentionally stable across
releases so dashboards and alerts can rely on it. `pulseboard_status == 3`
is the canonical "this service is currently down" assertion (everything
below it means the service is healthy enough to merit attention).

### `prometheus.yml` scrape config

For the serve mode, point a Prometheus scrape job at the endpoint:

```yaml
scrape_configs:
  - job_name: pulseboard
    scrape_interval: 30s
    static_configs:
      - targets: ['pulseboard.local:9464']
        labels:
          env: home
          monitor_source: pulseboard
```

For the Pushgateway (one-shot, useful in `cron` or systemd timers that
restart often):

```bash
pulseboard metrics --hours 168 \
  | curl --data-binary @- http://pushgateway:9091/metrics/job/pulseboard/instance/home
```

For node_exporter's textfile collector (zero port, just a file):

```bash
# /etc/cron.d/pulseboard-metrics — every minute
* * * * * pulseboard metrics -o /var/lib/node_exporter/textfile_collector/pulseboard.prom
```

### Quick PromQL

A handful of recipes that map well onto the metrics above:

```promql
# Fraction of services currently UP — a global SLO signal
sum(pulseboard_up) / count(pulseboard_up)

# Anything that has been DOWN for at least 5 minutes
pulseboard_status == 3
  and on(service) (time() - pulseboard_last_check_timestamp_seconds > 300)

# 99th-percentile latency, top 5 slowest services
topk(5, pulseboard_p99_latency_ms)

# Alert: any service with open incidents
pulseboard_open_incidents > 0

# Alert: any service went from UP to non-UP in the last 5 minutes
changes(pulseboard_status[5m]) > 0 and pulseboard_status != 1
```

The exporter does not maintain its own alerting — it just renders
metrics. Plug the rules above (or your own) into your existing
Alertmanager pipeline.

### Tuning

`--hours N` controls the window for the aggregate gauges (`uptime_ratio`,
`avg_latency_ms`, percentiles). The lifetime counters
(`checks_total`, `incidents_total`, `open_incidents`) are independent of
the window and always reflect total history. Default is 24 hours; pick
longer when building SLO dashboards (e.g. `--hours 720` for 30 days).

In serve mode, the binder binds to `127.0.0.1` by default — set
`--host 0.0.0.0` (or your interface IP) when exposing beyond the loopback
interface. No auth, so put it behind a reverse proxy or a firewall rule
when running on a shared network.

## Service Groups & Dependencies

For larger setups you'll want to roll up dozens of services into a
handful of logical groups (e.g. `production`, `external`, `infrastructure`)
and tell PulseBoard which services depend on which, so a database
failure doesn't generate N downstream alerts.

```yaml
services:
  - name: API
    url: https://api.example.com/health
    groups: [production, backend]
  - name: Admin UI
    url: https://admin.example.com
    groups: [production, frontend]
    depends_on: [API]                   # Admin UI requires API
  - name: Postgres
    url: https://db.example.com
    groups: [infrastructure, backend]
  - name: Redis
    url: https://redis.example.com
    groups: [infrastructure, backend]
  - name: Marketing Site
    url: https://blog.example.com
    groups: [production, frontend]
```

The `pulseboard groups` command renders a roll-up table by worst-case
status (any DOWN → group DOWN), plus a topological dependency graph:

```bash
pulseboard groups                      # table of group roll-ups
pulseboard groups --group production  # focus on one group
pulseboard groups --json               # machine-readable
pulseboard groups --graph              # topologically-sorted graph
```

Output (text mode) looks like:

```
⚡ PulseBoard — Service Groups
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ Group         ┃    Status   ┃ Counts             ┃ Members         ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ backend       │   🟢 UP     │ 0 up / 0 deg /     │ API, Postgres   │
│               │             │ 0 down / 0 ?  (2)  │                 │
├───────────────┼─────────────┼───────────────────┼─────────────────┤
│ frontend      │   🟢 UP     │ 0 up / 0 deg /     │ Admin UI,       │
│               │             │ 0 down / 0 ?  (2)  │ Marketing Site  │
├───────────────┼─────────────┼───────────────────┼─────────────────┤
│ infrastructure│   🟢 UP     │ 0 up / 0 deg /     │ Postgres, Redis │
│               │             │ 0 down / 0 ?  (2)  │                 │
├───────────────┼─────────────┼───────────────────┼─────────────────┤
│ production    │   🟢 UP     │ 0 up / 0 deg /     │ Admin UI, API,  │
│               │             │ 0 down / 0 ?  (3)  │ Marketing Site  │
└───────────────┴─────────────┴───────────────────┴─────────────────┘
```

`pulseboard groups --graph` prints one line per service in topological
order (deps first):

```
⚡ PulseBoard — Dependency Graph
  API          (no dependencies)
  Postgres     (no dependencies)
  Redis        (no dependencies)
  Admin UI  -> API
```

Dependency impact is applied *automatically* during `pulseboard watch`
and `pulseboard dashboard` (and `pulseboard check` since v0.10.0):

- If a dependency is **DOWN**, the dependent is downgraded to **DOWN**
  (even if the dependent itself appears healthy). The original status is
  preserved in `details["original_status"]` and the failing dependency
  is recorded in `details["dependency_impact"]`.
- If a dependency is **DEGRADED**, the dependent is downgraded to
  **DEGRADED** (but never to DOWN).
- A service that is already DOWN is never upgraded by a healthy
  dependency — downstream failure masks an upstream problem
  intentionally, so the user sees both.

The graph is validated for cycles at config-load time (a config like
`A depends_on B; B depends_on A` is rejected with a clear error), and
only **immediate** dependencies are evaluated — there is no transitive
cascading. This keeps alerts honest: a single broken component shows up
as itself plus its direct dependents, never as a sweeping "everything
down" event.

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
    # SMTP email channel — uses stdlib smtplib, no extra dependency.
    # Works with any SMTP relay: Gmail, Fastmail, your work Exchange, a
    # local postfix, etc.
    - name: oncall-email
      type: email
      smtp_host: smtp.gmail.com
      smtp_port: 587              # defaults to 587 (submission) if omitted
      smtp_username: alerts@gmail.com
      smtp_password: app-password  # Gmail users: use an app password
      smtp_use_tls: true           # STARTTLS — strongly recommended
      smtp_from_addr: alerts@gmail.com
      smtp_to_addrs:
        - oncall@example.com
        - manager@example.com
      smtp_subject_prefix: "[Oncall]"  # default: "[PulseBoard]"

services:
  - name: api
    url: https://api.example.com/health
    alert_channels: [ops-slack, pager-webhook]   # this service only uses 2
  - name: blog
    url: https://blog.example.com                 # no override → all 5 channels fire
```

Verify your setup with `pulseboard notify-test` (sends a synthetic DOWN
alert) and inspect what's wired up with `pulseboard notify-list`.

### Alert deduplication (cooldown)

When a service flaps — UP→DOWN→UP→DOWN repeatedly — every transition is
a legitimate state change, but you don't usually want to re-page on-call
for the same problem within a few minutes.

Set `alert_cooldown_seconds` under `settings` to suppress repeat alerts
of the same type (DOWN, DEGRADED) within a rolling window. The cooldown is
measured from the last *fired* alert of that type; suppressed alerts do
not extend the window. Recovery alerts are always sent, regardless of
cooldown — resolution is always worth surfacing. Set to `0` (the default)
to disable deduplication entirely.

```yaml
settings:
  alert_cooldown_seconds: 300   # don't re-alert the same type within 5 minutes
```

The email channel sends an RFC 5322 message with a multipart
plain/HTML body (so Gmail, Outlook, and Apple Mail all render it
well), a configurable `[Subject]` prefix, and `X-PulseBoard-*` headers
for downstream filtering. The SMTP dialogue is offloaded to a worker
thread so a slow relay never stalls the watcher loop, and STARTTLS +
plaintext auth both work — set `smtp_use_tls: false` for local relays
that don't speak TLS.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

### Unreleased
- Webhook and notification payloads now include `status` — the raw service status value (`up`, `down`, `degraded`) — so external systems don't have to reverse-engineer it from the alert type string
- Webhook and notification payloads now include `consecutive_failures` — the count of consecutive non-UP checks for the service — so external systems can escalate after N failures
- HTTP content validation now treats an empty or undecodable response body as validation input, so required substrings, regexes, and JSON paths fail instead of being silently skipped
- HTTP checks now verify HTTPS certificates against the system trust store instead of silently accepting invalid or forged certificates
- JSON check output now includes the full `details` object, exposing DNS answers, SSL certificate metadata, content-validation outcomes, threshold violations, and dependency impact to scripts and integrations
- New `pulseboard validate-config` command validates configuration without running checks or opening the history database
- Friendly non-zero errors for malformed YAML, non-mapping roots, invalid service-list shapes, duplicate notification channel names, and service routes that reference unknown notification channels
- Valid configurations report the parsed service count, making the command suitable for deployment and CI preflight checks

### v0.11.0 — Prometheus Metrics Export (2026-07-10)
- New `pulseboard metrics` CLI command with three modes: **stdout** (default), **textfile** (`-o`/atomic write for node_exporter's textfile collector), and **serve** (HTTP server exposing `/metrics`, `/`, `/healthz` on a configurable host/port)
- New `pulseboard.metrics` module with `MetricsExporter` (render the full PulseBoard history as a Prometheus text payload), `MetricSample` dataclass (single sample with HELP/TYPE rendering, label escaping per spec), `render_samples()` (aggregate samples into the canonical family-grouped text format), and `serve_metrics()` (built-in `ThreadingHTTPServer` runner with optional `max_requests` bound for tests)
- 12 stable per-service metric families — `pulseboard_up`, `pulseboard_status` (numeric encoding `1=UP, 2=DEGRADED, 3=DOWN, 4=UNKNOWN`), `pulseboard_status_code`, `pulseboard_latency_seconds`, `pulseboard_checks_total`, `pulseboard_incidents_total`, `pulseboard_open_incidents`, `pulseboard_uptime_ratio`, `pulseboard_avg_latency_ms`/`_min_`/`_max_`/`_p95_`/`_p99_`, `pulseboard_last_check_timestamp_seconds` — plus two self-metrics (`pulseboard_scrape_duration_seconds`, `pulseboard_services_exported`)
- Every per-service sample carries `service` (configured name) and `type` (resolved service type) labels — so PromQL can slice by `type="http"` / `type="ssl"` / etc.
- Scrape-config example, Pushgateway piping example, and node_exporter textfile cron example included in the README
- README "Quick PromQL" recipes for global SLO (`sum(pulseboard_up) / count(pulseboard_up)`), "down > 5 minutes" alerts, top-5 slowest by `p95_latency_ms`, and recent `changes(pulseboard_status[5m])` alerts
- The numeric `pulseboard_status` encoding is documented as a stable contract — dashboards and alerts can rely on it across releases
- `--hours N` controls the windowed aggregate gauges; lifetime counters (`checks_total`, `incidents_total`, `open_incidents`) are always total history
- Empty DB emits only the two self-metrics — no per-service lines for services that haven't been checked yet
- Textfile mode writes via `tempfile.mkstemp` + `fsync` + `os.replace`, so node_exporter never scrapes a half-written file
- `--serve` and `--output` are mutually exclusive at the CLI; bad port binding exits with a clear OSError message and a non-zero code
- Defensive fallback in `_last_result_for`, `_open_incidents_by_service`, `_lifetime_checks_by_service`, and `_lifetime_incidents_for` — if a `Storage` adapter doesn't implement a fast path, the exporter falls back to scanning the existing APIs instead of crashing
- 50 tests covering: `MetricSample` (value types, label/value escaping, HELP/TYPE preamble, sorted labels, bool→`0/1`), `render_samples` (family ordering, single HELP/TYPE per family, empty input), per-service metrics (windowed aggregates, status numeric encoding, lifetime counters, missing-result semantics, type-resolver override), `MetricsExporter` against a seeded `Storage` (no-checks, single-service full payload, open-incidents counting, lifetime counter correctness, multi-service payload, type resolver fallback), textfile mode (parent directory creation, atomic overwrite, no tmp left behind, sample-count return), HTTP server (`/metrics`, `/`, `/healthz`, unknown path 404, `max_requests` shutdown, listener-bind binding), and full CLI behavior (help lists all modes, stdout emits payload, textfile writes file in nested directory, missing config exits non-zero, empty DB emits only self-metrics)
- README roadmap checkbox for "Grafana/Prometheus metrics export" ticked — full feature is now discoverable

### v0.10.0 — Service Groups & Dependency Tracking (2026-07-09)
- New `pulseboard groups` CLI command with three modes: roll-up table (default), `--graph` (topological dependency view), and `--json`
- `--group <name>` filter focuses the output on a single group
- `pulseboard.groups` module wired into `pulseboard check`, `pulseboard watch`, and `pulseboard dashboard` — dependent services are now annotated and downgraded when their declared `depends_on` targets fail
- Dependency impact rules: DOWN dependency → dependent DOWN, DEGRADED dependency → dependent DEGRADED (max upgrade: never above UP), service already DOWN is never upgraded
- Failed dependencies recorded in `details["dependency_impact"]` (with name, status, and error); the original status is preserved in `details["original_status"]`
- Cycle detection in the dependency graph at config-load time (a config with `A depends_on B; B depends_on A` is rejected with a clear error)
- Only immediate dependencies are evaluated — no transitive cascading, so a single upstream failure surfaces as itself plus its direct dependents
- New `build_groups_table()` in `pulseboard.dashboard` renders the roll-up Rich table with status emoji + color, counts (up / degraded / down / unknown), and member list
- New `tests/test_groups.py` with 45 tests covering GroupSummary aggregation, group roll-up construction, topological sort, dependency graph description, dependency-impact annotation/downgrade (UP→DOWN, UP→DEGRADED, DOWN→DOWN preserved, DEGRADED+down→DOWN escalated), and full CLI behavior for `groups`, `--graph`, `--group`, `--json`
- Removed stray `groups.py` at the project root that predated the module being moved into `pulseboard/groups.py` (was tracked but unused)

### v0.9.0 — Incident Timeline (2026-07-09)
- New `incidents` table in SQLite: every UP→non-UP transition opens an incident row, every non-UP→UP closes it
- New `pulseboard incidents` command with rich table, JSON output, and `--summary` mode
- Filters: `--service`, `--hours`, `--from`/`--to`, `--type {down,degraded,all}`, `--open` (only ongoing), `--limit`
- New `Incident` dataclass in `pulseboard.incidents` with `severity`, `is_open`, `duration_seconds`, `peak_status`, `to_dict()`
- New `pulseboard.incidents.format_duration()` for human-friendly duration rendering (45s / 2m 5s / 1h 2m / 1d 1h)
- New `pulseboard.incidents.summarize()` produces MTTR / total-downtime / longest-incident stats for the JSON output
- New `reconstruct_incidents()` walks raw check history to recover incidents from a fresh import (no live recording needed)
- New `Storage.record_incident()`, `close_open_incident()`, `get_incidents()`, `prune_incidents()` with rich filter set
- New `AlertManager.previous_status()` exposes the prior status so the recorder knows the from-state of a transition
- DEGRADED↔DOWN flapping inside an outage is folded into a single incident (peak severity tracked separately)
- UNKNOWN status does not open an incident (treats it as a data gap, not an outage)
- Partial index `idx_incidents_open` on `(service_name) WHERE ended_at IS NULL` keeps the open-incident query fast
- New `pulseboard.dashboard.build_incident_table()` renders a Rich timeline table with live duration for open incidents
- Wired into both `pulseboard watch` and `pulseboard dashboard` loops — no extra setup, just start the watcher
- 53 new tests cover: reconstruction logic, peak-severity tracking, peak-error selection, multi-incident timelines, storage round-trip, UNKNOWN handling, open-only filtering, format_duration, summary aggregates, build_incident_table, and full CLI behavior (filters, JSON, summary, missing config, help)

### v0.8.0 — Email Notifications (SMTP) (2026-07-09)
- New `email` channel type: SMTP delivery via stdlib `smtplib` (zero new dependencies)
- New `render_email_payload()` builds a multipart `EmailMessage` (plain + HTML) with `X-PulseBoard-Alert` and `X-PulseBoard-Service` headers for downstream filtering
- New `_send_email()` async wrapper pushes the synchronous SMTP dialogue into a worker thread (`asyncio.to_thread`) so a slow relay never stalls the watcher loop
- Supports STARTTLS (default on), optional SMTP AUTH, configurable port (default 587), and a custom subject prefix
- Multiple recipients via `smtp_to_addrs: [a@x, b@y]` — validated at config-load time as a list of strings
- HTML body color-codes the status heading using the same palette as Slack/Discord
- HTML escaping on the title and description to neutralize XSS-via-service-name
- Per-service `alert_channels: [oncall-email]` routing works exactly like the existing HTTP backends
- `pulseboard notify-test` and `pulseboard notify-list` work out of the box for email channels (no CLI changes needed)
- 36 new tests cover payload rendering, validation, dispatcher routing, SMTP interaction, failure modes, and concurrent fan-out — no live network required

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
- [x] Notification channels (Slack, Discord, Telegram, email, generic webhook)
- [x] Incident timeline view
- [x] Service groups and dependency tracking
- [x] Grafana/Prometheus metrics export
- [ ] Web UI dashboard

## License

MIT
