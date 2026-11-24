"""The `default:*` model alias layer.

`default:turbo|pro|max` may appear anywhere a model string is accepted; the
resolver swaps it for the target the box is configured with — the
``default_models:`` config.yaml block when set, the box's built-in target
otherwise (Olano tiers on a managed box, Codex literals on BYOK). Resolution
happens at the consumers (build_model, the runner's billing paths, the context
report); agent.json5 keeps the alias verbatim.
"""

from __future__ import annotations

import json

import pytest

from olano.config import system_settings as ss
from olano.models.aliases import alias_targets, is_model_alias, resolve_model_alias


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)


@pytest.fixture
def unmanaged(monkeypatch):
    monkeypatch.setattr(ss, "managed_olano_default", lambda: False)


# ── the resolver ─────────────────────────────────────────────────────────────


def test_non_alias_strings_pass_through_verbatim(unmanaged) -> None:
    """Idempotent for anything that isn't `default:*` — safe at every layer."""
    for value in ("anthropic:claude-sonnet-4-6", "olano:turbo", "fake:instant", "gpt-4o", ""):
        assert resolve_model_alias(value) == value


def test_is_model_alias_detects_only_the_default_prefix() -> None:
    assert is_model_alias("default:turbo")
    assert is_model_alias(" Default:PRO ")  # provider match is case/space-insensitive
    assert not is_model_alias("olano:turbo")
    assert not is_model_alias("default")  # bare word, not an alias
    assert not is_model_alias(None)


def test_unknown_tier_raises_an_actionable_error(unmanaged) -> None:
    with pytest.raises(ValueError, match="default:turbo, default:pro or default:max"):
        resolve_model_alias("default:hyper")


def test_managed_builtin_targets_are_the_olano_tiers(managed) -> None:
    assert resolve_model_alias("default:turbo") == "olano:turbo"
    assert resolve_model_alias("default:pro") == "olano:pro"
    assert resolve_model_alias("default:max") == "olano:max"


def test_unmanaged_builtin_targets_are_the_codex_literals(unmanaged) -> None:
    """A keyless BYOK box must never default to `olano:*` — those can only 401
    without a gateway. The Codex GPT-5.6 ladder is the deliberate fallback."""
    assert resolve_model_alias("default:turbo") == "openai-codex:gpt-5.6-luna"
    assert resolve_model_alias("default:pro") == "openai-codex:gpt-5.6-terra"
    assert resolve_model_alias("default:max") == "openai-codex:gpt-5.6-sol"


def test_an_explicit_config_target_wins_per_tier(managed) -> None:
    ss.set_default_model("turbo", "fake:instant")
    assert resolve_model_alias("default:turbo") == "fake:instant"
    # The other tiers keep their built-in targets — override is per key.
    assert resolve_model_alias("default:pro") == "olano:pro"


def test_clearing_a_tier_resets_to_the_builtin_target(managed) -> None:
    ss.set_default_model("turbo", "fake:instant")
    ss.set_default_model("turbo", None)
    assert resolve_model_alias("default:turbo") == "olano:turbo"


def test_alias_case_and_whitespace_are_forgiven(managed) -> None:
    assert resolve_model_alias("default: TURBO") == "olano:turbo"


def test_a_hand_edited_alias_target_falls_back_instead_of_recursing(managed) -> None:
    """The write path refuses `default:*` targets, but a hand-edited config.yaml
    can still carry one. Resolution must not chase it — the built-in target
    stands, and the agent boots."""
    paths = ss.get_olano_paths()
    raw = {"default_models": {"turbo": "default:pro"}}
    ss._write_raw(paths, raw)
    assert resolve_model_alias("default:turbo") == "olano:turbo"


def test_a_malformed_block_never_breaks_resolution(managed) -> None:
    paths = ss.get_olano_paths()
    ss._write_raw(paths, {"default_models": "oops"})
    assert resolve_model_alias("default:turbo") == "olano:turbo"


def test_alias_targets_reports_every_tier(unmanaged) -> None:
    targets = alias_targets()
    assert set(targets) == {"turbo", "pro", "max"}


# ── the write path ───────────────────────────────────────────────────────────


def test_set_default_model_rejects_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="Unknown default-model tier"):
        ss.set_default_model("hyper", "fake:instant")


def test_set_default_model_rejects_alias_to_alias() -> None:
    """One level of indirection only — a chain would make resolution recursive
    and a cycle would brick every agent on the alias."""
    with pytest.raises(ValueError, match="alias"):
        ss.set_default_model("turbo", "default:pro")


def test_set_default_model_rejects_utility_groups() -> None:
    with pytest.raises(ValueError, match="utility"):
        ss.set_default_model("turbo", "olano:vision")


def test_set_default_model_rejects_an_unknown_olano_tier() -> None:
    with pytest.raises(ValueError, match="olano:turbo, olano:pro or olano:max"):
        ss.set_default_model("pro", "olano:hyper")


def test_reset_removes_the_key_rather_than_freezing_a_literal(managed) -> None:
    """Clearing must fall back to the box's posture-aware default — writing the
    current literal would silently pin a managed box off future retargets."""
    ss.set_default_model("turbo", "fake:instant")
    ss.set_default_model("turbo", "")
    raw = ss._load_raw(ss.get_olano_paths())
    assert "turbo" not in (raw.get("default_models") or {})


def test_default_models_report_shows_value_resolved_and_builtin(managed) -> None:
    ss.set_default_model("pro", "fake:instant")
    report = ss.default_models_report()
    assert report["pro"] == {
        "value": "fake:instant",
        "resolved": "fake:instant",
        "builtin": "olano:pro",
    }
    assert report["turbo"]["value"] == ""
    assert report["turbo"]["resolved"] == "olano:turbo"


