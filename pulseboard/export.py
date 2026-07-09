"""Export check history to CSV or JSON for analysis in other tools.

Pure serialization helpers — no I/O assumptions beyond a writable stream
or path. The :mod:`pulseboard.cli` command wires this up to the database.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import IO, Any, Iterable

from .models import CheckResult


# Stable column order for CSV — keeps Excel / pandas imports happy.
CSV_COLUMNS: tuple[str, ...] = (
    "service_name",
    "timestamp",
    "status",
    "latency_ms",
    "status_code",
    "error",
)


def to_rows(results: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert CheckResult objects to flat dicts for export.

    Accepts an iterable of :class:`CheckResult` *or* already-flattened
    dicts (so the helpers compose without double-conversion).

    Pure transformation — does no I/O so it's easy to test.
    """
    out: list[dict[str, Any]] = []
    for item in results:
        if isinstance(item, CheckResult):
            out.append(item.to_export_row())
        elif isinstance(item, dict):
            out.append(item)
        else:
            raise TypeError(
                f"to_rows expects CheckResult or dict, got {type(item).__name__}"
            )
    return out


def to_json(results: Iterable[Any], *, indent: int | None = 2) -> str:
    """Serialize results to a JSON string.

    ``indent`` of ``None`` produces compact output (single line per record
    when later split); default ``2`` produces pretty output suitable for
    human inspection.
    """
    rows = to_rows(results)
    payload = {
        "exported_at": _now_iso(),
        "count": len(rows),
        "records": rows,
    }
    return json.dumps(payload, indent=indent, ensure_ascii=False)


def to_csv(results: Iterable[Any]) -> str:
    """Serialize results to a CSV string with the stable column order."""
    return _render_csv(to_rows(results))


def write_export(
    results: Iterable[CheckResult],
    path: str | Path,
    fmt: str,
) -> int:
    """Write ``results`` to ``path`` in the given format.

    Args:
        results: Iterable of :class:`CheckResult`.
        path: Destination file path. Parent directories are created.
        fmt: ``"csv"`` or ``"json"`` (case-insensitive).

    Returns:
        Number of records written.

    Raises:
        ValueError: If ``fmt`` is not a supported format.
    """
    fmt_norm = fmt.lower().lstrip(".")
    rows = to_rows(results)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if fmt_norm == "csv":
        content = _render_csv(rows)
        target.write_text(content, encoding="utf-8")
    elif fmt_norm == "json":
        # For files we always use compact JSON to keep them small.
        target.write_text(to_json(rows, indent=None), encoding="utf-8")
    else:
        raise ValueError(
            f"Unsupported export format: '{fmt}'. Use 'csv' or 'json'."
        )
    return len(rows)


def _render_csv(rows: list[dict[str, Any]]) -> str:
    """Render rows to CSV text with stable columns and proper quoting."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def write_export_stream(
    results: Iterable[CheckResult],
    stream: IO[str],
    fmt: str,
) -> int:
    """Like :func:`write_export` but writes to an already-open text stream.

    Useful for tests and for piping directly to stdout from the CLI.
    """
    fmt_norm = fmt.lower().lstrip(".")
    rows = to_rows(results)

    if fmt_norm == "csv":
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    elif fmt_norm == "json":
        stream.write(to_json(rows, indent=2))
    else:
        raise ValueError(
            f"Unsupported export format: '{fmt}'. Use 'csv' or 'json'."
        )
    return len(rows)


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def infer_format(path: str | Path) -> str:
    """Guess ``"csv"`` or ``"json"`` from a file extension.

    Falls back to ``"json"`` if the extension is not recognised.
    """
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in ("csv", "tsv"):
        return "csv"
    return "json"