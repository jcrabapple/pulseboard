"""Tests for the GitHub Actions CI workflow.

These tests validate the workflow YAML structure without running GitHub
Actions itself — they parse the file and assert that the workflow will
run pytest on every push to the main branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "ci.yml"
)


@pytest.fixture
def workflow() -> dict:
    """Load and parse the CI workflow YAML."""
    if not WORKFLOW_PATH.exists():
        pytest.fail(
            f"CI workflow not found at {WORKFLOW_PATH}. "
            "Create .github/workflows/ci.yml"
        )
    with open(WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


class TestCIWorkflow:
    """The CI workflow must run the test suite on every push."""

    def test_workflow_file_exists(self):
        assert WORKFLOW_PATH.exists(), (
            "GitHub Actions workflow missing: .github/workflows/ci.yml"
        )

    def test_workflow_has_valid_yaml(self, workflow):
        assert isinstance(workflow, dict)
        assert workflow.get("name"), "Workflow must have a name"

    def test_triggers_on_push_to_main(self, workflow):
        on = workflow.get(True) or workflow.get("on")
        assert on is not None, "Workflow must define 'on' triggers"
        push = on.get("push")
        assert push is not None, "Workflow must trigger on push"
        branches = push.get("branches", []) if isinstance(push, dict) else []
        assert "master" in branches, "Workflow must run on push to master"

    def test_triggers_on_pull_request(self, workflow):
        on = workflow.get(True) or workflow.get("on")
        assert on is not None
        pr = on.get("pull_request")
        assert pr is not None, "Workflow should trigger on pull requests"

    def test_runs_on_ubuntu(self, workflow):
        jobs = workflow.get("jobs")
        assert jobs, "Workflow must define at least one job"
        test_job = jobs.get("test")
        assert test_job is not None, "Workflow must have a 'test' job"
        runs_on = test_job.get("runs-on")
        assert runs_on == "ubuntu-latest", (
            f"Expected runs-on: ubuntu-latest, got {runs_on}"
        )

    def test_test_job_installs_dependencies(self, workflow):
        test_job = workflow["jobs"]["test"]
        steps = test_job.get("steps", [])
        step_names = " ".join(
            str(s.get("name", "")) + " " + " ".join(str(v) for v in s.values())
            for s in steps
        )
        assert "pip install" in step_names or "uv" in step_names.lower() or "hatch" in step_names.lower(), (
            "Workflow must install project dependencies"
        )

    def test_test_job_runs_pytest(self, workflow):
        test_job = workflow["jobs"]["test"]
        steps = test_job.get("steps", [])
        all_commands = " ".join(
            str(s.get("run", "")) for s in steps if s.get("run")
        )
        assert "pytest" in all_commands, (
            "Workflow must run 'pytest' as part of the test job"
        )

    def test_checks_out_repository(self, workflow):
        test_job = workflow["jobs"]["test"]
        steps = test_job.get("steps", [])
        uses = [s.get("uses", "") for s in steps if s.get("uses")]
        assert any("actions/checkout" in u for u in uses), (
            "Workflow must use actions/checkout"
        )

    def test_sets_up_python(self, workflow):
        test_job = workflow["jobs"]["test"]
        steps = test_job.get("steps", [])
        uses = [s.get("uses", "") for s in steps if s.get("uses")]
        assert any("setup-python" in u for u in uses), (
            "Workflow must set up Python via actions/setup-python"
        )