# ── consumers ────────────────────────────────────────────────────────────────


def test_build_model_resolves_the_alias_before_provider_dispatch(managed) -> None:
    """`default:turbo` pointed at `fake:instant` must build the fake model —
    proof that resolution precedes parse_model_string AND the fake
    short-circuit, with no key or network involved."""
    from olano.models import build_model
    from olano.models.fake import FakeChatModel

    ss.set_default_model("turbo", "fake:instant")
    assert isinstance(build_model("default:turbo"), FakeChatModel)


def test_build_model_surfaces_an_unknown_tier_as_the_alias_error(managed) -> None:
    from olano.models import build_model

    with pytest.raises(ValueError, match="default-model tier"):
        build_model("default:hyper")


def test_runner_resolves_the_alias_for_billing_and_classification(managed) -> None:
    """Metering and provider-keyed error classification must see the concrete
    model — an unresolved alias meters as an unknown BYOK model and classifies
    under a nonexistent 'default' provider."""
    from olano.core.runner import _provider_from_model, _resolved_model

    assert _resolved_model("default:turbo") == "olano:turbo"
    assert _provider_from_model("default:turbo") == "olano"
    # Never raises: an unresolvable alias records verbatim rather than dropping
    # the usage row.
    assert _resolved_model("default:hyper") == "default:hyper"


def test_context_window_resolves_the_alias(managed) -> None:
    from olano.runtime.context_report import model_context_window

    assert model_context_window("default:pro") == model_context_window("olano:pro")


def test_agent_config_accepts_the_alias_as_a_brain() -> None:
    from olano.config.agent_config import AgentConfig

    cfg = AgentConfig(name="t", model="default:turbo")
    assert cfg.model == "default:turbo"  # stored verbatim — resolution is build-time


# ── API surfaces ─────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    import olano.runtime.api as api_mod

    gw = MagicMock()
    gw._runners = {}
    gw._channels = []
    return TestClient(api_mod.create_api(gw))


def test_providers_payload_carries_the_aliases_in_both_shapes(api_client, managed) -> None:
    """The pickers learn the alias→target map from here — including the cheap
    `?fields=basic` payload the onboarding picker uses."""
    for query in ("?fields=basic", ""):
        body = api_client.get(f"/api/catalog/providers{query}").json()
        aliases = {a["value"]: a["target"] for a in body["model_aliases"]}
        assert aliases["default:turbo"] == "olano:turbo"
        assert set(aliases) == {"default:turbo", "default:pro", "default:max"}


def test_system_config_round_trips_default_models(api_client, managed) -> None:
    resp = api_client.put("/api/system/config", json={"default_models": {"pro": "fake:instant"}})
    assert resp.status_code == 200, resp.text
    report = api_client.get("/api/system/config").json()["default_models"]
    assert report["pro"] == {
        "value": "fake:instant",
        "resolved": "fake:instant",
        "builtin": "olano:pro",
    }
    # Reset: "" falls back to the box default, never a frozen literal.
    assert (
        api_client.put("/api/system/config", json={"default_models": {"pro": ""}}).status_code
        == 200
    )
    report = api_client.get("/api/system/config").json()["default_models"]
    assert report["pro"]["value"] == ""
    assert report["pro"]["resolved"] == "olano:pro"


def test_system_config_rejects_bad_default_model_writes(api_client) -> None:
    assert (
        api_client.put(
            "/api/system/config", json={"default_models": {"pro": "default:max"}}
        ).status_code
        == 422
    )
    assert (
        api_client.put(
            "/api/system/config", json={"default_models": {"hyper": "fake:instant"}}
        ).status_code
        == 400
    )


# ── migration 0012: existing installs move onto the aliases ─────────────────


def _write_agent(agents_dir, name: str, data: dict):
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _apply_0012(home) -> None:
    from olano.migrations import _load_migrations

    for mod in _load_migrations():
        if mod.ID == "0012_default_model_aliases":
            mod.apply(home)
            return
    raise AssertionError("migration 0012 not discovered")


def _installed(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_0012_moves_the_frozen_pack_literal_to_the_alias(unmanaged, tmp_path) -> None:
    """Runs on unmanaged boxes too — the alias resolves per posture, so it is
    safe everywhere (unlike 0011's olano tiers)."""
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "helpdesk",
        {"name": "helpdesk", "archetype": "it-admin", "model": "anthropic:claude-sonnet-4-6"},
    )
    _apply_0012(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_0012_moves_agents_0011_put_on_the_olano_tiers(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    father = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "olano:pro"},
    )
    worker = _write_agent(
        agents, "helpdesk", {"name": "helpdesk", "archetype": "it-admin", "model": "olano:turbo"}
    )
    _apply_0012(tmp_path)
    assert _installed(father)["model"] == "default:pro"
    assert _installed(worker)["model"] == "default:turbo"


def test_0012_moves_a_model_less_integration_agent(unmanaged, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(agents, "gmail", {"name": "gmail", "toolkit_agent": "gmail"})
    _apply_0012(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_0012_leaves_owner_picked_models_alone(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents, "helpdesk", {"name": "helpdesk", "archetype": "it-admin", "model": "openai:gpt-5.5"}
    )
    before = path.read_text(encoding="utf-8")
    _apply_0012(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_0012_leaves_hand_written_agents_alone(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(agents, "goku", {"name": "goku", "model": "anthropic:claude-sonnet-4-6"})
    before = path.read_text(encoding="utf-8")
    _apply_0012(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_0012_is_idempotent(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )
    _apply_0012(tmp_path)
    first = path.read_text(encoding="utf-8")
    _apply_0012(tmp_path)
    assert path.read_text(encoding="utf-8") == first
