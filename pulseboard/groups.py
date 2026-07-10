"""Service groups and dependency tracking for PulseBoard.

Service groups provide a logical layer on top of individual services:
you can tag services with group names (``groups: [production, backend]``)
and then see rolled-up group health with ``pulseboard groups``.

Dependency tracking lets you declare that one service depends on another
(e.g. a web app depends on its database). When a dependency is not UP,
the dependent service's status is annotated with the failing dependency
and optionally downgraded so you don't see misleading "Service X is DOWN"
alerts when the real problem is upstream.

Design rules
------------

- **No magic cascading.** Only direct dependencies are checked — we don't
  walk the entire transitive graph during runtime. The graph *is* validated
  for cycles at config-load time (so A→B→C→A is caught early), but the
  runtime logic only looks at immediate ``depends_on`` entries.

- **Conservative downgrade.** If a dependency is DEGRADED, the dependent
  can be annotated but is only downgraded from UP → DEGRADED (not DOWN).
  If a dependency is DOWN, the dependent is downgraded to DOWN regardless
  of its own health. A service already at DOWN is never upgraded.

- **Transparent details.** Every dependency-impact decision is written
  into ``CheckResult.details["dependency_impact"]`` so dashboards,
  exports, and the incident timeline can surface *why* a status changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import CheckResult, ServiceConfig, Status


# ---------------------------------------------------------------------------
# Data model: group summary
# ----------------------------------------------------------------

@dataclass
class GroupSummary:
    """Aggregated health for a named group of services."""

    name: str
    total: int = 0
    up: int = 0
    degraded: int = 0
    down: int = 0
    unknown: int = 0
    services: list[str] = field(default_factory=list)

    @property
    def status(self) -> Status:
        """Worst-case status across all services in this group."""
        if self.total == 0:
            return Status.UNKNOWN
        if self.down > 0:
            return Status.DOWN
        if self.degraded > 0:
            return Status.DEGRADED
        if self.unknown > 0 and self.up == 0:
            return Status.UNKNOWN
        return Status.UP

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": self.total,
            "up": self.up,
            "degraded": self.degraded,
            "down": self.down,
            "unknown": self.unknown,
            "status": self.status.value,
            "services": sorted(self.services),
        }


# ---------------------------------------------------------------------------
# Group aggregation helpers
# ----------------------------------------------------------------

def build_group_summaries(
    services: list[ServiceConfig],
    results: list[CheckResult] | None = None,
) -> list[GroupSummary]:
    """Build a summary for each unique group name across all services.

    When *results* is provided (a list of :class:`CheckResult` items —
    typically from a just-completed check run), the counts reflect those
    results.  When *results* is ``None`` the counters are zeroed but the
    membership lists are still populated (useful for the CLI to show the
    group roster without running a check first).

    Parameters
    ----------
    services:
        Parsed service list from :func:`pulseboard.config.parse_services`.
    results:
        Optional check results to drive the health counts.

    Returns
    -------
    list[GroupSummary]
        Sorted by group name.
    """
    groups: dict[str, GroupSummary] = {}
    for svc in services:
        for g in (svc.groups or []):
            if g not in groups:
                groups[g] = GroupSummary(name=g)
            gs = groups[g]
            gs.total += 1
            gs.services.append(svc.name)
            if results:
                st = _lookup_status(svc.name, results)
                if st == Status.UP:
                    gs.up += 1
                elif st == Status.DEGRADED:
                    gs.degraded += 1
                elif st == Status.DOWN:
                    gs.down += 1
                else:
                    gs.unknown += 1

    return sorted(groups.values(), key=lambda g: g.name)


def _lookup_status(service_name: str, results: list[CheckResult]) -> Status:
    """Return the Status for *service_name* from *results*, or UNKNOWN."""
    for r in results:
        if r.service_name == service_name:
            return r.status
    return Status.UNKNOWN


# ---------------------------------------------------------------------------
# Dependency graph utilities
# ----------------------------------------------------------------

def get_dependency_graph(
    services: list[ServiceConfig],
) -> dict[str, list[str]]:
    """Return a ``{name: [dep1, dep2]}`` adjacency dict.

    Only includes services that declare at least one dependency — services
    with no ``depends_on`` are omitted for brevity.
    """
    return {
        svc.name: list(svc.depends_on)
        for svc in services
        if svc.depends_on
    }


def topological_sort(services: list[ServiceConfig]) -> list[str]:
    """Return service names in dependency-first order.

    Services with no dependencies come first, then services whose deps
    have already been listed, and so on.  The graph is assumed acyclic
    (enforced by :func:`pulseboard.config._validate_dependency_graph`).

    This is useful for ordering a check run so that dependencies are
    checked *before* the services that depend on them.
    """
    by_name = {s.name: s for s in services}
    in_degree: dict[str, int] = {s.name: 0 for s in services}
    for svc in services:
        for dep in svc.depends_on:
            in_degree[svc.name] += 1

    queue: list[str] = [n for n, d in in_degree.items() if d == 0]
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        # Find services that depend on *node*
        for svc in services:
            if node in svc.depends_on:
                in_degree[svc.name] -= 1
                if in_degree[svc.name] == 0:
                    queue.append(svc.name)
    return order


# ---------------------------------------------------------------------------
# Dependency-impact application (runtime)
# ---------------------------------------------------------------------------

def apply_dependency_impact(
    services: list[ServiceConfig],
    results: list[CheckResult],
) -> list[CheckResult]:
    """Annotate and optionally downgrade check results based on dependencies.

    For each service that has ``depends_on`` entries, look up the status
    of each dependency in *results*.  If any dependency is not UP:

    - Write ``details["dependency_impact"]`` with a list of failing deps.
    - Downgrade the status: DEGRADED dependency -> DEGRADED (if currently UP),
      DOWN dependency -> DOWN.

    A service that was already DOWN is never upgraded.  The original
    status is preserved in ``details["original_status"]`` so downstream
    consumers can see both.

    Parameters
    ----------
    services:
        Parsed service list (for the dependency declarations).
    results:
        The mutable result list from a check run.  Modified in-place AND
        returned for convenience.

    Returns
    -------
    list[CheckResult]
        The same *results* list, mutated.
    """
    result_by_name = {r.service_name: r for r in results}

    for svc in services:
        if not svc.depends_on:
            continue

        result = result_by_name.get(svc.name)
        if result is None:
            continue

        failing_deps: list[dict[str, Any]] = []
        for dep_name in svc.depends_on:
            dep_result = result_by_name.get(dep_name)
            if dep_result is None:
                # Dependency not checked in this run — can't evaluate,
                # so we leave the dependent service's status unchanged.
                continue
            if dep_result.status != Status.UP:
                failing_deps.append({
                    "name": dep_name,
                    "status": dep_result.status.value,
                    "error": dep_result.error,
                })

        if not failing_deps:
            continue

        # There are failing dependencies — annotate and downgrade.
        result.details["dependency_impact"] = failing_deps
        result.details["original_status"] = result.status.value

        # Determine the worst dependency severity.
        worst_dep = Status.UP
        for fd in failing_deps:
            dep_st = Status(fd["status"])
            if dep_st == Status.DOWN:
                worst_dep = Status.DOWN
            elif dep_st == Status.DEGRADED and worst_dep != Status.DOWN:
                worst_dep = Status.DEGRADED

        if result.status == Status.UP:
            # UP can be downgraded to either DEGRADED or DOWN depending
            # on the worst dependency.
            result.status = worst_dep
        elif result.status == Status.DEGRADED and worst_dep == Status.DOWN:
            # Already DEGRADED but dependency is DOWN — escalate.
            result.status = Status.DOWN
        # If result.status is already DOWN, leave it.

    return results


def get_service_by_name(
    services: list[ServiceConfig], name: str,
) -> ServiceConfig | None:
    """Look up a service by name. Returns None if not found."""
    for s in services:
        if s.name == name:
            return s
    return None


def list_services_in_group(
    services: list[ServiceConfig], group: str,
) -> list[ServiceConfig]:
    """Return all services that belong to *group*."""
    return [s for s in services if group in (s.groups or [])]


def all_group_names(services: list[ServiceConfig]) -> list[str]:
    """Return sorted deduplicated list of all group names across services."""
    names: set[str] = set()
    for svc in services:
        names.update(svc.groups or [])
    return sorted(names)


def describe_dependency_graph(services: list[ServiceConfig]) -> str:
    """Return a human-readable multi-line description of the dependency graph.

    Useful for ``pulseboard groups --graph`` or debugging.
    Lines look like::

        API -> Database, Cache
        Database -> Redis
        Redis  (no dependencies)
    """
    by_name = {s.name: s for s in services}
    lines: list[str] = []
    order = topological_sort(services)
    for name in order:
        deps = by_name[name].depends_on
        if deps:
            lines.append(f"  {name} -> {', '.join(deps)}")
        else:
            lines.append(f"  {name}  (no dependencies)")
    return "\n".join(lines)
