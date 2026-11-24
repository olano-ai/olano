"""Per-member overrides on catalog-sourced subagents.

A `{"from_archetype": ref}` stub (and a `builtin_subagents` dict entry) can
carry `model`, `system_prompt`, and `system_prompt_extension` next to the ref:
the AgentConfig validator merges them over the catalog defaults, so the ref
stays live — catalog updates keep landing — while the owner's choices ride on
top. The API writes exactly those stubs (never a resolved copy), and team
installs accept per-member models the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.archetypes import installer
from olano.config.agent_config import AgentConfig


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """A minimal archetype pack: one expert, one team referencing it."""
    ext = tmp_path / "pack"
    ext.mkdir()
    (ext / "eng-backend.json5").write_text(
        json.dumps(
            {
                "name": "eng-backend",
                "label": "Backend Developer",
                "description": "Builds APIs.",
                "system_prompt": "You are Backend, an API specialist.",
                "model": "default:turbo",
                "tools": ["safe_shell"],
            }
        ),
        encoding="utf-8",
    )
    (ext / "lionTest.json5").write_text(
        json.dumps(
            {
                "name": "lionTest",
                "label": "LionTest",
                "description": "A team.",
                "system_prompt": "You are the supervisor.",
                "model": "default:pro",
                "subagents": [{"from_archetype": "eng-backend"}],
                "builtin_subagents": ["ops"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_archetype_search_dirs", lambda: [ext])
    installer._FILE_CACHE.clear()
    yield ext
    installer._FILE_CACHE.clear()


# ── AgentConfig merge semantics ──────────────────────────────────────────────


def test_overrides_next_to_the_ref_win_over_the_archetype(pack) -> None:
    cfg = AgentConfig(
        name="team",
        subagents=[
            {"from_archetype": "eng-backend", "model": "fake:instant", "tools": ["web_search"]}
        ],
    )
    sub = cfg.subagents[0]
    assert sub.model == "fake:instant"
    assert sub.tools == ["web_search"]
    assert sub.from_archetype == "eng-backend"  # provenance preserved
    assert sub.system_prompt == "You are Backend, an API specialist."


def test_extension_appends_to_the_live_archetype_prompt(pack) -> None:
    cfg = AgentConfig(
        name="team",
        subagents=[
            {"from_archetype": "eng-backend", "system_prompt_extension": "Always answer in CSV."}
        ],
    )
    prompt = cfg.subagents[0].system_prompt
    assert prompt.startswith("You are Backend, an API specialist.")
    assert "## Additional owner instructions" in prompt
    assert prompt.endswith("Always answer in CSV.")


def test_extension_composition_is_idempotent_on_revalidation(pack) -> None:
    """A resolved config fed back through validation must not append twice."""
    cfg = AgentConfig(
        name="team",
        subagents=[{"from_archetype": "eng-backend", "system_prompt_extension": "Be terse."}],
    )
    again = AgentConfig(**cfg.model_dump())
    assert again.subagents[0].system_prompt == cfg.subagents[0].system_prompt
    assert again.subagents[0].system_prompt.count("## Additional owner instructions") == 1


def test_extension_appends_to_an_explicit_prompt_override(pack) -> None:
    """Detach + append compose: the override replaces the base, the extension
    still rides on top of it."""
    cfg = AgentConfig(
        name="team",
        subagents=[
            {
                "from_archetype": "eng-backend",
                "system_prompt": "You are Backend v2.",
                "system_prompt_extension": "Be terse.",
            }
        ],
    )
    prompt = cfg.subagents[0].system_prompt
    assert prompt.startswith("You are Backend v2.")
    assert "an API specialist" not in prompt  # base was replaced
    assert prompt.endswith("Be terse.")


def test_builtin_dict_entries_carry_the_extension() -> None:
    """ToolkitAgent (`builtin_subagents`) entries compose the same way."""
    from olano.subagents.registry import resolve_as_subagent

    sub = resolve_as_subagent("GitHubAgent")
    base = sub.system_prompt

    from olano.subagents._schema import ToolkitAgentOverride

    override = ToolkitAgentOverride(name="GitHubAgent", system_prompt_extension="Only org repos.")
    extended = resolve_as_subagent("GitHubAgent", override)
    assert extended.system_prompt.startswith(base.rstrip())
    assert extended.system_prompt.endswith("Only org repos.")
    assert extended.system_prompt_extension == "Only org repos."


# ── team install: per-member models ──────────────────────────────────────────


def _installed(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_writes_member_models_next_to_the_refs(pack, tmp_path) -> None:
    path = installer.install_archetype(
        "lionTest",
        agents_dir=tmp_path / "agents",
        subagent_models={"eng-backend": "fake:instant", "ops": "fake:fast"},
    )
    data = _installed(path)
    assert data["subagents"] == [{"from_archetype": "eng-backend", "model": "fake:instant"}]
    # The bare builtin string grew a dict form to carry its override.
    assert data["builtin_subagents"] == [{"name": "ops", "model": "fake:fast"}]
    # And the whole thing still validates as a bootable config.
    AgentConfig(**data)


def test_install_refuses_a_member_the_team_does_not_have(pack, tmp_path) -> None:
    """A silently ignored override is worse than a refused install."""
    with pytest.raises(ValueError, match="ghost-member"):
        installer.install_archetype(
            "lionTest",
            agents_dir=tmp_path / "agents",
            subagent_models={"ghost-member": "fake:instant"},
        )


# ── API endpoints ────────────────────────────────────────────────────────────


@pytest.fixture
def api_env(pack, tmp_workspace, monkeypatch):
    """TestClient + an in-memory agent.json5 store behind the config helpers."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))

    import olano.runtime.api as api_mod

    store: dict = {
        "name": "team",
        "model": "default:pro",
        "subagents": [{"from_archetype": "eng-backend"}],
        "builtin_subagents": ["GitHubAgent"],
    }
    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: dict(store))

    def _fake_write(name, data, create=False):
        store.clear()
        store.update(data)

    monkeypatch.setattr(api_mod, "_write_agent_config", _fake_write)

    from fastapi.testclient import TestClient

    gw = MagicMock()
    gw._runners = {}
    gw._channels = []
    client = TestClient(api_mod.create_api(gw))
    return client, store


