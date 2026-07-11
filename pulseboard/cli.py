"""CLI entry point for PulseBoard."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.live import Live

from . import __version__
from .alerting import Alert, AlertManager, AlertType, terminal_alert
from .notifications import NotificationDispatcher
from .config import (
    ConfigError,
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG,
    find_config,
    get_settings,
    init_config,
    load_config,
    parse_services,
)
from .dashboard import (
    build_cert_table,
    build_content_validation_table,
    build_dns_table,
    build_groups_table,
    build_overview_table,
    console,
    print_status_line,
    render_dashboard,
)
from .backoff import RateLimitBackoff, synthesize_backoff_result
from .monitor import run_all_checks, run_all_checks_with_thresholds, run_check
from .models import ServiceType, Status
from .storage import Storage
from .content_check import has_content_checks
from .export import infer_format, write_export, write_export_stream
from .metrics import MetricsExporter, serve_metrics
from .groups import (
    apply_dependency_impact,
    build_group_summaries,
    describe_dependency_graph,
    get_dependency_graph,
    list_services_in_group,
    topological_sort,
)


@click.group()
@click.version_option(__version__, prog_name="pulseboard")
def cli() -> None:
    """⚡ PulseBoard — Personal uptime monitor and service dashboard."""


@cli.command()
@click.option("--path", "-p", type=click.Path(), default=None, help="Config file path")
def init(path: str | None) -> None:
    """Create an example configuration file."""
    try:
        target = Path(path) if path else None
        result = init_config(target)
        console.print(f"[green]✓[/green] Config created at {result}")
        console.print("  Edit it with your services, then run: [bold]pulseboard check[/bold]")
    except FileExistsError as e:
        console.print(f"[yellow]⚠[/yellow] {e}")
        sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--json", "-j", "as_json", is_flag=True, help="Output as JSON")
@click.option("--timeout", "timeout_override", type=int, default=None,
              help="Override the per-service timeout (seconds) for this run. "
                   "Useful for ad-hoc debugging when the config-level timeout is too aggressive.")
def check(config: str | None, as_json: bool, timeout_override: int | None) -> None:
    """Run a one-time health check against all configured services."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    services = parse_services(cfg)
    if not services:
        console.print("[yellow]No services configured.[/yellow]")
        sys.exit(0)

    if timeout_override is not None and timeout_override < 1:
        console.print(
            f"[red]✗[/red] --timeout must be >= 1 second, got {timeout_override}"
        )
        sys.exit(2)

    if timeout_override is not None:
        for svc in services:
            svc.timeout = timeout_override

    # Use the threshold-aware runner so dependency-impact is applied.
    # History is empty here, so error-rate thresholds are no-ops — but
    # the dependency graph is still honored (a DOWN dependency downgrades
    # its dependents, even on a one-shot check).
    results = asyncio.run(run_all_checks_with_thresholds(services))

    if as_json:
        import json
        click.echo(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print_status_line(results)
        down = [r for r in results if not r.is_up]
        if down:
            console.print(f"\n[red]{len(down)} service(s) unreachable[/red]")
            sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--once", is_flag=True, help="Run once and exit (no loop)")
def watch(config: str | None, once: bool) -> None:
    """Continuously monitor services with live terminal output."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    services = parse_services(cfg)
    storage = Storage(settings["db_path"])
    alerter = AlertManager(
        alert_on_recovery=settings.get("alert_on_recovery", True),
        alert_cooldown_seconds=settings.get("alert_cooldown_seconds", 0.0),
        re_alert_every_n_failures=settings.get("re_alert_every_n_failures", 0),
    )
    notifier = NotificationDispatcher.from_config(
        settings.get("notification_channels", [])
    )
    backoff = RateLimitBackoff()

    console.print(f"[bold cyan]⚡ PulseBoard[/bold cyan] monitoring {len(services)} services (Ctrl+C to stop)\n")

    try:
        while True:
            # History provider for error-rate thresholds — exclude the
            # in-flight batch so we don't double-count.
            def history_provider(
                name: str, _pending: list[str] | None = None
            ) -> list[CheckResult]:
                return storage.get_recent(name, limit=200)

            # Partition services: those in an active 429 backoff window
            # are skipped (no HTTP request) and given a synthetic result.
            to_check, to_skip = backoff.filter_active(services)
            checked_results = asyncio.run(
                run_all_checks_with_thresholds(to_check, history_provider)
            )
            skipped_results = [
                synthesize_backoff_result(name, remaining)
                for name, remaining in to_skip
            ]
            results = checked_results + skipped_results
            # Keep results in service order so output is stable.
            svc_order = {s.name: i for i, s in enumerate(services)}
            results.sort(key=lambda r: svc_order.get(r.service_name, 9999))

            # Feed results back into the backoff tracker so it updates
            # its per-service windows from any new 429 responses.
            for r in checked_results:
                backoff.observe(r)

            storage.store_many(results)

            # Auto-prune old history so the DB doesn't grow unbounded.
            # history_days=0 disables pruning entirely.
            history_days = settings.get("history_days", 30)
            if history_days and history_days > 0:
                storage.auto_prune(history_days=history_days)

            # Evaluate alerts
            for r in results:
                prev = alerter.previous_status(r.service_name)
                alert = alerter.evaluate(r)
                if alert:
                    terminal_alert(alert)
                    svc = next(
                        (s for s in services if s.name == r.service_name), None
                    )
                    notifier.dispatch_sync(alert, svc)
                    # Persist the fired alert so it can be queried later
                    # via ``pulseboard alerts`` — a durable audit trail
                    # of what was sent, when, and for which service.
                    storage.record_alert(alert)
                # Persist incident timeline state (durable) regardless of
                # whether an alert fires. The helper is a no-op when the
                # service stays in the same state, so this is cheap.
                from .incidents import _record_state_change
                _record_state_change(storage, prev, r)

            # Print status line
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            console.print(f"[dim]{now}[/dim] ", end="")
            print_status_line(results)

            if once:
                break

            time.sleep(settings["check_interval"])

    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        storage.close()


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--hours", "-h", default=24, help="History window in hours")
def status(config: str | None, hours: int) -> None:
    """Show service status summary from stored history."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    storage = Storage(settings["db_path"])
    summaries = storage.get_all_summaries(hours=hours)

    if not summaries:
        console.print("[yellow]No check history found. Run 'pulseboard watch' first.[/yellow]")
        sys.exit(0)

    table = build_overview_table(summaries)
    console.print(table)
    storage.close()


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
def dashboard(config: str | None) -> None:
    """Launch the full TUI dashboard."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    services = parse_services(cfg)
    storage = Storage(settings["db_path"])
    alerter = AlertManager(
        alert_on_recovery=settings.get("alert_on_recovery", True),
        alert_cooldown_seconds=settings.get("alert_cooldown_seconds", 0.0),
        re_alert_every_n_failures=settings.get("re_alert_every_n_failures", 0),
    )
    notifier = NotificationDispatcher.from_config(
        settings.get("notification_channels", [])
    )
    backoff = RateLimitBackoff()
    refresh = settings.get("dashboard_refresh", 5)

    console.print("[bold cyan]⚡ PulseBoard Dashboard[/bold cyan] (Ctrl+C to exit)\n")

    try:
        with Live(render_dashboard([], []), refresh_per_second=1, console=console) as live:
            while True:
                # Run checks
                def history_provider(name: str) -> list[CheckResult]:
                    return storage.get_recent(name, limit=200)

                # Partition services: those in an active 429 backoff window
                # are skipped and given a synthetic result.
                to_check, to_skip = backoff.filter_active(services)
                checked_results = asyncio.run(
                    run_all_checks_with_thresholds(to_check, history_provider)
                )
                skipped_results = [
                    synthesize_backoff_result(name, remaining)
                    for name, remaining in to_skip
                ]
                results = checked_results + skipped_results
                svc_order = {s.name: i for i, s in enumerate(services)}
                results.sort(key=lambda r: svc_order.get(r.service_name, 9999))

                for r in checked_results:
                    backoff.observe(r)

                storage.store_many(results)

                for r in results:
                    prev = alerter.previous_status(r.service_name)
                    alert = alerter.evaluate(r)
                    if alert:
                        terminal_alert(alert)
                        svc = next(
                            (s for s in services if s.name == r.service_name), None
                        )
                        notifier.dispatch_sync(alert, svc)
                        storage.record_alert(alert)
                    from .incidents import _record_state_change
                    _record_state_change(storage, prev, r)

                # Get summaries and recent
                summaries = storage.get_all_summaries(hours=24)
                all_recent = []
                for svc in services:
                    all_recent.extend(storage.get_recent(svc.name, limit=10))
                all_recent.sort(key=lambda r: r.timestamp, reverse=True)

                live.update(render_dashboard(summaries, all_recent))
                time.sleep(refresh)

    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard closed.[/dim]")
    finally:
        storage.close()


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--days", "-d", default=30, help="Keep history older than N days")
def prune(config: str | None, days: int) -> None:
    """Prune old check history from the database."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    storage = Storage(settings["db_path"])
    deleted = storage.prune(days=days)
    console.print(f"[green]✓[/green] Pruned {deleted} records older than {days} days")
    storage.close()


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--service", "-s", default=None,
              help="Show only incidents for this service name.")
