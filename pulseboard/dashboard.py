"""TUI dashboard using Rich for beautiful terminal output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import CheckResult, ServiceSummary, Status


console = Console()


def status_style(status: Status) -> str:
    """Return Rich style for a status."""
    return {
        Status.UP: "bold green",
        Status.DOWN: "bold red",
        Status.DEGRADED: "bold yellow",
        Status.UNKNOWN: "dim",
    }.get(status, "dim")


def status_emoji(status: Status) -> str:
    return {"up": "🟢", "down": "🔴", "degraded": "🟡", "unknown": "⚪"}.get(
        status, "⚪"
    )


def latency_bar(ms: float, max_ms: float = 2000) -> str:
    """ASCII latency bar visualization."""
    width = 20
    filled = min(int((ms / max_ms) * width), width) if max_ms > 0 else 0
    if ms < 200:
        color = "green"
    elif ms < 500:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {ms:.0f}ms"


def build_overview_table(summaries: list[ServiceSummary]) -> Table:
    """Build the main status overview table."""
    table = Table(
        title="⚡ PulseBoard — Service Status",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Status", width=6, justify="center")
    table.add_column("Service", style="bold", min_width=20)
    table.add_column("Uptime", justify="right", width=8)
    table.add_column("Latency", min_width=28)
    table.add_column("Checks", justify="right", width=8)
    table.add_column("Last Check", width=16)

    for s in sorted(summaries, key=lambda x: x.last_status != Status.UP):
        last_str = s.last_check.strftime("%H:%M:%S") if s.last_check else "—"
        uptime_color = "green" if s.uptime_pct >= 99 else "yellow" if s.uptime_pct >= 95 else "red"

        table.add_row(
            status_emoji(s.last_status),
            s.service_name,
            f"[{uptime_color}]{s.uptime_pct:.1f}%[/{uptime_color}]",
            latency_bar(s.avg_latency_ms),
            f"{s.total_checks}",
            last_str,
        )

    return table


def build_latency_table(summaries: list[ServiceSummary]) -> Table:
    """Build a detailed latency breakdown table."""
    table = Table(
        title="📊 Latency Breakdown (24h)",
        show_header=True,
        header_style="bold magenta",
        border_style="bright_black",
    )
    table.add_column("Service", style="bold", min_width=20)
    table.add_column("Avg", justify="right", width=8)
    table.add_column("P95", justify="right", width=8)
    table.add_column("P99", justify="right", width=8)
    table.add_column("Min", justify="right", width=8)
    table.add_column("Max", justify="right", width=8)

    for s in sorted(summaries, key=lambda x: x.avg_latency_ms, reverse=True):
        if s.total_checks == 0:
            continue
        table.add_row(
            s.service_name,
            f"{s.avg_latency_ms:.0f}ms",
            f"{s.p95_latency_ms:.0f}ms",
            f"{s.p99_latency_ms:.0f}ms",
            f"{s.min_latency_ms:.0f}ms",
            f"{s.max_latency_ms:.0f}ms",
        )

    return table


def build_recent_checks_table(results: list[CheckResult], limit: int = 15) -> Table:
    """Build a table of recent check results."""
    table = Table(
        title=f"🕐 Last {limit} Checks",
        show_header=True,
        header_style="bold blue",
        border_style="bright_black",
    )
    table.add_column("Time", width=12)
    table.add_column("Service", style="bold", min_width=20)
    table.add_column("Status", width=8, justify="center")
    table.add_column("Latency", justify="right", width=10)
    table.add_column("Error", max_width=40)

    for r in results[:limit]:
        ts = r.timestamp.strftime("%H:%M:%S")
        table.add_row(
            ts,
            r.service_name,
            Text(status_emoji(r.status), style=status_style(r.status)),
            f"{r.latency_ms:.0f}ms",
            r.error or "",
        )

    return table


def render_dashboard(summaries: list[ServiceSummary], recent: list[CheckResult]) -> Layout:
    """Compose the full dashboard layout."""
    layout = Layout()
    layout.split_column(
        Layout(build_overview_table(summaries), name="overview", size=len(summaries) + 5),
        Layout(name="bottom", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(build_latency_table(summaries), name="latency"),
        Layout(build_recent_checks_table(recent), name="recent"),
    )
    return layout


def print_status_line(results: list[CheckResult]) -> None:
    """Print a single-line status summary (for non-TUI mode)."""
    parts = []
    for r in results:
        emoji = status_emoji(r.status)
        parts.append(f"{emoji} {r.service_name} ({r.latency_ms:.0f}ms)")
    console.print(" | ".join(parts))


def build_cert_table(results: list[CheckResult]) -> Table:
    """Build a certificate expiry overview table from SSL check results."""
    table = Table(
        title="🔒 SSL Certificate Status",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Status", width=6, justify="center")
    table.add_column("Service", style="bold", min_width=18)
    table.add_column("Host", min_width=22)
    table.add_column("Issuer", min_width=24)
    table.add_column("Expires", width=14, justify="right")
    table.add_column("Days Left", width=10, justify="right")

    def _sort_key(r: CheckResult) -> float:
        days = r.details.get("days_until_expiry")
        return days if days is not None else 1e9

    for r in sorted(results, key=_sort_key):
        host = f"{r.details.get('host', '?')}:{r.details.get('port', '?')}"
        issuer = r.details.get("issuer", "<unknown>")
        if len(issuer) > 40:
            issuer = issuer[:37] + "..."
        expires = r.details.get("not_after") or "?"
        days = r.details.get("days_until_expiry")
        if days is None:
            days_str = "[red]?[/red]"
        elif days <= 0:
            days_str = f"[red]{days:.0f}d (expired)[/red]"
        elif days <= 14:
            days_str = f"[red]{days:.0f}d[/red]"
        elif days <= 30:
            days_str = f"[yellow]{days:.0f}d[/yellow]"
        else:
            days_str = f"[green]{days:.0f}d[/green]"

        table.add_row(
            Text(status_emoji(r.status), style=status_style(r.status)),
            r.service_name,
            host,
            issuer,
            expires,
            days_str,
        )
    return table
