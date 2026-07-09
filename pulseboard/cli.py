"""CLI entry point for PulseBoard."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
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
    build_overview_table,
    console,
    print_status_line,
    render_dashboard,
)
from .monitor import run_all_checks, run_check
from .models import ServiceType, Status
from .storage import Storage


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
            results = asyncio.run(run_all_checks(services))
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
                results = asyncio.run(run_all_checks(services))
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
def config_path() -> None:
    """Show the config file location."""
    try:
        path = find_config()
        console.print(f"[green]{path}[/green]")
    except FileNotFoundError:
        console.print(f"[dim]No config found. Default location: {DEFAULT_CONFIG_PATH}[/dim]")


if __name__ == "__main__":
    cli()
