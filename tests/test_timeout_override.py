"""Tests for per-check CLI timeout override.

The ``pulseboard check`` command accepts ``--timeout N`` to override the
config-level timeout for the current run.  This is useful for ad-hoc
debugging:  e.g. ``pulseboard check --timeout 30`` when a target is
slow and the default 10 s timeout is too aggressive.

Tests are written first (RED) — the feature does not exist yet.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pulseboard.cli import cli
from pulseboard.models import ServiceConfig, Status


# ---------------------------------------------------------------------------
# Helper: write a minimal config to a tmp file
# ---------------------------------------------------------------------------

def _write_config(tmp_path, *, timeout=None):
    config = tmp_path / "pulseboard.yaml"
    lines = ["services:", "  - name: Svc", "    url: https://example.com"]
    if timeout is not None:
        lines.append(f"    timeout: {timeout}")
    config.write_text("\n".join(lines) + "\n")
    return config


# ---------------------------------------------------------------------------
# --timeout override works
# ---------------------------------------------------------------------------

def test_check_timeout_override_applies_to_all_services(tmp_path):
    """``--timeout 25`` should override every service's timeout."""
    config = _write_config(tmp_path, timeout=10)

    captured = {}

    def fake_run(services):
        captured["timeouts"] = [s.timeout for s in services]
        return []

    import pulseboard.cli as cli_mod
    with patch.object(cli_mod, "run_all_checks_with_thresholds", side_effect=fake_run):
        result = CliRunner().invoke(cli, ["check", "-c", str(config), "--timeout", "25"])

    assert result.exit_code == 0, result.output
    assert captured.get("timeouts") == [25], captured.get("timeouts")


def test_check_timeout_override_uses_config_default_when_absent(tmp_path):
    """Without ``--timeout``, the config-level timeout is preserved."""
    config = _write_config(tmp_path, timeout=7)

    captured = {}

    def fake_run(services):
        captured["timeouts"] = [s.timeout for s in services]
        return []

    import pulseboard.cli as cli_mod
    with patch.object(cli_mod, "run_all_checks_with_thresholds", side_effect=fake_run):
        result = CliRunner().invoke(cli, ["check", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert captured.get("timeouts") == [7], captured.get("timeouts")


def test_check_timeout_override_rejects_zero(tmp_path):
    """A timeout of 0 should be rejected at CLI parse time."""
    config = _write_config(tmp_path)

    captured = {}

    def fake_run(services):
        captured["timeouts"] = [s.timeout for s in services]
        return []

    import pulseboard.cli as cli_mod
    with patch.object(cli_mod, "run_all_checks_with_thresholds", side_effect=fake_run):
        result = CliRunner().invoke(cli, ["check", "-c", str(config), "--timeout", "0"])

    # Should fail clearly (not silently pass a 0 timeout to httpx).
    assert result.exit_code != 0
    assert "timeout" in result.output.lower()
    # And it should not have called the underlying runner if rejected.
    assert captured.get("timeouts") is None


def test_check_timeout_option_is_documented_in_help(tmp_path):
    """``pulseboard check --help`` should mention ``--timeout``.

    This guards against the option being silently removed later.
    """
    result = CliRunner().invoke(cli, ["check", "--help"])
    assert result.exit_code == 0
    assert "--timeout" in result.output
