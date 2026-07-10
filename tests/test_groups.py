"""Tests for the service groups & dependency-tracking module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from pulseboard import groups
from pulseboard.cli import cli
from pulseboard.groups import (
    GroupSummary,
    apply_dependency_impact,
    build_group_summaries,
    describe_dependency_graph,
    get_dependency_graph,
    get_service_by_name,
    list_services_in_group,
    all_group_names,
    topological_sort,
)
from pulseboard.models import CheckResult, ServiceConfig, Status
from pulseboard.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_service(name: str, **kwargs) -> ServiceConfig:
    """Build a minimal HTTP ServiceConfig with optional overrides."""
    defaults: dict = {"name": name, "url": f"https://{name}.example.com"}
    defaults.update(kwargs)
    return ServiceConfig(**defaults)


def _result(
    name: str,
    status: Status = Status.UP,
    error: str | None = None,
) -> CheckResult:
    """Build a CheckResult for testing."""
    return CheckResult(
        service_name=name,
        timestamp=datetime.now(timezone.utc),
        status=status,
        latency_ms=10.0,
        error=error,
    )


# ---------------------------------------------------------------------------
# GroupSummary
# ---------------------------------------------------------------------------


class TestGroupSummary:
    """Tests for the GroupSummary dataclass."""

    def test_empty_group_is_unknown(self) -> None:
        gs = GroupSummary(name="empty")
        assert gs.total == 0
        assert gs.status == Status.UNKNOWN
        assert gs.services == []

    def test_all_up_is_up(self) -> None:
        gs = GroupSummary(name="prod", total=3, up=3)
        assert gs.status == Status.UP

    def test_any_degraded_is_degraded(self) -> None:
        gs = GroupSummary(name="prod", total=3, up=2, degraded=1)
        assert gs.status == Status.DEGRADED

    def test_any_down_is_down_even_with_ups(self) -> None:
        gs = GroupSummary(name="prod", total=3, up=2, down=1)
        assert gs.status == Status.DOWN

    def test_down_takes_precedence_over_degraded(self) -> None:
        gs = GroupSummary(
            name="prod", total=4, up=1, degraded=1, down=1, unknown=1,
        )
        assert gs.status == Status.DOWN

    def test_only_unknowns_is_unknown(self) -> None:
        gs = GroupSummary(name="fog", total=3, unknown=3)
        assert gs.status == Status.UNKNOWN

    def test_unknown_with_up_is_still_up(self) -> None:
        gs = GroupSummary(name="mixed", total=3, up=2, unknown=1)
        assert gs.status == Status.UP

    def test_to_dict_is_jsonable_and_sorted(self) -> None:
        gs = GroupSummary(name="prod", total=3, up=2, down=1,
                          services=["zeta", "alpha"])
        d = gs.to_dict()
        assert d["name"] == "prod"
        assert d["up"] == 2
        assert d["down"] == 1
        assert d["status"] == Status.DOWN.value
        assert d["services"] == ["alpha", "zeta"]  # sorted
        # must be JSON-serializable
        json.dumps(d)


# ---------------------------------------------------------------------------
# build_group_summaries
# ---------------------------------------------------------------------------


class TestBuildGroupSummaries:
    def test_no_services_no_groups(self) -> None:
        assert build_group_summaries([]) == []

    def test_service_without_groups_produces_no_summaries(self) -> None:
        services = [_http_service("a")]
        assert build_group_summaries(services) == []

    def test_groups_count_membership(self) -> None:
        services = [
            _http_service("a", groups=["prod"]),
            _http_service("b", groups=["prod", "shared"]),
            _http_service("c", groups=["dev"]),
        ]
        summaries = {g.name: g for g in build_group_summaries(services)}
        assert summaries["prod"].total == 2
        assert "a" in summaries["prod"].services
        assert "b" in summaries["prod"].services
        assert summaries["shared"].total == 1
        assert summaries["dev"].total == 1

    def test_results_propagate_status(self) -> None:
        services = [
            _http_service("a", groups=["prod"]),
            _http_service("b", groups=["prod"]),
            _http_service("c", groups=["prod"]),
        ]
        results = [
            _result("a", Status.UP),
            _result("b", Status.DOWN, error="boom"),
            _result("c", Status.DEGRADED),
        ]
        [gs] = build_group_summaries(services, results)
        assert gs.total == 3
        assert gs.up == 1
        assert gs.down == 1
        assert gs.degraded == 1
        assert gs.unknown == 0
        assert gs.status == Status.DOWN

    def test_missing_result_counted_as_unknown(self) -> None:
        services = [
            _http_service("a", groups=["prod"]),
            _http_service("b", groups=["prod"]),
        ]
        results = [_result("a", Status.UP)]  # no result for b
        [gs] = build_group_summaries(services, results)
        assert gs.up == 1
        assert gs.unknown == 1

    def test_results_none_still_populates_membership(self) -> None:
        services = [
            _http_service("a", groups=["prod"]),
            _http_service("b", groups=["dev"]),
        ]
        out = build_group_summaries(services, None)
        names = [g.name for g in out]
        assert sorted(names) == ["dev", "prod"]
        # Without results, counts are zeroed.
        assert all(g.total >= 1 for g in out)
        assert all(g.up == 0 for g in out)
        assert all(g.status in (Status.UP, Status.UNKNOWN) for g in out)

    def test_summary_sorted_by_name(self) -> None:
        services = [
            _http_service("z", groups=["zebra"]),
            _http_service("a", groups=["alpha"]),
            _http_service("m", groups=["mango"]),
        ]
        out = [g.name for g in build_group_summaries(services)]
        assert out == ["alpha", "mango", "zebra"]


# ---------------------------------------------------------------------------
# Dependency graph utilities
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_get_dependency_graph_omits_no_deps(self) -> None:
        services = [
            _http_service("a", depends_on=["b"]),
            _http_service("b"),
        ]
        graph = get_dependency_graph(services)
        assert graph == {"a": ["b"]}

    def test_get_dependency_graph_multiple_deps(self) -> None:
        services = [
            _http_service("a", depends_on=["b", "c"]),
            _http_service("b"),
            _http_service("c"),
        ]
        graph = get_dependency_graph(services)
        assert graph == {"a": ["b", "c"]}

    def test_get_dependency_graph_all_independent(self) -> None:
        services = [_http_service("a"), _http_service("b")]
        assert get_dependency_graph(services) == {}

    def test_topological_sort_no_deps(self) -> None:
        services = [_http_service("x"), _http_service("y"), _http_service("z")]
        order = topological_sort(services)
        assert sorted(order) == ["x", "y", "z"]

    def test_topological_sort_dependency_first(self) -> None:
        services = [
            _http_service("api", depends_on=["db"]),
            _http_service("db", depends_on=["redis"]),
            _http_service("redis"),
        ]
        order = topological_sort(services)
        # Deps must come before dependents.
        assert order.index("redis") < order.index("db")
        assert order.index("db") < order.index("api")

    def test_topological_sort_multiple_independent_chains(self) -> None:
        services = [
            _http_service("a1", depends_on=["a0"]),
            _http_service("a0"),
            _http_service("b1", depends_on=["b0"]),
            _http_service("b0"),
        ]
        order = topological_sort(services)
        assert order.index("a0") < order.index("a1")
        assert order.index("b0") < order.index("b1")

    def test_describe_dependency_graph_contains_arrows(self) -> None:
        services = [
            _http_service("api", depends_on=["db"]),
            _http_service("db", depends_on=["redis"]),
            _http_service("redis"),
            _http_service("web"),
        ]
        desc = describe_dependency_graph(services)
        assert "api -> db" in desc
        assert "db -> redis" in desc
        assert "redis  (no dependencies)" in desc
        assert "web  (no dependencies)" in desc
        # Sorted topologically.
        lines = desc.splitlines()
        assert lines[0].startswith("  redis")
        assert lines[-1].startswith("  api")


# ---------------------------------------------------------------------------
# apply_dependency_impact
# ---------------------------------------------------------------------------


class TestApplyDependencyImpact:
    def test_no_dependencies_does_nothing(self) -> None:
        services = [_http_service("a"), _http_service("b")]
        results = [_result("a", Status.UP), _result("b", Status.DOWN)]
        before = [r.status for r in results]
        apply_dependency_impact(services, results)
        # Identical statuses; no details added.
        assert [r.status for r in results] == before
        for r in results:
            assert "dependency_impact" not in r.details

    def test_up_dep_leaves_dependent_up(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.UP),
            _result("db", Status.UP),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.UP
        assert "dependency_impact" not in results[0].details

    def test_down_dep_downgrades_up_to_down(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.UP),
            _result("db", Status.DOWN, error="boom"),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.DOWN
        assert "dependency_impact" in results[0].details
        impact = results[0].details["dependency_impact"]
        assert impact == [{"name": "db", "status": "down", "error": "boom"}]
        assert results[0].details["original_status"] == "up"

    def test_degraded_dep_downgrades_up_to_degraded_only(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.UP),
            _result("db", Status.DEGRADED),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.DEGRADED
        assert "dependency_impact" in results[0].details

    def test_already_down_is_never_upgraded(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.DOWN, error="own-failure"),
            _result("db", Status.UP),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.DOWN
        # No downgrade happened, so no annotation should be added.
        assert "dependency_impact" not in results[0].details

    def test_already_degraded_with_down_dep_escalates_to_down(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.DEGRADED),
            _result("db", Status.DOWN),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.DOWN
        assert results[0].details["original_status"] == "degraded"

    def test_already_degraded_with_degraded_dep_stays_degraded(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [
            _result("api", Status.DEGRADED),
            _result("db", Status.DEGRADED),
        ]
        apply_dependency_impact(services, results)
        assert results[0].status == Status.DEGRADED

    def test_multiple_deps_worst_wins(self) -> None:
        services = [
            _http_service("api", depends_on=["cache", "db"]),
        ]
        results = [
            _result("api", Status.UP),
            _result("cache", Status.DEGRADED),
            _result("db", Status.DOWN),
        ]
        apply_dependency_impact(services, results)
        # worst is DOWN → result is DOWN.
        assert results[0].status == Status.DOWN
        impact = results[0].details["dependency_impact"]
        assert len(impact) == 2
        names = sorted(d["name"] for d in impact)
        assert names == ["cache", "db"]

    def test_missing_dependency_is_skipped(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [_result("api", Status.UP)]
        # No result for db.
        apply_dependency_impact(services, results)
        # Status not changed, no annotation added.
        assert results[0].status == Status.UP
        assert "dependency_impact" not in results[0].details

    def test_returns_same_list(self) -> None:
        services = [_http_service("api", depends_on=["db"])]
        results = [_result("api", Status.UP), _result("db", Status.UP)]
        out = apply_dependency_impact(services, results)
        assert out is results

    def test_dependent_with_multiple_levels_only_uses_immediate_deps(self) -> None:
        # api -> web -> db. Even though db is also down, we don't cascade.
        # We only look at the immediate dep "web" in this rule set.
        services = [
            _http_service("api", depends_on=["web"]),
            _http_service("web", depends_on=["db"]),
        ]
        results = [
            _result("api", Status.UP),
            _result("web", Status.UP),
            _result("db", Status.DOWN),
        ]
        apply_dependency_impact(services, results)
        # api only cares about web's status (UP), not db's.
        assert results[0].status == Status.UP
        assert "dependency_impact" not in results[0].details


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestLookupHelpers:
    def test_get_service_by_name_found(self) -> None:
        services = [_http_service("a"), _http_service("b")]
        assert get_service_by_name(services, "b").name == "b"

    def test_get_service_by_name_missing(self) -> None:
        services = [_http_service("a")]
        assert get_service_by_name(services, "missing") is None

    def test_list_services_in_group(self) -> None:
        services = [
            _http_service("a", groups=["prod"]),
            _http_service("b", groups=["prod"]),
            _http_service("c", groups=["dev"]),
        ]
        prod = list_services_in_group(services, "prod")
        assert [s.name for s in prod] == ["a", "b"]
        assert list_services_in_group(services, "missing") == []

    def test_all_group_names(self) -> None:
        services = [
            _http_service("a", groups=["prod", "backend"]),
            _http_service("b", groups=["dev"]),
            _http_service("c", groups=["backend"]),
            _http_service("d"),  # no groups
        ]
        names = all_group_names(services)
        assert names == ["backend", "dev", "prod"]


# ---------------------------------------------------------------------------
# CLI: `pulseboard groups`
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, services: list[dict]) -> Path:
    cfg = {
        "settings": {"db_path": str(tmp_path / "pulseboard.db")},
        "services": services,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


class TestGroupsCommand:
    """Behaviour tests for the `pulseboard groups` CLI command."""

    def test_help_lists_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--group" in result.output
        assert "--graph" in result.output

    def test_missing_config_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "no such file" in result.output.lower() or "config" in result.output.lower()

    def test_no_services_reports_empty(self, tmp_path: Path) -> None:
        cfg = _write_config(tmp_path, [])
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg)])
        assert result.exit_code == 0
        assert "no services" in result.output.lower() or "no groups" in result.output.lower()

    def test_groups_listed_in_table(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            [
                {"name": "api", "url": "https://api.example.com", "groups": ["production"]},
                {"name": "blog", "url": "https://blog.example.com", "groups": ["production", "external"]},
                {"name": "db", "url": "https://db.example.com", "groups": ["infra"]},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg)])
        assert result.exit_code == 0
        # Group names appear.
        for name in ("production", "external", "infra"):
            assert name in result.output
        # At least one service name appears.
        for svc in ("api", "blog", "db"):
            assert svc in result.output

    def test_graph_mode_shows_arrows(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            [
                {"name": "api", "url": "https://api.example.com", "depends_on": ["db"]},
                {"name": "db", "url": "https://db.example.com", "depends_on": ["redis"]},
                {"name": "redis", "url": "https://redis.example.com"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg), "--graph"])
        assert result.exit_code == 0
        assert "api -> db" in result.output
        assert "db -> redis" in result.output
        assert "redis  (no dependencies)" in result.output

    def test_json_output_is_valid(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            [
                {"name": "api", "url": "https://api.example.com", "groups": ["prod"]},
                {"name": "db", "url": "https://db.example.com", "groups": ["prod"]},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "groups" in payload
        names = [g["name"] for g in payload["groups"]]
        assert "prod" in names
        prod = next(g for g in payload["groups"] if g["name"] == "prod")
        assert sorted(prod["services"]) == ["api", "db"]

    def test_group_filter_shows_only_matching(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            [
                {"name": "api", "url": "https://api.example.com", "groups": ["prod"]},
                {"name": "db", "url": "https://db.example.com", "groups": ["prod"]},
                {"name": "docs", "url": "https://docs.example.com", "groups": ["docs"]},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg), "--group", "docs", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        names = [g["name"] for g in payload["groups"]]
        assert names == ["docs"]
        assert payload["groups"][0]["services"] == ["docs"]

    def test_group_filter_unknown_group_returns_empty(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            [{"name": "api", "url": "https://api.example.com", "groups": ["prod"]}],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "-c", str(cfg), "--group", "ghost"])
        assert result.exit_code == 0
        assert "no services match" in result.output.lower() or "no groups" in result.output.lower()
