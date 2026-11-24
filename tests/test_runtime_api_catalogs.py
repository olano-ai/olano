"""Endpoint tests for the agent-creation catalogs.

`/api/archetypes` and `/api/integrations` back the dashboard's New-agent and
Add-subagent pickers and had no coverage at all, which is how a box serving 5
bundled experts instead of the pack's 187 could look healthy. These assert the
contract the pickers rely on: a populated, well-shaped payload, served without
blocking the event loop.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)

    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.archetypes import catalog_index
    from olano.runtime.api import create_api

    catalog_index.invalidate()
    gw = MagicMock()
    gw._runners = {}
    gw._channels = []
    yield TestClient(create_api(gw))
    catalog_index.invalidate()


# ── /api/archetypes ─────────────────────────────────────────────────────────


def test_archetypes_returns_both_halves_populated(api_client):
    """The bundled templates alone must fill both the Team and Expert tabs."""
    resp = api_client.get("/api/archetypes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["teams"], "no teams — the Team tab would render as empty"
    assert data["specialists"], "no specialists — the Expert tab would render as empty"


def test_archetype_entries_carry_every_field_the_picker_reads(api_client):
    data = api_client.get("/api/archetypes").json()
    for entry in data["teams"] + data["specialists"]:
        for field in ("name", "label", "description", "emoji", "color", "category", "model"):
            assert field in entry, f"{entry.get('name')} missing {field}"
        assert entry["name"] and entry["label"]
    for team in data["teams"]:
        # What renders the subagent-count badge; a team without it is a team
        # the picker cannot distinguish from an expert.
        assert team["subagent_count"] >= 1


def test_every_listed_archetype_is_installable(api_client):
    """A picker entry whose name doesn't resolve installs nothing.

    Names come from the file's `name` field but installs resolve by name *or*
    filename stem, so this pins the two together.
    """
    from olano.archetypes.installer import find_archetype_path

    data = api_client.get("/api/archetypes").json()
    unresolvable = [
        e["name"]
        for e in data["teams"] + data["specialists"]
        if find_archetype_path(e["name"]) is None
    ]
    assert not unresolvable, f"listed but not installable: {unresolvable}"


def test_every_listed_archetype_actually_loads(api_client):
    """Resolving is not enough — the installed config must also validate.

    Three bundled team templates (lionDev/lionFinance/lionMarketing) shipped
    referencing specialists that exist only in the canonical `olano-agents`
    pack. On any box without it they resolved, installed cleanly, and then
    raised `ValidationError` on load, so the picker offered agents that could
    never boot. Nothing tested the step between "resolves" and "runs".
    """
    from olano.archetypes.installer import _load_archetype_file, find_archetype_path
    from olano.config.agent_config import AgentConfig

    data = api_client.get("/api/archetypes").json()
    broken: list[str] = []
    for entry in data["teams"] + data["specialists"]:
        path = find_archetype_path(entry["name"])
        assert path is not None
        raw = dict(_load_archetype_file(path))
        raw.setdefault("name", entry["name"])
        try:
            AgentConfig.model_validate(raw)
        except Exception as exc:
            broken.append(f"{entry['name']}: {type(exc).__name__}")
    assert not broken, f"listed but cannot be loaded: {broken}"


def test_archetypes_refresh_flag_rebuilds(api_client):
    assert api_client.get("/api/archetypes?refresh=true").status_code == 200


def test_archetypes_does_not_block_the_event_loop(api_client):
    """The handler must hand its work to a thread.

    With the canonical pack installed a cold rebuild opens 202 files; running
    that inline froze streaming, `/api/status` polling and every other request
    behind it for the duration.
    """
    import olano.archetypes.catalog_index as ci
    from olano.runtime import api as api_mod

    calls: list[bool] = []
    real_to_thread = asyncio.to_thread

    async def _tracking(fn, *a, **kw):
        if fn is ci.get_archetype_catalog:
            calls.append(True)
        return await real_to_thread(fn, *a, **kw)

    api_mod.asyncio.to_thread = _tracking  # type: ignore[assignment]
    try:
        assert api_client.get("/api/archetypes").status_code == 200
    finally:
        api_mod.asyncio.to_thread = real_to_thread  # type: ignore[assignment]
    assert calls, "get_archetype_catalog ran on the event loop"


# ── /api/integrations ───────────────────────────────────────────────────────


def test_integrations_are_populated(api_client):
    resp = api_client.get("/api/integrations")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == len(data["integrations"])
    # Auto-generated from the toolkit registry minus the hidden infra toolkits —
    # a handful would mean the registry import silently failed.
    assert data["count"] > 20, f"only {data['count']} integrations"


def test_integration_entries_carry_every_field_the_picker_reads(api_client):
    for entry in api_client.get("/api/integrations").json()["integrations"]:
        for field in ("name", "toolkit", "label", "description", "category", "writes"):
            assert field in entry, f"{entry.get('name')} missing {field}"


def test_hidden_infrastructure_toolkits_stay_out_of_the_catalog(api_client):
    from olano.subagents._defaults import _CATALOG_HIDDEN

    toolkits = {e["toolkit"] for e in api_client.get("/api/integrations").json()["integrations"]}
    assert not (toolkits & _CATALOG_HIDDEN)


# ── /api/catalog/status ─────────────────────────────────────────────────────


def test_catalog_status_reports_the_archetype_catalog(api_client):
    """Makes "only five experts" diagnosable without shell access to the box."""
    api_client.get("/api/archetypes")
    status = api_client.get("/api/catalog/status").json()["catalogs"]["archetypes"]
    assert status["source"] in {"memory", "disk", "none"}
    assert "pack_installed" in status
    assert status["specialists"] >= 1
    assert status["search_dirs"]
