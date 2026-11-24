"""Tests for the model catalog picker behavior and --reconfigure CLI flag."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from olano.cli.main import cli
from olano.models.catalog import (
    filter_catalog,
    load_catalog,
    recommended_entries,
)

# ─────────────────────────────────────────────────────────────────
# Catalog filter (simulates the picker filter box)
# ─────────────────────────────────────────────────────────────────


def test_filter_narrows_list():
    catalog = load_catalog()
    filtered = filter_catalog(catalog, "sonnet")
    assert 0 < len(filtered) < len(catalog)


def test_filter_case_insensitive():
    catalog = load_catalog()
    lower = filter_catalog(catalog, "sonnet")
    upper = filter_catalog(catalog, "SONNET")
    assert lower == upper


def test_filter_empty_returns_all():
    catalog = load_catalog()
    assert filter_catalog(catalog, "") == catalog


def test_recommended_group_exists():
    recs = recommended_entries()
    assert len(recs) >= 2


def test_cloud_model_has_require_keys():
    catalog = load_catalog()
    anthropic = [e for e in catalog if e.provider == "anthropic"]
    assert anthropic
    for e in anthropic:
        assert e.requires_keys, f"{e.name} should require ANTHROPIC_API_KEY"


def test_local_model_no_require_keys():
    catalog = load_catalog()
    local = [e for e in catalog if e.local]
    if local:
        for e in local:
            assert e.requires_keys == (), f"{e.name} should not require keys"


# ─────────────────────────────────────────────────────────────────
# --reconfigure flag wiring (CLI surface)
# ─────────────────────────────────────────────────────────────────


def test_setup_reconfigure_flag_exists(tmp_path: Path):
    """--reconfigure is a valid CLI flag and doesn't crash immediately."""
    runner = CliRunner()
    # With --yes the wizard runs non-interactively; combine with --reconfigure
    # but we need a minimal existing agent state. Just verify the flag is parsed.
    result = runner.invoke(
        cli,
        ["--home", str(tmp_path), "setup", "--reconfigure", "--yes", "--skip-verify"],
    )
    # May succeed or fail based on archetype load; what matters is the flag is accepted
    assert result.exit_code in (0, 1)
    assert "Error: No such option: --reconfigure" not in (result.output or "")


def test_setup_yes_skip_verify_exits_zero(tmp_path: Path):
    """Non-interactive setup should succeed with mocked connectivity."""
    runner = CliRunner()
    with (
        patch("olano.cli.main._probe_model_connectivity", return_value=(True, "ok")),
        patch("olano.skills.built_in_sync.sync_builtin_skills"),
    ):
        result = runner.invoke(
            cli,
            [
                "--home",
                str(tmp_path),
                "setup",
                "--yes",
                "--skip-verify",
                "--provider",
                "anthropic",
                "--model",
                "anthropic:claude-sonnet-4-6",
            ],
        )
    assert result.exit_code == 0


def test_doctor_model_probe_prints_text_not_content_blocks(monkeypatch):
    """A list-content reply (OpenAI Responses, Anthropic) shows as its text."""
    from types import SimpleNamespace

    from olano.cli import main as cli_main

    reply = SimpleNamespace(
        content=[
            {"type": "reasoning", "summary": []},
            {"type": "text", "text": "OK", "annotations": [], "id": "msg_1"},
        ]
    )
    model = SimpleNamespace(invoke=lambda _prompt: reply)
    monkeypatch.setattr("olano.models.build_model", lambda _name: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ok, detail = cli_main._probe_model_connectivity("openai:gpt-5.4-mini")

    assert ok is True
    assert detail == "OK"