@click.option("--hours", default=None, type=int,
              help="Only show incidents that started in the last N hours.")
@click.option("--from", "since_iso", default=None,
              help="Only show incidents at or after this ISO timestamp (UTC).")
@click.option("--to", "until_iso", default=None,
              help="Only show incidents at or before this ISO timestamp (UTC).")
@click.option("--type", "incident_type", default=None,
              type=click.Choice(["down", "degraded", "all"], case_sensitive=False),
              help="Filter by peak severity. Default: all.")
@click.option("--open", "open_only", is_flag=True,
              help="Show only incidents that are still ongoing.")
@click.option("--limit", default=None, type=int,
              help="Maximum number of incidents to show (most recent first).")
@click.option("--summary", "summary_only", is_flag=True,
              help="Show aggregate counts/durations instead of the per-incident list.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def incidents(
    config: str | None,
    service: str | None,
    hours: int | None,
    since_iso: str | None,
    until_iso: str | None,
    incident_type: str | None,
    open_only: bool,
    limit: int | None,
    summary_only: bool,
    as_json: bool,
) -> None:
    """Show the incident timeline (every state-change that wasn't UP→UP).

    Incidents are recorded automatically by the ``watch`` and ``dashboard``
    loops. If those haven't run yet (or you just imported a fresh database),
    PulseBoard can also reconstruct incidents from the raw check history
    by re-running the state-change detector.

    Use ``--summary`` to get aggregate downtime / MTTR stats, or pass
    ``--json`` for machine-readable output.
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    storage = Storage(settings["db_path"])

    since: datetime | None = None
    until: datetime | None = None
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if since_iso is not None:
        since = datetime.fromisoformat(since_iso)
    if until_iso is not None:
        until = datetime.fromisoformat(until_iso)

    types: set[Status] | None = None
    if incident_type is not None and incident_type.lower() != "all":
        types = {Status(incident_type.lower())}

    results = storage.get_incidents(
        service_name=service,
        since=since,
        until=until,
        types=types,
        open_only=open_only,
        order="desc",
        limit=limit,
    )

    if as_json:
        from .incidents import summarize
        payload: dict[str, object] = {
            "incidents": [i.to_dict() for i in results],
            "summary": summarize(results),
        }
        click.echo(json.dumps(payload, indent=2, default=str))
    elif summary_only:
        from .incidents import summarize as summarize_incidents
        s = summarize_incidents(results)
        console.print(f"[bold]Total:[/bold]      {s['total']}")
        console.print(f"[bold]Open:[/bold]       {s['open']}")
        console.print(f"[bold]Closed:[/bold]     {s['closed']}")
        console.print(f"[bold]DOWN:[/bold]       {s['down']}")
        console.print(f"[bold]DEGRADED:[/bold]   {s['degraded']}")
        from .incidents import format_duration
        console.print(
            f"[bold]Total downtime:[/bold]  {format_duration(s['total_downtime_seconds'])}"
        )
        console.print(
            f"[bold]Avg duration:[/bold]    {format_duration(s['average_duration_seconds'])}"
        )
        console.print(
            f"[bold]Longest:[/bold]        {format_duration(s['longest_duration_seconds'])}"
        )
    else:
        if not results:
            console.print("[yellow]No incidents recorded in this window.[/yellow]")
            console.print(
                "Run [bold]pulseboard watch[/bold] (or [bold]pulseboard dashboard[/bold]) "
                "to start tracking outages, or expand your time window with --hours."
            )
            storage.close()
            sys.exit(0)
        from .dashboard import build_incident_table
        table = build_incident_table(results)
        console.print(table)
        from .incidents import summarize
        s = summarize(results)
        console.print(
            f"\n[dim]{s['total']} incident(s) — {s['open']} open, "
            f"{s['down']} DOWN, {s['degraded']} DEGRADED[/dim]"
        )

    storage.close()


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--days", "-d", default=None, type=int,
              help="Show only certificates expiring within N days")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def certs(config: str | None, days: int | None, as_json: bool) -> None:
    """Check SSL certificate expiry for configured SSL services."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    services = parse_services(cfg)
    ssl_services = [s for s in services if s.service_type == ServiceType.SSL]
    if not ssl_services:
        console.print("[yellow]No SSL services configured.[/yellow]")
        console.print("Add a service with [bold]type: ssl[/bold] to your config.")
        sys.exit(0)

    results = asyncio.run(run_all_checks(ssl_services))

    # Optional filter by expiry window
    if days is not None:
        results = [
            r
            for r in results
            if r.details.get("days_until_expiry") is None
            or r.details["days_until_expiry"] <= days
        ]

    if as_json:
        import json
        click.echo(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        table = build_cert_table(results)
        console.print(table)
        bad = [r for r in results if r.status != Status.UP]
        if bad:
            sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def dns(config: str | None, as_json: bool) -> None:
    """Run DNS queries for configured DNS services."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    services = parse_services(cfg)
    dns_services = [s for s in services if s.service_type == ServiceType.DNS]
    if not dns_services:
        console.print("[yellow]No DNS services configured.[/yellow]")
        console.print("Add a service with [bold]type: dns[/bold] to your config.")
        sys.exit(0)

    results = asyncio.run(run_all_checks(dns_services))

    if as_json:
        import json
        click.echo(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        table = build_dns_table(results)
        console.print(table)
        bad = [r for r in results if r.status != Status.UP]
        if bad:
            console.print(f"\n[red]{len(bad)} DNS service(s) failing[/red]")
            sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def validate(config: str | None, as_json: bool) -> None:
    """Run HTTP checks and report body content validation results."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    services = parse_services(cfg)
    http_services = [
        s
        for s in services
        if s.service_type == ServiceType.HTTP and has_content_checks(s)
    ]
    if not http_services:
        console.print(
            "[yellow]No HTTP services with body content checks configured.[/yellow]"
        )
        console.print(
            "Add body_contains, body_regex, json_path, or body_not_contains "
            "to an HTTP service to use this command."
        )
        sys.exit(0)

    results = asyncio.run(run_all_checks(http_services))

    if as_json:
        import json
        click.echo(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        table = build_content_validation_table(results)
        console.print(table)
        bad = [r for r in results if r.status != Status.UP]
        if bad:
            console.print(f"\n[red]{len(bad)} service(s) failing content validation[/red]")
            sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Output file path. Defaults to stdout. Extension determines format.")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"], case_sensitive=False),
              default=None, help="Output format. Defaults to file extension, or csv if piped to stdout.")
