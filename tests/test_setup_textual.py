from __future__ import annotations

import argparse
import json
from pathlib import Path

from textual.containers import VerticalScroll

from olano.cli.setup_textual import (
    SetupOutcome,
    SetupPlan,
    _resolve_initial_values,
    apply_setup_plan,
    run_doctor_if_requested,
)
from olano.cli.setup_textual_app import OlanoSetupPrototype, SetupPromptArea
from olano.config.home import HomeConfig, load_home_config


async def test_textual_setup_prototype_mounts(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    app = OlanoSetupPrototype(_resolve_initial_values(args))

    async with app.run_test():
        assert app.query_one("#header-line").display is False
        assert str(app.query_one("#start-hint").content) == ""
        assert str(app.query_one("#key-hints").content) == ""
        assert app.query_one("#body-scroll", VerticalScroll) is not None


async def test_textual_setup_prototype_right_then_left_returns_to_splash(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    app = OlanoSetupPrototype(_resolve_initial_values(args))

    async with app.run_test() as pilot:
        await pilot.press("right")
        assert str(app.query_one("#header-line").content).startswith("LIONCLAW  2/")
        assert str(app.query_one("#header-line").content).endswith("  Secrets")
        await pilot.press("left")
        assert app.query_one("#header-line").display is False


def test_textual_setup_prototype_uses_simplified_flow_without_extra_finish_pages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    initial = _resolve_initial_values(args)
    initial["secrets_strategy"] = "later"
    app = OlanoSetupPrototype(initial)

    assert app._page_sequence() == [
        "splash",
        "secrets_strategy",
        "provider",
        "model",
        "safety",
        "agent_mode",
        "agent_name",
        "emoji",
        "review",
    ]


def test_textual_setup_prototype_skips_vault_mode_when_only_passphrase_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    app = OlanoSetupPrototype(_resolve_initial_values(args))

    assert "vault_mode" not in app._page_sequence()
    assert "vault_passphrase" in app._page_sequence()
    assert "quick_start" in app._page_sequence()
    assert "review" not in app._page_sequence()


def test_resolve_initial_values_defaults_to_quick_start(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["default", "merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    initial = _resolve_initial_values(args)

    assert initial["quick_start"] is True


def test_make_plan_quick_start_forces_default_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["default", "merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    values = _resolve_initial_values(args)
    values["quick_start"] = True
    from olano.cli.setup_textual import _make_plan

    result = _make_plan(values)

    assert result.quick_start is True
    assert result.provider == "ollama"
    assert result.model == "ollama:kimi-k2.5:cloud"
    assert result.safety_preset == "safe"
    assert result.archetype == "default"
    assert result.agent_name == "default"


def test_textual_setup_prototype_compact_breakpoint() -> None:
    assert OlanoSetupPrototype._is_compact_viewport(109, 40) is True
    assert OlanoSetupPrototype._is_compact_viewport(140, 29) is True
    assert OlanoSetupPrototype._is_compact_viewport(140, 40) is False


async def test_custom_prompt_enter_advances_instead_of_inserting_newline(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    initial = _resolve_initial_values(args)
    initial["quick_start"] = False
    initial["mode"] = "custom"
    app = OlanoSetupPrototype(initial)

    async with app.run_test() as pilot:
        app.page_index = app._page_sequence().index("custom_prompt")
        app._render_current_page()
        assert isinstance(app.focused, SetupPromptArea)
        await pilot.press("enter")
        assert app._page_sequence()[app.page_index] == "review"


async def test_quick_start_next_submits_without_review(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["default", "merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )
    app = OlanoSetupPrototype(_resolve_initial_values(args))
    submitted: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.press("right")
        app.query_one("#vault-passphrase").value = "secret"
        app.query_one("#vault-passphrase-confirm").value = "secret"
        app.values["vault_passphrase"] = "secret"
        app.page_index = app._page_sequence().index("quick_start")
        app._render_current_page()
        app.action_submit = lambda: submitted.append(True)  # type: ignore[method-assign]
        app.action_next()

    assert submitted == [True]


def test_resolve_initial_values_uses_valid_specialist_for_archetype_select(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode="general",
        archetype=None,
        agent_name=None,
        emoji=None,
    )

    initial = _resolve_initial_values(args)

    assert initial["mode"] == "general"
    assert initial["archetype"] == "lionDev"


def test_resolve_initial_values_defaults_to_ollama_kimi(monkeypatch) -> None:
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_archetypes",
        lambda: ["merli", "lionDev", "lionFinance"],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual._available_vault_modes",
        lambda: [("Passphrase", "passphrase")],
    )
    monkeypatch.setattr(
        "olano.cli.setup_textual.load_home_config",
        lambda: HomeConfig(default_agent="merli", preferred_model="anthropic:claude-opus-4-6"),
    )

    args = argparse.Namespace(
        home=None,
        provider=None,
        model=None,
        api_key=None,
        safety_preset=None,
        mode=None,
        archetype=None,
        agent_name=None,
        emoji=None,
    )

    initial = _resolve_initial_values(args)

    assert initial["provider"] == "ollama"
    assert initial["model"] == "ollama:kimi-k2.5:cloud"


def test_apply_setup_plan_installs_agent_and_updates_home_config(
    lionclaw_home: Path,
    monkeypatch,
) -> None:
    from olano.cli import main as main_cli

    installed_path = lionclaw_home / "agents" / "merli" / "agent.json5"
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    installed_path.write_text('{"name": "merli"}\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_install(**kwargs):
        captured["install"] = kwargs
        return installed_path

    def fake_write_setup_profile(
        workspace_root, user_name, timezone, preferences, *, include_name=True
    ):
        captured["profile"] = (workspace_root, user_name, timezone, preferences, include_name)

    monkeypatch.setattr(main_cli, "_install_or_update_archetype_agent", fake_install)
    monkeypatch.setattr(main_cli, "_write_setup_profile", fake_write_setup_profile)
    monkeypatch.setattr(main_cli, "_credentials_available_for_setup", lambda *args, **kwargs: False)

    plan = SetupPlan(
        home=lionclaw_home,
        provider="ollama",
        model="ollama:llama3.2",
        api_key="",
        safety_preset="developer",
        mode="general",
        archetype="merli",
        agent_name="Merli",
        emoji="🦁",
        user_name="Casey",
        timezone="Asia/Singapore",
        preferences="",
        secrets_strategy="later",
        vault_mode="passphrase",
        vault_passphrase="",
        import_env_credentials=False,
        skip_verify=False,
        run_doctor=True,
        custom_description="",
        custom_system_prompt="",
    )

    outcome = apply_setup_plan(plan)

    assert outcome.agent_id == "merli"
    assert outcome.verification_skipped is True
    assert captured["install"] == {
        "archetype_name": "merli",
        "agent_id": "merli",
        "emoji": "🦁",
        "security_preset": "developer",
        "overwrite_existing": False,
        "main_model": "ollama:llama3.2",
        "subagent_models": {},
        "force_all_subagents": True,
        # The wizard now persists the human name the user typed instead of
        # slugifying it away — "Merli" survives as the display name.
        "display_name": "Merli",
    }
    assert captured["profile"] == (
        installed_path.parent / "workspace",
        "Casey",
        "Asia/Singapore",
        "",
        False,
    )

    home_config = load_home_config()
    assert home_config.default_agent == "merli"
    assert home_config.preferred_model == "ollama:llama3.2"


def test_apply_setup_plan_runs_verification_when_requested(
    lionclaw_home: Path,
    monkeypatch,
) -> None:
    from olano.cli import main as main_cli

    installed_path = lionclaw_home / "agents" / "merli" / "agent.json5"
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    installed_path.write_text('{"name": "merli"}\n', encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        main_cli, "_install_or_update_archetype_agent", lambda **kwargs: installed_path
    )
    monkeypatch.setattr(main_cli, "_write_setup_profile", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_cli, "_credentials_available_for_setup", lambda *args, **kwargs: True)

    def fake_probe(model: str):
        captured["model"] = model
        return True, "connectivity ok"

    monkeypatch.setattr(main_cli, "_probe_model_connectivity", fake_probe)

    plan = SetupPlan(
        home=lionclaw_home,
        provider="ollama",
        model="ollama:kimi-k2.5:cloud",
        api_key="",
        safety_preset="developer",
        mode="general",
        archetype="merli",
        agent_name="Merli",
        emoji="🦁",
        user_name="Casey",
        timezone="Asia/Singapore",
        preferences="",
        secrets_strategy="later",
        vault_mode="passphrase",
        vault_passphrase="",
        import_env_credentials=False,
        skip_verify=False,
        run_doctor=False,
        custom_description="",
        custom_system_prompt="",
    )

    outcome = apply_setup_plan(plan)

    assert captured["model"] == "ollama:kimi-k2.5:cloud"
    assert outcome.verification_skipped is False


def test_apply_setup_plan_quick_start_creates_default_agent(
    lionclaw_home: Path,
    monkeypatch,
) -> None:
    from olano.cli import main as main_cli

    class _FakeVault:
        _db_path = lionclaw_home / "vault.sqlite3"

        def set(self, *args, **kwargs):
            return None

    write_setup_profile_calls: list[tuple] = []
    monkeypatch.setattr(
        main_cli,
        "_write_setup_profile",
        lambda *args, **kwargs: write_setup_profile_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(main_cli, "_credentials_available_for_setup", lambda *args, **kwargs: True)
    monkeypatch.setattr(main_cli, "_probe_model_connectivity", lambda model: (True, "ok"))
    monkeypatch.setattr(
        "olano.cli.setup_textual._initialize_vault_for_plan",
        lambda plan: (_FakeVault(), "Vault initialized"),
    )

    plan = SetupPlan(
        home=lionclaw_home,
        provider="ollama",
        model="ollama:kimi-k2.5:cloud",
        api_key="",
        safety_preset="safe",
        mode="general",
        archetype="default",
        agent_name="default",
        emoji="🤖",
        user_name="Casey",
        timezone="Asia/Singapore",
        preferences="",
        secrets_strategy="vault",
        vault_mode="passphrase",
        vault_passphrase="secret",
        import_env_credentials=False,
        skip_verify=False,
        run_doctor=False,
        custom_description="",
        custom_system_prompt="",
        quick_start=True,
    )

    outcome = apply_setup_plan(plan)

    config_path = lionclaw_home / "agents" / "default" / "agent.json5"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert outcome.agent_id == "default"
    assert outcome.verification_skipped is False
    assert config["model"] == "ollama:kimi-k2.5:cloud"
    assert "safe_shell" in config["tools"]
    assert {agent["name"] for agent in config["subagents"]} == {
        "WebAgent",
        "BrowserAgent",
        "GoogleAgent",
    }
    assert config["shared_paths"][0]["target"] == "/shared/default"
    assert config["archetype"] == "default"
    assert config["description"] == "Fresh Olano starter agent"
    workspace_root = lionclaw_home / "agents" / "default" / "workspace"
    assert "Casey" not in (workspace_root / "USER.md").read_text(encoding="utf-8")
    identity_text = (workspace_root / "IDENTITY.md").read_text(encoding="utf-8")
    soul_text = (workspace_root / "SOUL.md").read_text(encoding="utf-8")
    assert "**Name:** *Not chosen yet*" in identity_text
    assert "lionclaw:blank-persona" in identity_text
    assert "No fixed tone or personality yet" in soul_text
    assert write_setup_profile_calls == []


def test_run_doctor_if_requested_blocks_chat_on_hard_failure(
    monkeypatch, lionclaw_home: Path
) -> None:
    from olano.cli import main as main_cli

    monkeypatch.setattr(
        main_cli,
        "_run_doctor_checks",
        lambda agent_name, model_string, skip_model: ([("Docker", "FAIL", "missing")], True),
    )
    monkeypatch.setattr(main_cli, "_render_doctor_report", lambda rows, hard_fail: None)

    plan = SetupPlan(
        home=lionclaw_home,
        provider="openai",
        model="openai:gpt-4o",
        api_key="",
        safety_preset="developer",
        mode="general",
        archetype="merli",
        agent_name="Merli",
        emoji="🦁",
        user_name="Casey",
        timezone="Asia/Singapore",
        preferences="",
        secrets_strategy="later",
        vault_mode="passphrase",
        vault_passphrase="",
        import_env_credentials=False,
        skip_verify=True,
        run_doctor=True,
        custom_description="",
        custom_system_prompt="",
    )
    outcome = SetupOutcome(
        home=lionclaw_home,
        agent_id="merli",
        entered_name="Merli",
        emoji="🦁",
        verification_model="openai:gpt-4o",
        verification_skipped=True,
    )

    assert run_doctor_if_requested(plan, outcome) is False
