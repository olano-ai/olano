"""Tests for the all-agents system-zone refresh (`olano agents refresh`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.archetypes.installer import preview_system_zone, scaffold_agent_workspace
from olano.config.loader import load_agent_config
from olano.update.refresh import (
    _template_from_config,
    refresh_agent_system_zone,
    refresh_all_agents,
)


def _make_agent(home: Path, name: str) -> tuple[Path, dict]:
    """Create a minimal agent (config + scaffolded workspace) under a home.

    The workspace is scaffolded with exactly the template a refresh regenerates
    (``_template_from_config`` of the loaded config), so an unmodified workspace
    re-syncs to no changes.
    """
    agent_dir = home / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": name,
                "model": "anthropic:claude-sonnet-4-6",
                "system_prompt": "You are a test agent.",
                "description": "Test agent",
                "emoji": "🦁",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    template = _template_from_config(load_agent_config(name, source="home"))
    workspace_root = agent_dir / "workspace"
    scaffold_agent_workspace(name, template, workspace_root)
    return workspace_root, template


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(h))
    return h


# ─────────────────────────────────────── preview (dry-run) ───────────────────


def test_preview_system_zone_reports_changes_without_writing(home: Path) -> None:
    ws, template = _make_agent(home, "merli")
    eb = ws / "system" / "prompts" / "EXECUTION_BIAS.md"
    eb.unlink()  # simulate a stale/missing system file

    changed = preview_system_zone(ws, agent_name="merli", template=template)

    assert "prompts/EXECUTION_BIAS.md" in changed
    assert not eb.exists()  # preview must not write to the real workspace


def test_preview_clean_workspace_reports_no_changes(home: Path) -> None:
    ws, template = _make_agent(home, "merli")
    # Freshly scaffolded → system zone already matches the bundled content.
    assert preview_system_zone(ws, agent_name="merli", template=template) == []


# ─────────────────────────────────────── single-agent refresh ────────────────


def test_refresh_agent_check_does_not_write(home: Path) -> None:
    ws, _template = _make_agent(home, "merli")
    eb = ws / "system" / "prompts" / "EXECUTION_BIAS.md"
    eb.unlink()

    result = refresh_agent_system_zone("merli", source="home", check=True)

    assert "prompts/EXECUTION_BIAS.md" in result.changed
    assert not eb.exists()
    assert result.error is None


def test_refresh_agent_rewrites_and_emits_pulse(home: Path) -> None:
    ws, _template = _make_agent(home, "merli")
    eb = ws / "system" / "prompts" / "EXECUTION_BIAS.md"
    eb.unlink()

    result = refresh_agent_system_zone("merli", source="home", check=False)

    assert "prompts/EXECUTION_BIAS.md" in result.changed
    assert eb.exists()  # restored
    # A prompts/ change is a notable prefix → a PULSE review item is queued.
    assert result.pulse_id is not None


def test_refresh_agent_clean_is_up_to_date(home: Path) -> None:
    _make_agent(home, "merli")
    result = refresh_agent_system_zone("merli", source="home", check=False)
    assert result.changed == []
    assert result.pulse_id is None


# ─────────────────────────────────────── all-agents sweep ────────────────────


def test_refresh_all_agents_covers_every_agent(home: Path) -> None:
    ws_a, _template = _make_agent(home, "alpha")
    _make_agent(home, "beta")
    (ws_a / "system" / "prompts" / "EXECUTION_BIAS.md").unlink()

    results = refresh_all_agents(source="home", check=False)

    by_name = {r.agent: r for r in results}
    assert set(by_name) == {"alpha", "beta"}
    assert "prompts/EXECUTION_BIAS.md" in by_name["alpha"].changed
    assert by_name["beta"].changed == []


def test_refresh_all_agents_survives_one_broken_config(home: Path) -> None:
    _make_agent(home, "good")
    broken = home / "agents" / "broken"
    broken.mkdir(parents=True)
    (broken / "agent.json5").write_text("{ this is not valid json5 ", encoding="utf-8")

    results = refresh_all_agents(source="home", check=False)

    by_name = {r.agent: r for r in results}
    assert by_name["broken"].error is not None
    assert by_name["good"].error is None  # the good agent still refreshed


# ─────────────────────────────────────── post-upgrade wiring ─────────────────


def test_post_upgrade_refreshes_all_agents(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.skills.built_in_sync import SyncReport
    from olano.update import _run_post_upgrade

    _make_agent(home, "merli")
    monkeypatch.setattr(
        "olano.skills.built_in_sync.sync_builtin_skills",
        lambda _home: SyncReport(0, 0, 0, 0, 0),
    )
    monkeypatch.setattr(
        "olano.migrations.run_pending_migrations",
        lambda _home, _applied: [],
    )

    skills, migrations, agents_refreshed = _run_post_upgrade(home)

    assert migrations == []
    assert [r.agent for r in agents_refreshed] == ["merli"]