def test_from_archetype_add_writes_overrides_next_to_the_ref(api_env) -> None:
    client, store = api_env
    store["subagents"] = []
    resp = client.post(
        "/api/agents/team/subagents/from-archetype",
        json={
            "archetype": "eng-backend",
            "model": "default:pro",
            "system_prompt_extension": "Be terse.",
        },
    )
    assert resp.status_code == 200, resp.text
    assert store["subagents"] == [
        {
            "from_archetype": "eng-backend",
            "model": "default:pro",
            "system_prompt_extension": "Be terse.",
        }
    ]


def test_overrides_put_rewrites_the_stub_and_keeps_the_ref(api_env) -> None:
    client, store = api_env
    store["subagents"] = [
        {
            "from_archetype": "eng-backend",
            "model": "fake:instant",
            "system_prompt_extension": "Old.",
        }
    ]
    resp = client.put(
        "/api/agents/team/subagents/eng-backend/overrides",
        json={"model": "default:max"},
    )
    assert resp.status_code == 200, resp.text
    # PUT semantics: the omitted extension override is CLEARED, the ref stays.
    assert store["subagents"] == [{"from_archetype": "eng-backend", "model": "default:max"}]


def test_overrides_put_converts_a_bare_builtin_to_its_dict_form(api_env) -> None:
    client, store = api_env
    resp = client.put(
        "/api/agents/team/subagents/GitHubAgent/overrides",
        json={"model": "fake:instant", "system_prompt_extension": "Only org repos."},
    )
    assert resp.status_code == 200, resp.text
    assert store["builtin_subagents"] == [
        {
            "name": "GitHubAgent",
            "model": "fake:instant",
            "system_prompt_extension": "Only org repos.",
        }
    ]
    # Clearing everything collapses back to the bare string.
    resp = client.put("/api/agents/team/subagents/GitHubAgent/overrides", json={})
    assert resp.status_code == 200, resp.text
    assert store["builtin_subagents"] == ["GitHubAgent"]


def test_overrides_put_rejects_a_bad_model(api_env) -> None:
    client, _ = api_env
    resp = client.put(
        "/api/agents/team/subagents/eng-backend/overrides",
        json={"model": "default:hyper"},
    )
    assert resp.status_code == 422


def test_overrides_put_404s_on_an_unknown_ref(api_env) -> None:
    client, _ = api_env
    resp = client.put("/api/agents/team/subagents/nope/overrides", json={"model": "fake:instant"})
    assert resp.status_code == 404


def test_legacy_put_refuses_catalog_sourced_refs(api_env) -> None:
    """The full-replacement PUT would drop `from_archetype` and freeze a stale
    prompt copy — catalog-sourced subagents must go through /overrides."""
    client, store = api_env
    before = dict(store)
    resp = client.put(
        "/api/agents/team/subagents/eng-backend",
        json={
            "name": "eng-backend",
            "description": "x",
            "system_prompt": "y",
            "tools": [],
            "skills": [],
        },
    )
    assert resp.status_code == 409
    assert "overrides" in resp.json()["detail"]
    assert store == before  # nothing written


def test_archetype_detail_lists_members_for_the_install_preview(api_env) -> None:
    client, _ = api_env
    resp = client.get("/api/archetypes/lionTest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "default:pro"
    members = {m["ref"]: m for m in body["subagents"]}
    assert members["eng-backend"]["source"] == "archetype"
    assert members["eng-backend"]["model"] == "default:turbo"
    assert "ops" in members
