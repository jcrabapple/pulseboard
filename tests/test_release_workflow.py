"""Tests for the GitHub Actions release workflow.

Validates that a release-on-tag workflow exists and is structured to
build the distribution artifacts and create a GitHub Release when a tag
is pushed. Mirrors the style of ``test_ci_workflow.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


RELEASE_WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "release.yml"
)


@pytest.fixture
def workflow() -> dict:
    """Load and parse the release workflow YAML."""
    if not RELEASE_WORKFLOW_PATH.exists():
        pytest.fail(
            f"Release workflow not found at {RELEASE_WORKFLOW_PATH}. "
            "Create .github/workflows/release.yml"
        )
    with open(RELEASE_WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


class TestReleaseWorkflow:
    """The release workflow must build artifacts on tag push."""

    def test_workflow_file_exists(self):
        assert RELEASE_WORKFLOW_PATH.exists(), (
            "GitHub Actions release workflow missing: "
            ".github/workflows/release.yml"
        )

    def test_workflow_has_valid_yaml(self, workflow):
        assert isinstance(workflow, dict)
        assert workflow.get("name"), "Release workflow must have a name"

    def test_triggers_on_tag_push(self, workflow):
        on = workflow.get(True) or workflow.get("on")
        assert on is not None, "Workflow must define 'on' triggers"
        push = on.get("push")
        assert push is not None, "Workflow must trigger on push"
        # Tag pushes are expressed as ``tags: ['*']`` (or a pattern).
        tags = push.get("tags", push.get("branches")) if isinstance(push, dict) else []
        assert tags, (
            "Release workflow must trigger on tag push "
            "(expected 'push.tags' in triggers)"
        )

    def test_runs_on_ubuntu(self, workflow):
        jobs = workflow.get("jobs")
        assert jobs, "Workflow must define at least one job"
        release_job = jobs.get("release")
        assert release_job is not None, "Workflow must have a 'release' job"
        runs_on = release_job.get("runs-on")
        assert runs_on == "ubuntu-latest", (
            f"Expected runs-on: ubuntu-latest, got {runs_on}"
        )

    def test_checks_out_repository(self, workflow):
        release_job = workflow["jobs"]["release"]
        steps = release_job.get("steps", [])
        uses = [s.get("uses", "") for s in steps if s.get("uses")]
        assert any("actions/checkout" in u for u in uses), (
            "Workflow must use actions/checkout"
        )

    def test_sets_up_python(self, workflow):
        release_job = workflow["jobs"]["release"]
        steps = release_job.get("steps", [])
        uses = [s.get("uses", "") for s in steps if s.get("uses")]
        assert any("setup-python" in u for u in uses), (
            "Workflow must set up Python via actions/setup-python"
        )

    def test_builds_distribution(self, workflow):
        release_job = workflow["jobs"]["release"]
        steps = release_job.get("steps", [])
        all_commands = " ".join(
            str(s.get("run", "")) for s in steps if s.get("run")
        )
        assert any(
            term in all_commands for term in ("python -m build", "python -m build", "hatch build", "pyproject-build")
        ), (
            "Release workflow must build the distribution "
            "(expected 'python -m build' or equivalent)"
        )

    def test_uploads_release_artifact(self, workflow):
        release_job = workflow["jobs"]["release"]
        steps = release_job.get("steps", [])
        uses = [s.get("uses", "") for s in steps if s.get("uses")]
        # Either softprops/action-gh-release or actions/upload-artifact
        assert (
            any("action-gh-release" in u for u in uses)
            or any("upload-artifact" in u for u in uses)
        ), (
            "Release workflow must upload the built artifacts "
            "(expected softprops/action-gh-release or "
            "actions/upload-artifact)"
        )
