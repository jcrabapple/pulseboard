"""CLI entry point for PulseBoard."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live

from . import __version__
from .alerting import AlertManager, terminal_alert, send_webhook_alert
from .config import (
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
    build_overview_table,
    console,
    print_status_line,
    render_dashboard,
)
from .monitor import run_all_checks, run_all_checks_with_thresholds, run_check
from .models import ServiceType, Status
from .storage import Storage
from .content_check import has_content_checks
from .export import infer_format, write_export, write_export_stream


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
def check(config: str | None, as_json: bool) -> None:
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

    results = asyncio.run(run_all_checks(services))

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
    alerter = AlertManager(alert_on_recovery=settings.get("alert_on_recovery", True))

    console.print(f"[bold cyan]⚡ PulseBoard[/bold cyan] monitoring {len(services)} services (Ctrl+C to stop)\n")

    try:
        while True:
            # History provider for error-rate thresholds — exclude the
            # in-flight batch so we don't double-count.
            def history_provider(
                name: str, _pending: list[str] | None = None
            ) -> list[CheckResult]:
                return storage.get_recent(name, limit=200)

            results = asyncio.run(
                run_all_checks_with_thresholds(services, history_provider)
            )
            storage.store_many(results)

            # Evaluate alerts
            for r in results:
                alert = alerter.evaluate(r)
                if alert:
                    terminal_alert(alert)
                    svc = next((s for s in services if s.name == r.service_name), None)
                    if svc and svc.alert_webhook:
                        asyncio.run(send_webhook_alert(svc.alert_webhook, alert))

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
    alerter = AlertManager(alert_on_recovery=settings.get("alert_on_recovery", True))
    refresh = settings.get("dashboard_refresh", 5)

    console.print("[bold cyan]⚡ PulseBoard Dashboard[/bold cyan] (Ctrl+C to exit)\n")

    try:
        with Live(render_dashboard([], []), refresh_per_second=1, console=console) as live:
            while True:
                # Run checks
                def history_provider(name: str) -> list[CheckResult]:
                    return storage.get_recent(name, limit=200)

                results = asyncio.run(
                    run_all_checks_with_thresholds(services, history_provider)
                )
                storage.store_many(results)

                for r in results:
                    alert = alerter.evaluate(r)
                    if alert:
                        terminal_alert(alert)

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
def export(
    config: str | None,
    output: str | None,
    fmt: str | None,
    service: str | None,
    hours: int | None,
    since_iso: str | None,
    until_iso: str | None,
    limit: int | None,
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


if __name__ == "__main__":
    cli()
