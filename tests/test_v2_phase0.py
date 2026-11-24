"""Phase 0 CLI coverage for setup, doctor, help, and agent scaffolding."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from olano.cli.main import _normalize_model_string, _read_setup_tty_line, _setup_confirm, cli
from olano.models import parse_model_string


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_agents_create_from_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: CliRunner
) -> None:
    home = tmp_path / "home"

    result = runner.invoke(
        cli,
        [
            "--home",
            str(home),
            "agents",
            "create",
            "--from-template",
            "researcher",
            "--name",
            "scout",
        ],
    )

    assert result.exit_code == 0
    config_path = home / "agents" / "scout" / "agent.json5"
    workspace_root = home / "agents" / "scout" / "workspace"
    assert config_path.exists()
    assert workspace_root.exists()
    assert (workspace_root / "AGENTS.md").exists()
    assert "scout" in config_path.read_text(encoding="utf-8")


def test_agents_install_archetype_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "home"

    result = runner.invoke(
        cli, ["--home", str(home), "agents", "install", "merli", "--as", "captain"]
    )

    assert result.exit_code == 0
    config_path = home / "agents" / "captain" / "agent.json5"
    assert config_path.exists()
    assert (home / "agents" / "captain" / "workspace" / "IDENTITY.md").exists()


def test_agents_install_merli_writes_valid_config(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"

    result = runner.invoke(cli, ["--home", str(home), "agents", "install", "merli"])

    assert result.exit_code == 0
    config_path = home / "agents" / "merli" / "agent.json5"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["name"] == "merli"


def test_setup_yes_initializes_workspace_and_applies_safety_preset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "client-home"

    result = runner.invoke(
        cli,
        [
            "--home",
            str(home),
            "setup",
            "--yes",
            "--skip-verify",
            "--safety-preset",
            "safe",
            "--mode",
            "general",
            "--agent-name",
            "Merli",
        ],
    )

    assert result.exit_code == 0
    workspace_root = home / "agents" / "merli" / "workspace"
    assert (workspace_root / "USER.md").exists()
    assert "Setup Profile" in (workspace_root / "USER.md").read_text(encoding="utf-8")

    config_path = home / "agents" / "merli" / "agent.json5"
    assert config_path.exists()
    config_text = config_path.read_text(encoding="utf-8")
    assert '"profile": "safe"' in config_text or "profile: safe" in config_text
    assert "default_agent: merli" in (home / "config.yaml").read_text(encoding="utf-8")
    assert "olano --home" in result.output
    assert " dashboard" in result.output
    assert " chat" not in result.output


def test_setup_interactive_shows_descriptive_menus(
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "interactive-home"

    result = runner.invoke(
        cli,
        ["--home", str(home), "setup", "--skip-verify"],
        input="Carlos\nSingapore\n\n1\n\n\n1\n1\n\n\n\n",
    )

    assert result.exit_code == 0
    assert "Choose a safety level" in result.output
    assert "Guided workspace + shell" in result.output
    assert "Choose your first agent" in result.output
    assert "Built-in specialist" in result.output
    assert "Merli" in result.output


def test_read_setup_tty_line_treats_carriage_return_as_enter() -> None:
    output: list[tuple[str, bool]] = []
    chars = iter(["M", "e", "r", "l", "i", "\r"])

    value = _read_setup_tty_line(
        "  Agent name",
        getchar=lambda: next(chars),
        echo=lambda text="", nl=True: output.append((text, nl)),
    )

    assert value == "Merli"
    assert "\r" not in "".join(text for text, _ in output)


def test_setup_confirm_uses_default_for_carriage_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("olano.cli.main._setup_prompt_uses_raw_reader", lambda: True)
    monkeypatch.setattr(
        "olano.cli.main._read_setup_tty_line",
        lambda *args, **kwargs: "",
    )

    assert _setup_confirm("  Continue working in this Olano home?", default=True) is True


def test_setup_initializes_vault(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "vault-home"

    result = runner.invoke(
        cli,
        [
            "--home",
            str(home),
            "setup",
            "--yes",
            "--skip-verify",
            "--mode",
            "general",
            "--agent-name",
            "Merli",
        ],
    )

    assert result.exit_code == 0
    assert (home / "vault" / "vault.db").exists()
    assert "Vault" in result.output


def test_setup_existing_home_can_modify_existing_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    from olano.archetypes.installer import install_archetype

    home = tmp_path / "existing-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    install_archetype("merli", agent_name="merli")

    result = runner.invoke(
        cli,
        ["--home", str(home), "setup", "--skip-verify"],
        input="y\n2\n1\nCarlos\nSingapore\n\n1\n\n\n\n\n\n🐾\n\n",
    )

    assert result.exit_code == 0
    assert "Existing Olano Home Detected" in result.output
    assert "Editing existing agent id" in result.output
    config_text = (home / "agents" / "merli" / "agent.json5").read_text(encoding="utf-8")
    assert "🐾" in config_text


def test_setup_existing_home_create_flow_suggests_available_agent_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    from olano.archetypes.installer import install_archetype

    home = tmp_path / "existing-home-new-agent"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    install_archetype("merli", agent_name="merli")

    result = runner.invoke(
        cli,
        ["--home", str(home), "setup", "--skip-verify"],
        input="y\n1\nCarlos\nSingapore\n\n1\n\n\n\n\n\n\n\n\n",
    )

    assert result.exit_code == 0
    assert (home / "agents" / "merli_2" / "agent.json5").exists()
    assert "Merli 2" in result.output


def test_setup_custom_agent_configures_subagents_and_tools(
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "custom-home"

    result = runner.invoke(
        cli,
        ["--home", str(home), "setup", "--skip-verify"],
        input="Carlos\nSingapore\n\n1\n\n\n1\n3\nCharlie\n\nStoic bot\n\n\n3\n\n\n\n\n",
    )

    assert result.exit_code == 0
    config_text = (home / "agents" / "charlie" / "agent.json5").read_text(encoding="utf-8")
    assert "Stoic bot" in config_text
    assert "system_prompt" in config_text
    assert "subagents" in config_text
    assert "WebAgent" in config_text
    assert "web_search" in config_text


def test_doctor_reports_success_for_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    from olano.archetypes.installer import install_agent_template

    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    install_agent_template("minimal", agent_name="merli")

    vault_path = home / "vault" / "vault.db"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    monkeypatch.setattr("olano.cli.main._probe_model_connectivity", lambda model: (True, "OK"))
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in {"deepagents", "langchain_anthropic"} else None,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "ollama"} else None,
    )

    def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: int):
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["--home", str(home), "doctor", "--agent", "merli"])

    assert result.exit_code == 0
    assert "Python >= 3.11" in result.output
    assert "Docker available" in result.output
    assert "Model connectivity" in result.output
    assert "PASS" in result.output


def test_doctor_fails_when_workspace_is_missing_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "broken-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    monkeypatch.setattr("olano.cli.main._probe_model_connectivity", lambda model: (True, "OK"))
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: object() if name == "deepagents" else None
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "ollama"} else None,
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, capture_output, text, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )

    result = runner.invoke(cli, ["--home", str(home), "doctor", "--skip-model"])

    assert result.exit_code == 0
    assert "Vault initialized" in result.output
    assert "Workspace valid" in result.output


def test_chat_without_a_task_explains_the_tui_was_removed(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["chat"])

    assert result.exit_code == 2
    flat = " ".join(result.output.split())
    assert "has been removed" in flat
    assert "olano dashboard" in flat


def test_chat_is_hidden_from_root_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    commands = [line.split()[0] for line in result.output.splitlines() if line.startswith("  ")]
    assert "chat" not in commands


def test_setup_specialized_mode_installs_selected_archetype(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "specialized-home"

    # `lionLife`, not `lionDev`: the lion* teams built from pack specialists
    # ship in the canonical olano-agents pack, not in-package, so naming one
    # here made the test depend on `setup` completing a network `git clone` —
    # which passes on a dev box and fails in CI. lionLife is a bundled,
    # self-contained team, so this exercises specialized mode without a network.
    result = runner.invoke(
        cli,
        [
            "--home",
            str(home),
            "setup",
            "--yes",
            "--skip-verify",
            "--mode",
            "specialized",
            "--archetype",
            "lionLife",
            "--agent-name",
            "Coder",
        ],
    )

    assert result.exit_code == 0
    config_path = home / "agents" / "coder" / "agent.json5"
    assert config_path.exists()
    assert "lionLife" in config_path.read_text(encoding="utf-8")
    assert "default_agent: coder" in (home / "config.yaml").read_text(encoding="utf-8")

    # The installed agent must actually LOAD, not merely be written. This
    # assertion is the one that was missing: `lionDev` passed the checks above
    # for months while referencing specialists that exist only in the pack, so
    # on a box without it the install "succeeded" and then raised
    # ValidationError on load.
    from olano.config.loader import load_agent_config

    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    assert load_agent_config("coder", source="home").name == "coder"


def test_chat_message_runs_one_shot_on_home_default_agent(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.config.home import HomeConfig

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "olano.cli.main.load_home_config",
        lambda: HomeConfig(default_agent="merli", runtime_api_port=8765, dashboard_port=3200),
    )

    async def fake_run_non_interactive(agent_name, prompt, **kwargs):
        captured["agent"] = agent_name
        captured["prompt"] = prompt
        captured["config_source"] = kwargs["config_source"]
        return 0

    monkeypatch.setattr("olano.cli.non_interactive.run_non_interactive", fake_run_non_interactive)

    result = runner.invoke(cli, ["chat", "-m", "say hi"])

    assert result.exit_code == 0
    assert captured == {"agent": "merli", "prompt": "say hi", "config_source": "home"}


def test_agentfather_points_at_the_dashboard_or_runs_one_shot(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Already installed, so neither invocation reinstalls it.
    monkeypatch.setattr(
        "olano.config.loader.find_agent_config_path",
        lambda name, source="auto": tmp_path / "agent.json5",
    )
    captured: dict[str, object] = {}

    async def fake_run_non_interactive(agent_name, prompt, **kwargs):
        captured["agent"] = agent_name
        captured["prompt"] = prompt
        return 0

    monkeypatch.setattr("olano.cli.non_interactive.run_non_interactive", fake_run_non_interactive)

    result = runner.invoke(cli, ["agentfather"])
    assert result.exit_code == 0
    assert "olano dashboard" in result.output
    assert captured == {}

    result = runner.invoke(cli, ["agentfather", "-m", "list my agents"])
    assert result.exit_code == 0
    assert captured == {"agent": "agentfather", "prompt": "list my agents"}


def test_setup_uses_textual_wizard_on_tty(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "wizard-home"
    captured: dict[str, bool] = {"called": False}

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)
    monkeypatch.setattr("olano.cli.main._can_run_post_setup_doctor", lambda: False)

    wizard_state = {
        "user_name": "Carlos",
        "timezone": "Singapore",
        "preferences": "Likes structured setup flows",
        "secrets_strategy": "vault",
        "vault_mode": "passphrase",
        "vault_passphrase": "secret-passphrase",
        "import_env_vars": [],
        "provider": "anthropic",
        "model": "anthropic:claude-haiku-4-5",
        "api_key": "",
        "preset": "safe",
        "mode": "general",
        "archetype": "merli",
        "agent_name": "Merli",
        "emoji": "🦁",
        "description": "General-purpose Olano assistant",
        "system_prompt": "",
        "custom_tools": [],
        "custom_subagents": [],
    }

    def fake_run_textual_setup_wizard(**kwargs):
        captured["called"] = True
        return wizard_state, False

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", fake_run_textual_setup_wizard)

    result = runner.invoke(cli, ["--home", str(home), "setup", "--skip-verify"])

    assert result.exit_code == 0
    assert captured["called"] is True
    assert (home / "agents" / "merli" / "agent.json5").exists()


def test_setup_env_strategy_skips_vault_initialization(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "env-home"

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)
    monkeypatch.setattr("olano.cli.main._can_run_post_setup_doctor", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")

    def fake_run_textual_setup_wizard(**kwargs):
        return (
            {
                "user_name": "Carlos",
                "timezone": "Singapore",
                "preferences": "",
                "secrets_strategy": "env",
                "vault_mode": "",
                "vault_passphrase": "",
                "import_env_vars": [],
                "provider": "anthropic",
                "model": "anthropic:claude-haiku-4-5",
                "api_key": "",
                "preset": "safe",
                "mode": "general",
                "archetype": "merli",
                "agent_name": "Merli",
                "emoji": "🦁",
                "description": "General-purpose Olano assistant",
                "system_prompt": "",
                "custom_tools": [],
                "custom_subagents": [],
            },
            False,
        )

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", fake_run_textual_setup_wizard)
    monkeypatch.setattr("olano.cli.main._probe_model_connectivity", lambda model: (True, "OK"))

    result = runner.invoke(cli, ["--home", str(home), "setup"])

    assert result.exit_code == 0
    assert not (home / "vault" / "vault.db").exists()
    assert "Secrets strategy" in result.output
    assert "environment variables" in result.output


def test_setup_normalizes_unqualified_model_and_saves_home_preference(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "model-home"

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)
    monkeypatch.setattr("olano.cli.main._can_run_post_setup_doctor", lambda: False)

    def fake_run_textual_setup_wizard(**kwargs):
        return (
            {
                "user_name": "Carlos",
                "timezone": "Singapore",
                "preferences": "",
                "secrets_strategy": "later",
                "vault_mode": "",
                "vault_passphrase": "",
                "import_env_vars": [],
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "api_key": "",
                "preset": "safe",
                "mode": "general",
                "archetype": "merli",
                "agent_name": "Merli",
                "emoji": "🦁",
                "description": "General-purpose Olano assistant",
                "system_prompt": "",
                "custom_tools": [],
                "custom_subagents": [],
            },
            False,
        )

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", fake_run_textual_setup_wizard)

    result = runner.invoke(cli, ["--home", str(home), "setup", "--skip-verify"])

    assert result.exit_code == 0
    config_text = (home / "agents" / "merli" / "agent.json5").read_text(encoding="utf-8")
    assert "openai:gpt-5.4-mini" in config_text
    home_config = (home / "config.yaml").read_text(encoding="utf-8")
    assert "preferred_model: openai:gpt-5.4-mini" in home_config


def test_parse_model_string_treats_ollama_tag_as_model_suffix() -> None:
    assert parse_model_string("kimi-k2.5:cloud") == (None, "kimi-k2.5:cloud")
    assert parse_model_string("ollama:kimi-k2.5:cloud") == ("ollama", "kimi-k2.5:cloud")


def test_setup_normalizes_ollama_model_with_tag_suffix() -> None:
    assert _normalize_model_string("ollama", "kimi-k2.5:cloud") == "ollama:kimi-k2.5:cloud"


def test_setup_configure_later_skips_model_verification(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "later-home"

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)
    monkeypatch.setattr("olano.cli.main._can_run_post_setup_doctor", lambda: False)

    def fake_run_textual_setup_wizard(**kwargs):
        return (
            {
                "user_name": "Carlos",
                "timezone": "Singapore",
                "preferences": "",
                "secrets_strategy": "later",
                "vault_mode": "",
                "vault_passphrase": "",
                "import_env_vars": [],
                "provider": "anthropic",
                "model": "anthropic:claude-haiku-4-5",
                "api_key": "",
                "preset": "safe",
                "mode": "general",
                "archetype": "merli",
                "agent_name": "Merli",
                "emoji": "🦁",
                "description": "General-purpose Olano assistant",
                "system_prompt": "",
                "custom_tools": [],
                "custom_subagents": [],
            },
            False,
        )

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", fake_run_textual_setup_wizard)
    monkeypatch.setattr(
        "olano.cli.main._probe_model_connectivity",
        lambda model: (_ for _ in ()).throw(AssertionError("should not probe model")),
    )

    result = runner.invoke(cli, ["--home", str(home), "setup"])

    assert result.exit_code == 0
    assert "Secrets will be configured later" in result.output
    assert "Verification skipped" in result.output


def test_doctor_suggests_qualified_ollama_model_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    home = tmp_path / "doctor-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "deepagents" else None,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "ollama"} else None,
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, capture_output, text, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )

    result = runner.invoke(cli, ["--home", str(home), "doctor", "--model", "kimi-k2.5:cloud"])

    assert result.exit_code == 1
    assert "Model runtime" in result.output
    assert "ollama:kimi-k2.5:cloud" in result.output


def test_setup_auto_doctor_failure_does_not_abort_setup(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "auto-doctor-home"

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)
    monkeypatch.setattr("olano.cli.main._can_run_post_setup_doctor", lambda: True)

    def fake_run_textual_setup_wizard(**kwargs):
        return (
            {
                "user_name": "Carlos",
                "timezone": "Singapore",
                "preferences": "",
                "secrets_strategy": "later",
                "vault_mode": "",
                "vault_passphrase": "",
                "import_env_vars": [],
                "provider": "ollama",
                "model": "kimi-k2.5:cloud",
                "api_key": "",
                "preset": "safe",
                "mode": "general",
                "archetype": "merli",
                "agent_name": "Merli",
                "emoji": "🦁",
                "description": "General-purpose Olano assistant",
                "system_prompt": "",
                "custom_tools": [],
                "custom_subagents": [],
            },
            False,
        )

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", fake_run_textual_setup_wizard)
    monkeypatch.setattr(
        "olano.cli.main._run_doctor_checks",
        lambda agent_name, model_string, skip_model: (
            [("Model runtime", "FAIL", "ollama:kimi-k2.5:cloud: not installed locally")],
            True,
        ),
    )

    result = runner.invoke(cli, ["--home", str(home), "setup"])

    assert result.exit_code == 0
    assert "Doctor reported a blocking issue" in " ".join(result.output.split())


def test_doctor_prefers_home_default_agent_over_local_shadow(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.config.home import HomeConfig, save_home_config

    home = tmp_path / "home"
    save_home_config(
        HomeConfig(default_agent="merli", preferred_model="openai:gpt-5.4-mini"),
        override=home,
    )

    home_agent_dir = home / "agents" / "merli"
    home_workspace = home_agent_dir / "workspace"
    home_workspace.mkdir(parents=True, exist_ok=True)
    (home_workspace / "AGENTS.md").write_text("# Home\n", encoding="utf-8")
    (home_agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "merli",
                "emoji": "🦁",
                "model": "openai:gpt-5.4-mini",
                "workspace": str(home_workspace),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    local_agent_dir = tmp_path / "agents" / "merli"
    local_workspace = local_agent_dir / "workspace"
    local_workspace.mkdir(parents=True, exist_ok=True)
    (local_workspace / "AGENTS.md").write_text("# Local\n", encoding="utf-8")
    (local_agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "merli",
                "emoji": "🦁",
                "model": "anthropic:claude-sonnet-4-6",
                "workspace": str(local_workspace),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("olano.cli.main._probe_model_connectivity", lambda model: (True, "OK"))
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in {"deepagents", "langchain_openai"} else None,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "ollama"} else None,
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, capture_output, text, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )

    result = runner.invoke(cli, ["--home", str(home), "doctor", "--agent", "merli"])

    assert result.exit_code == 0
    assert "openai:gpt-5.4-mini" in result.output
    assert "home agent 'merli'" in result.output


def test_textual_setup_failure_does_not_fall_back_to_deprecated_setup(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "wizard-fail-home"

    monkeypatch.setattr("olano.cli.main._can_use_fullscreen_setup_wizard", lambda: True)

    def boom(**kwargs):
        raise RuntimeError("broken wizard")

    monkeypatch.setattr("olano.cli.main._run_textual_setup_wizard", boom)

    result = runner.invoke(cli, ["--home", str(home), "setup"])

    assert result.exit_code != 0
    assert "Textual setup wizard failed: broken wizard" in result.output
    assert "falling back to deprecated prompts" not in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--help"], "--home"),
        (["setup", "--help"], "--mode"),
        (["doctor", "--help"], "--skip-model"),
        (["agents", "--help"], "agents install"),
        (["agents", "create", "--help"], "--from-template"),
        (["agents", "install", "--help"], "olano agents install"),
        (["agents", "list", "--help"], "--archetypes"),
        (["vault", "--help"], "olano vault init"),
        (["dashboard", "--help"], "Dashboard port"),
        (["chat", "--help"], "olano chat"),
        (["update", "--help"], "uv tool upgrade olano"),
    ],
)
def test_phase0_help_examples(
    runner: CliRunner,
    args: list[str],
    expected: str,
) -> None:
    result = runner.invoke(cli, args)

    assert result.exit_code == 0
    # Normalize whitespace so Click's line-wrapping doesn't break substring checks.
    assert expected in " ".join(result.output.split())
