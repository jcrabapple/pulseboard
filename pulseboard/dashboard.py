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


def build_content_validation_table(results: list[CheckResult]) -> Table:
    """Build a content-validation results table from HTTP check results."""
    table = Table(
        title="✅ Content Validation",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Status", width=6, justify="center")
    table.add_column("Service", style="bold", min_width=18)
    table.add_column("HTTP", justify="right", width=6)
    table.add_column("Checks", min_width=20)
    table.add_column("Notes", max_width=50)

    def _sort_key(r: CheckResult) -> int:
        order = {"up": 0, "degraded": 1, "down": 2, "unknown": 3}
        return order.get(r.status.value, 9)

    for r in sorted(results, key=_sort_key):
        checks = r.details.get("content_checks") or []
        if checks:
            ok = sum(1 for c in checks if c.get("passed"))
            total = len(checks)
            check_str = f"[green]{ok}[/green]/{total} passed"
        else:
            check_str = "[dim]—[/dim]"

        table.add_row(
            Text(status_emoji(r.status), style=status_style(r.status)),
            r.service_name,
            str(r.status_code) if r.status_code is not None else "—",
            check_str,
            (r.error or "")[:120],
        )
    return table


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


def build_dns_table(results: list[CheckResult]) -> Table:
    """Build a DNS query results table."""
    table = Table(
        title="🌐 DNS Status",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Status", width=6, justify="center")
    table.add_column("Service", style="bold", min_width=16)
    table.add_column("Query", min_width=20)
    table.add_column("Type", width=6, justify="center")
    table.add_column("Answers", min_width=30)
    table.add_column("Latency", justify="right", width=10)
    table.add_column("Notes", max_width=40)

    for r in sorted(results, key=lambda x: x.status != Status.UP):
        query = r.details.get("query", "?")
        rdtype = r.details.get("record_type", "?")
        answers = r.details.get("answers", [])
        if len(answers) <= 3:
            answer_str = ", ".join(answers) if answers else "[dim]—[/dim]"
        else:
            answer_str = f"{answers[0]}, {answers[1]}, … ({len(answers)} records)"
        if len(answer_str) > 50:
            answer_str = answer_str[:47] + "..."

        notes = r.error or ""

        table.add_row(
            Text(status_emoji(r.status), style=status_style(r.status)),
            r.service_name,
            query,
            rdtype,
            answer_str,
            f"{r.latency_ms:.0f}ms",
            notes,
        )
    return table


def build_groups_table(summaries) -> Table:
    """Render a service-groups roll-up table.

    Each group is rendered as a single row with the worst-case status
    (with emoji + color), member counts (up / degraded / down / unknown),
    and total count. The member list itself is rendered underneath using
    a second row with a continuation row style.
    """
    from .groups import GroupSummary

    table = Table(
        title="⚡ PulseBoard — Service Groups",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Group", style="bold", min_width=12)
    table.add_column("Status", width=12, justify="center")
    table.add_column("Counts", justify="right", width=22)
    table.add_column("Members", min_width=8)

    sorted_summaries: list[GroupSummary] = sorted(
        summaries, key=lambda g: g.name,
    )

    for idx, gs in enumerate(sorted_summaries):
        assert isinstance(gs, GroupSummary)
        status_label = Text(
            status_emoji(gs.status) + " " + gs.status.value.upper(),
            style=status_style(gs.status),
        )
        counts = (
            f"[green]{gs.up} up[/green] / "
            f"[yellow]{gs.degraded} deg[/yellow] / "
            f"[red]{gs.down} down[/red] / "
            f"[dim]{gs.unknown} ?[/dim]"
        )
        members = ", ".join(sorted(gs.services)) or "[dim]—[/dim]"
        total = gs.up + gs.degraded + gs.down + gs.unknown
        table.add_row(
            gs.name,
            status_label,
            f"{counts}  [dim]({total})[/dim]",
            members,
        )
        if idx != len(sorted_summaries) - 1:
            # Thin separator between groups without consuming a row.
            table.add_section()
    return table


def build_incident_table(incidents: list) -> Table:
    """Render an incident-timeline table.

    Each row shows: when the outage started, the service, the worst
    severity, the duration, and a one-line error sample. Open incidents
    show a live duration (since ``started_at``) and are marked with a
    pulsing [yellow]●[/yellow].
    """
    from datetime import datetime, timezone
    from .incidents import format_duration

    table = Table(
        title="⚡ PulseBoard — Incident Timeline",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Started", width=19, style="dim")
    table.add_column("Service", style="bold", min_width=16)
    table.add_column("Severity", width=9, justify="center")
    table.add_column("Duration", justify="right", width=10)
    table.add_column("Error", max_width=60)

    now = datetime.now(timezone.utc)

    for inc in sorted(incidents, key=lambda i: i.started_at, reverse=True):
        sev = inc.severity
        sev_label = (
            Text(status_emoji(sev) + " " + sev.value.upper(), style=status_style(sev))
        )
        if inc.is_open:
            live = (now - inc.started_at).total_seconds()
            duration_str = format_duration(live) + " [yellow]●[/yellow]"
        else:
            duration_str = format_duration(inc.duration_seconds)
        err = inc.error or ""
        if len(err) > 80:
            err = err[:77] + "..."
        table.add_row(
            inc.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            inc.service_name,
            sev_label,
            duration_str,
            err or "[dim]—[/dim]",
        )
    return table