@click.option("--service", "-s", default=None,
              help="Export only checks for this service name.")
@click.option("--hours", "hours", default=None, type=int,
              help="Only export checks from the last N hours.")
@click.option("--from", "since_iso", default=None,
              help="Only export checks at or after this ISO timestamp (UTC).")
@click.option("--to", "until_iso", default=None,
              help="Only export checks at or before this ISO timestamp (UTC).")
@click.option("--limit", "limit", default=None, type=int,
              help="Maximum number of records to export (most recent first when set).")
@click.option("--status", "status", default=None,
              type=click.Choice(["up", "down", "degraded", "unknown"],
                                case_sensitive=False),
              help="Only export checks with this status.")
def export(
    config: str | None,
    output: str | None,
    fmt: str | None,
    service: str | None,
    hours: int | None,
    since_iso: str | None,
    until_iso: str | None,
    limit: int | None,
    status: str | None,
) -> None:
    """Export stored check history to CSV or JSON.

    Defaults to writing to stdout in CSV format (handy for piping into
    ``awk``, ``column``, or redirecting to a file). When ``--output`` is
    provided, the file extension decides the format unless ``--format`` is
    explicit.
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    storage = Storage(settings["db_path"])

    # Resolve filters
    since: datetime | None = None
    until: datetime | None = None
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if since_iso is not None:
        since = datetime.fromisoformat(since_iso)
    if until_iso is not None:
        until = datetime.fromisoformat(until_iso)

    order = "desc" if limit is not None else "asc"
    results = storage.get_history(
        service_name=service,
        since=since,
        until=until,
        order=order,
        status=status,
    )
    if limit is not None:
        results = results[:limit]

    # Resolve format & destination
    if fmt is None:
        if output is not None:
            fmt = infer_format(output)
        else:
            fmt = "csv"
    fmt = fmt.lower()

    if output:
        count = write_export(results, output, fmt)
        console.print(
            f"[green]✓[/green] Exported {count} check record(s) to {output}"
        )
    else:
        count = write_export_stream(results, sys.stdout, fmt)

    storage.close()


@cli.command()
def config_path() -> None:
    """Show the config file location."""
    try:
        path = find_config()
        console.print(f"[green]{path}[/green]")
    except FileNotFoundError:
        console.print(f"[dim]No config found. Default location: {DEFAULT_CONFIG_PATH}[/dim]")


@cli.command("validate-config")
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
def validate_config(config: str | None) -> None:
    """Validate the configuration without running checks."""
    try:
        cfg = load_config(config)
        services = parse_services(cfg)
        settings = get_settings(cfg)
        notifier = NotificationDispatcher.from_config(
            settings.get("notification_channels", [])
        )
        known_channels = set(notifier._by_name)
        for service in services:
            for channel_name in service.alert_channels:
                if channel_name not in known_channels:
                    raise ConfigError(
                        f"Service '{service.name}': alert_channels references "
                        f"unknown notification channel '{channel_name}'"
                    )
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.exceptions.Exit(1) from e
    except yaml.YAMLError as e:
        console.print(f"[red]✗[/red] Invalid YAML: {e}")
        raise click.exceptions.Exit(1) from e
    except (ConfigError, KeyError, TypeError, ValueError) as e:
        console.print(f"[red]✗[/red] Invalid config: {e}")
        raise click.exceptions.Exit(1) from e

    count = len(services)
    noun = "service" if count == 1 else "services"
    console.print(f"[green]✓[/green] Config valid: {count} {noun}")


def _build_test_alert(service_name: str) -> Alert:
    """Construct a synthetic DOWN alert for notify-test."""
    from .models import CheckResult

    result = CheckResult(
        service_name=service_name,
        timestamp=datetime.now(timezone.utc),
        status=Status.DOWN,
        latency_ms=0.0,
        error="This is a PulseBoard test alert — your channels are configured correctly.",
    )
    return Alert(
        service_name=service_name,
        alert_type=AlertType.DOWN,
        result=result,
        message=f"🔴 {service_name} is DOWN (test alert)",
    )


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option(
    "--service",
    "-s",
    default="PulseBoard Test",
    help="Service name to use in the synthetic test alert.",
)
@click.option(
    "--channel",
    "channel_name",
    default=None,
    help="Send to this channel only (by name). Default: all configured channels.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def notify_test(
    config: str | None,
    service: str,
    channel_name: str | None,
    as_json: bool,
) -> None:
    """Send a synthetic alert through every configured notification channel.

    Useful for verifying webhook URLs, Telegram bot tokens, and Discord
    channel routing after editing the config -- without having to wait
    for an actual outage.
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    try:
        notifier = NotificationDispatcher.from_config(
            settings.get("notification_channels", [])
        )
    except ValueError as e:
        console.print(f"[red]✗[/red] Invalid notification_channels config: {e}")
        sys.exit(1)

    if not notifier.channels:
        console.print(
            "[yellow]No notification channels configured.[/yellow]\n"
            "Add a 'notification_channels:' block under 'settings:' in your "
            "config (see README 'Notification Channels' section)."
        )
        sys.exit(1)

    if channel_name is not None:
        if channel_name not in notifier._by_name:
            console.print(
                f"[red]✗[/red] No channel named '{channel_name}'. "
                f"Known: {', '.join(notifier._by_name) or '(none)'}"
            )
            sys.exit(1)
        notifier.channels = [notifier._by_name[channel_name]]

    alert = _build_test_alert(service)
    console.print(
        f"[dim]Sending test alert to {len(notifier.channels)} channel(s)...[/dim]"
    )
    results = notifier.dispatch_sync(alert)

    if as_json:
        click.echo(
            json.dumps(
                [r.to_dict() for r in results],
                indent=2,
            )
        )
    else:
        from rich.table import Table

        table = Table(title="Notify Test Results", show_lines=False)
        table.add_column("Channel", style="bold")
        table.add_column("Type")
        table.add_column("Status", justify="center")
        table.add_column("HTTP")
        table.add_column("Detail")
        for r in results:
            status = (
                "[green]OK[/green]"
                if r.success
                else f"[red]FAIL[/red]"
            )
            detail = r.error or ""
            if len(detail) > 60:
                detail = detail[:57] + "..."
            table.add_row(
                r.channel.name,
                r.channel.channel_type.value,
                status,
                str(r.status_code) if r.status_code is not None else "-",
                detail,
            )
        console.print(table)

    failed = [r for r in results if not r.success]
    if failed:
        sys.exit(1)


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def notify_list(config: str | None, as_json: bool) -> None:
    """List configured notification channels."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    try:
        notifier = NotificationDispatcher.from_config(
            settings.get("notification_channels", [])
        )
    except ValueError as e:
        console.print(f"[red]✗[/red] Invalid notification_channels config: {e}")
        sys.exit(1)

    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "name": c.name,
                        "type": c.channel_type.value,
                        "webhook_url_set": bool(c.webhook_url),
                        "telegram_token_set": bool(c.telegram_token),
                        "telegram_chat_id": c.telegram_chat_id,
                    }
                    for c in notifier.channels
                ],
                indent=2,
            )
        )
    else:
        from rich.table import Table

        if not notifier.channels:
            console.print("[yellow]No notification channels configured.[/yellow]")
            return
        table = Table(title="Notification Channels")
        table.add_column("Name", style="bold")
        table.add_column("Type")
        table.add_column("Target")
        for c in notifier.channels:
            if c.channel_type.value == "telegram":
                target = f"chat {c.telegram_chat_id}"
            else:
                # Mask the webhook URL so secrets don't leak into shell scrollback.
                target = c.webhook_url or ""
                if "://" in target:
                    scheme, rest = target.split("://", 1)
                    if len(rest) > 24:
                        target = f"{scheme}://{rest[:8]}…{rest[-6:]}"
            table.add_row(c.name, c.channel_type.value, target)
        console.print(table)


@cli.command()
@click.option(
    "--config", "-c", type=click.Path(), default=None, help="Config file path."
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help=(
        "Write the Prometheus text payload to this file (textfile mode). "
        "Parent directories are created as needed. Mutually exclusive with "
        "--serve."
    ),
)
@click.option(
    "--serve",
    is_flag=True,
    help=(
        "Run an HTTP server exposing /metrics, /, and /healthz instead of "
        "writing to stdout or a file. Combine with --port / --host to "
        "control the bind address."
    ),
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Bind host for --serve mode. Default: 127.0.0.1 (loopback only).",
)
@click.option(
    "--port",
    type=int,
    default=9464,
    help="Bind port for --serve mode. Default: 9464 (the IANA-registered "
    "Prometheus default port range).",
)
@click.option(
    "--hours",
    type=int,
    default=24,
    help="Time window for windowed aggregates (uptime, latency stats).",
)
def metrics(
    config: str | None,
    output: str | None,
    serve: bool,
    host: str,
    port: int,
    hours: int,
) -> None:
    """Export check history as Prometheus metrics.

    By default the rendered text payload is written to stdout — pipe
    it to ``curl --data-binary @-`` against a Pushgateway, or capture
    it into a file for inspection.

    Three modes are supported:

    \b
      - stdout (default)        emit the payload to standard output.
      - textfile (--output)     atomically write the payload to a file,
                                suitable for node_exporter's
                                --collector.textfile.directory.
      - serve   (--serve)       run an HTTP server exposing
                                /metrics, /, and /healthz endpoints.

    Metric families are documented in :mod:`pulseboard.metrics`.
    """
    if serve and output:
        console.print(
            "[red]✗[/red] --serve and --output are mutually exclusive."
        )
        sys.exit(2)

    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)

    # Build a resolver that maps service name -> ServiceType.value
    # using the parsed config. Services with no stored checks fall
    # back to "unknown" via the metrics module.
    services = parse_services(cfg)
    type_by_name: dict[str, str] = {
        s.name: s.service_type.value for s in services
    }

    def _resolve_service_type(name: str) -> str:
        return type_by_name.get(name, "unknown")

    storage = Storage(settings["db_path"])
    try:
        exporter = MetricsExporter(
            storage=storage,
            hours=hours,
            service_type_resolver=_resolve_service_type,
        )

        if serve:
            console.print(
                f"[green]✓[/green] Serving Prometheus metrics on "
                f"[cyan]http://{host}:{port}/[/cyan] "
                f"([dim]Ctrl+C to stop[/dim])"
            )
            try:
                serve_metrics(exporter, host=host, port=port)
            except KeyboardInterrupt:
                console.print("\n[yellow]Stopped.[/yellow]")
            except OSError as e:
                console.print(f"[red]✗[/red] Failed to bind {host}:{port}: {e}")
                sys.exit(1)
            return

        if output:
            n = exporter.write_textfile(output)
            console.print(
                f"[green]✓[/green] Wrote [bold]{n}[/bold] samples to "
                f"[cyan]{output}[/cyan]"
            )
            return

        # Default: stdout.
        click.echo(exporter.render(), nl=False)
    finally:
        storage.close()


@cli.command()
@click.option(
    "--config", "-c", type=click.Path(), default=None, help="Config file path."
)
@click.option(
    "--group",
    "group_name",
    default=None,
    help="Show only the named group. Default: show all groups.",
)
@click.option(
    "--graph",
    is_flag=True,
    help="Show the dependency graph (which services depend on what) "
    "instead of the group roll-up table.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON (object with a 'groups' list).",
)
def groups(
    config: str | None,
    group_name: str | None,
    graph: bool,
    as_json: bool,
) -> None:
    """Show rolled-up service health per group and the dependency graph.

    Groups are declared on each service with a ``groups: [name, ...]`` list.
    A group's status is the worst-case status of its members (any DOWN
    makes the whole group DOWN). When *any* dependency declared via
    ``depends_on`` is not UP, the dependent service is downgraded before
    the roll-up is computed, so a single upstream failure can't disguise
    itself as N independent outages.

    Use ``--graph`` to print a topological view of the dependency graph
    (``api -> database``), or pass ``--json`` for machine-readable
    output. Combine with ``--group <name>`` to focus on a single group.
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    services = parse_services(cfg)

    # Validate `--group` if provided.
    if group_name is not None:
        all_names = {g for s in services for g in (s.groups or [])}
        # Also allow filtering by group name even if the group isn't in
        # any current service's groups list — we just show an empty result.
        del all_names  # kept for documentation; we always return an empty
                       # result rather than erroring, so users can pipe
                       # with confidence.

    if graph:
        text = describe_dependency_graph(services)
        if not text:
            console.print("[yellow]No services configured.[/yellow]")
        else:
            console.print(
                "[bold cyan]⚡ PulseBoard — Dependency Graph[/bold cyan]"
            )
            console.print(text)
        return

    if not services:
        if as_json:
            click.echo(json.dumps({"groups": []}, indent=2))
        else:
            console.print("[yellow]No services configured.[/yellow]")
        return

    # If the user filtered to a specific group, restrict member services
    # so the JSON / table only describe that one group.
    if group_name is not None:
        services_for_summary = list_services_in_group(services, group_name)
    else:
        services_for_summary = services

    # No groups declared at all.
    if not services_for_summary and all(not (s.groups or []) for s in services):
        msg = "[yellow]No service groups configured.[/yellow] "\
              "Set `groups: [name, ...]` on at least one service."
        if as_json:
            click.echo(json.dumps({"groups": []}, indent=2))
        else:
            console.print(msg)
        return

    # The roll-up is computed against `None` (no live results here) — it
    # is purely a roster view. Live status counts come from `pulseboard
    # status` or the dashboard. We deliberately do NOT run live checks
    # from this command (it stays cheap and configuration-only).
    summaries = build_group_summaries(services_for_summary, None)

    if as_json:
        payload = {
            "groups": [s.to_dict() for s in summaries],
            "filter": group_name,
        }
        click.echo(json.dumps(payload, indent=2))
        return

    if not summaries:
        console.print(
            f"[yellow]No services match group '{group_name}'.[/yellow]"
        )
        return

    console.print(build_groups_table(summaries))

    # When more than one group exists, add a small footer so the output
    # is self-explanatory. The dependency graph is always available via
    # `--graph` for those who want it.
    if len(summaries) > 1:
        console.print(
            "\n[dim]Pass --group <name> to focus on one group, "
            "or --graph to view the dependency graph.[/dim]"
        )


