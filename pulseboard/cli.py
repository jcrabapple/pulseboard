"""CLI entry point for PulseBoard."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live

from . import __version__
from .alerting import Alert, AlertManager, AlertType, terminal_alert
from .notifications import NotificationDispatcher
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
    notifier = NotificationDispatcher.from_config(
        settings.get("notification_channels", [])
    )

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
                prev = alerter.previous_status(r.service_name)
                alert = alerter.evaluate(r)
                if alert:
                    terminal_alert(alert)
                    svc = next(
                        (s for s in services if s.name == r.service_name), None
                    )
                    notifier.dispatch_sync(alert, svc)
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
    alerter = AlertManager(alert_on_recovery=settings.get("alert_on_recovery", True))
    notifier = NotificationDispatcher.from_config(
        settings.get("notification_channels", [])
    )
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
                    prev = alerter.previous_status(r.service_name)
                    alert = alerter.evaluate(r)
                    if alert:
                        terminal_alert(alert)
                        svc = next(
                            (s for s in services if s.name == r.service_name), None
                        )
                        notifier.dispatch_sync(alert, svc)
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


def _build_test_alert(service_name: str) -> Alert:
    """Construct a synthetic DOWN alert for notify-test."""
    from .models import CheckResult, Status
    from datetime import datetime, timezone

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


if __name__ == "__main__":
    cli()