@cli.command()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--service", "-s", default=None,
              help="Show only alerts for this service name.")
@click.option("--hours", default=None, type=int,
              help="Only show alerts from the last N hours.")
@click.option("--from", "since_iso", default=None,
              help="Only show alerts at or after this ISO timestamp (UTC).")
@click.option("--to", "until_iso", default=None,
              help="Only show alerts at or before this ISO timestamp (UTC).")
@click.option("--type", "alert_type", default=None,
              type=click.Choice(["down", "recovery", "degraded"], case_sensitive=False),
              help="Filter by alert type. Default: all.")
@click.option("--limit", default=None, type=int,
              help="Maximum number of alerts to show (most recent first).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def alerts(
    config: str | None,
    service: str | None,
    hours: int | None,
    since_iso: str | None,
    until_iso: str | None,
    alert_type: str | None,
    limit: int | None,
    as_json: bool,
) -> None:
    """Show the alert history log — a durable audit trail of fired alerts.

    Every time PulseBoard fires an alert (down, recovery, degraded, re-alert),
    the ``watch`` and ``dashboard`` loops persist a record.  This command
    lets you query that history:

    \b
        pulseboard alerts                    # recent alerts
        pulseboard alerts --service api       # alerts for one service
        pulseboard alerts --hours 6           # last 6 hours
        pulseboard alerts --type recovery     # only recovery alerts
        pulseboard alerts --json               # machine-readable
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    settings = get_settings(cfg)
    storage = Storage(settings["db_path"])

    since: datetime | None = None
    until: datetime | None = None
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if since_iso is not None:
        since = datetime.fromisoformat(since_iso)
    if until_iso is not None:
        until = datetime.fromisoformat(until_iso)

    type_filter = alert_type.lower() if alert_type is not None else None

    results = storage.get_alerts(
        service_name=service,
        alert_type=type_filter,
        since=since,
        until=until,
        order="desc",
        limit=limit,
    )

    if as_json:
        click.echo(json.dumps(results, indent=2, default=str))
        storage.close()
        return

    if not results:
        console.print("[yellow]No alerts recorded in this window.[/yellow]")
        console.print(
            "Run [bold]pulseboard watch[/bold] "
            "(or [bold]pulseboard dashboard[/bold]) to start alerting."
        )
        storage.close()
        sys.exit(0)

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="⚡ PulseBoard — Alert History",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Time", width=19, style="dim")
    table.add_column("Service", style="bold", min_width=16)
    table.add_column("Type", width=9, justify="center")
    table.add_column("Failures", width=9, justify="right")
    table.add_column("Message", max_width=80)

    for r in results:
        ts = r.get("timestamp", "?")
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            ts_str = str(ts)

        a_type = r.get("alert_type", "?")
        style = "red" if a_type == "down" else "green" if a_type == "recovery" \
            else "yellow"
        type_label = Text(a_type.upper(), style=f"bold {style}")

        msg = r.get("message", "") or ""
        if len(msg) > 100:
            msg = msg[:97] + "..."
        failures = r.get("consecutive_failures", 0)
        fail_str = str(failures) if failures else "—"

        table.add_row(
            ts_str,
            r.get("service_name", "?"),
            type_label,
            fail_str,
            msg,
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(results)} alert(s) in this window.[/dim]"
    )
    storage.close()


if __name__ == "__main__":
    cli()
