"""Textual-based setup flow for Olano.

Run with:
    uv run python -m olano.cli.setup_textual

This module can be run directly for standalone iteration, and is also used by
``olano setup`` as the primary interactive wizard.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from olano.config.home import (
    HomeConfig,
    ensure_olano_home,
    get_olano_home,
    load_home_config,
    save_home_config,
    set_olano_home,
)


@dataclass(slots=True)
class SetupPlan:
    home: Path
    provider: str
    model: str
    api_key: str
    safety_preset: str
    mode: str
    archetype: str
    agent_name: str
    emoji: str
    user_name: str
    timezone: str
    preferences: str
    secrets_strategy: str
    vault_mode: str
    vault_passphrase: str
    import_env_credentials: bool
    skip_verify: bool
    run_doctor: bool
    custom_description: str
    custom_system_prompt: str
    quick_start: bool = False


@dataclass(slots=True)
class SetupOutcome:
    home: Path
    agent_id: str
    entered_name: str
    emoji: str
    verification_model: str
    verification_skipped: bool


def _main_cli():
    from olano.cli import main as main_cli

    return main_cli


def _provider_info() -> dict[str, Any]:
    from olano.models import PROVIDERS

    return PROVIDERS


def _available_archetypes() -> list[str]:
    from olano.archetypes.installer import list_archetypes

    return list_archetypes()


def _ordered_specialists(archetypes: list[str]) -> list[str]:
    preferred = ["lionDev", "lionFinance", "lionMarketing", "lionLife"]
    hidden = {"merli", "default"}
    remaining = [name for name in archetypes if name not in preferred and name not in hidden]
    ordered = [name for name in preferred if name in archetypes]
    ordered.extend(sorted(remaining))
    return ordered


def _default_specialist(archetypes: list[str]) -> str:
    specialists = _ordered_specialists(archetypes)
    return specialists[0] if specialists else "lionDev"


def _normalize_archetype_for_select(archetype: str | None, archetypes: list[str]) -> str:
    specialists = _ordered_specialists(archetypes)
    if archetype and archetype in specialists:
        return archetype
    return specialists[0] if specialists else "lionDev"


def _default_agent_name(mode: str, archetype: str) -> str:
    if mode == "general":
        return "Merli"
    if mode == "specialized":
        return archetype or "lionDev"
    return "Olano"


def _default_emoji(mode: str, archetype: str) -> str:
    if mode == "specialized" and archetype:
        from olano.archetypes.installer import load_archetype_template

        try:
            template = load_archetype_template(archetype)
            return str(template.get("emoji", "🤖"))
        except Exception:
            return "🤖"
    return "🦁"


def _available_vault_modes() -> list[tuple[str, str]]:
    main_cli = _main_cli()
    modes: list[tuple[str, str]] = [("Passphrase", "passphrase")]
    if main_cli._system_keyring_available():
        modes.insert(0, ("System keychain", "keyring"))
    from olano.vault.store import VAULT_KEY_ENV, vault_key_from_env

    if vault_key_from_env():
        insert_at = 1 if modes and modes[0][1] == "keyring" else 0
        modes.insert(insert_at, (VAULT_KEY_ENV, "env"))
    return modes


def _default_model_for_setup(provider: str) -> str:
    if provider == "ollama":
        return "ollama:kimi-k2.5:cloud"
    main_cli = _main_cli()
    return cast(str, main_cli._default_model_for_provider(provider))


_QUICK_START_AGENT_ID = "default"
_QUICK_START_MODEL = "ollama:kimi-k2.5:cloud"
_QUICK_START_SAFETY = "safe"
_QUICK_START_ARCHETYPE = "default"
_QUICK_START_EMOJI = "🤖"
_QUICK_START_SUBAGENTS = {"WebAgent", "BrowserAgent", "GoogleAgent"}


def _resolve_initial_values(args: argparse.Namespace) -> dict[str, Any]:
    main_cli = _main_cli()
    from olano.models import parse_model_string

    providers = _provider_info()
    home_config = load_home_config()
    default_name, default_timezone = main_cli._discover_identity_defaults()

    provider = args.provider or "ollama"
    preferred_model = home_config.preferred_model
    preferred_provider, _ = parse_model_string(preferred_model) if preferred_model else (None, "")
    model = (
        args.model
        or (preferred_model if preferred_provider == provider else None)
        or _default_model_for_setup(provider)
    )
    mode = args.mode or "general"
    archetypes = _available_archetypes()
    selected_archetype = args.archetype or (
        "merli" if mode == "general" else _default_specialist(archetypes)
    )
    archetype = _normalize_archetype_for_select(selected_archetype, archetypes)
    agent_name = args.agent_name or _default_agent_name(mode, archetype)
    emoji = args.emoji or _default_emoji(mode, archetype)
    vault_modes = _available_vault_modes()
    vault_mode = vault_modes[0][1] if vault_modes else "passphrase"
    provider_env_var = providers[provider].env_var if provider in providers else None

    return {
        "home": get_olano_home(),
        "provider": provider,
        "model": model,
        "api_key": args.api_key or "",
        "safety_preset": args.safety_preset or "developer",
        "mode": mode,
        "archetype": archetype,
        "agent_name": agent_name,
        "emoji": emoji,
        "user_name": default_name,
        "timezone": default_timezone,
        "preferences": "",
        "secrets_strategy": "vault",
        "vault_mode": vault_mode,
        "vault_passphrase": "",
        "import_env_credentials": bool(provider_env_var and os.environ.get(provider_env_var)),
        "skip_verify": False,
        "run_doctor": True,
        "custom_description": "General-purpose Olano assistant",
        "custom_system_prompt": f"You are {agent_name}, a helpful AI assistant.",
        "quick_start": True,
        "provider_env_var": provider_env_var or "",
        "vault_modes": vault_modes,
        "archetypes": archetypes,
    }


def _apply_quick_start_defaults(values: dict[str, Any]) -> dict[str, Any]:
    quick = dict(values)
    quick.update(
        {
            "provider": "ollama",
            "model": _QUICK_START_MODEL,
            "api_key": "",
            "safety_preset": _QUICK_START_SAFETY,
            "mode": "general",
            "archetype": _QUICK_START_ARCHETYPE,
            "agent_name": _QUICK_START_AGENT_ID,
            "emoji": _QUICK_START_EMOJI,
            "import_env_credentials": False,
            "skip_verify": False,
            "run_doctor": True,
            "custom_description": "Fresh Olano starter agent",
            "custom_system_prompt": (
                "You are a newly created Olano assistant with no fixed identity yet. "
                "Use early conversations to learn who the user is, what they are building, "
                "how they like to work, and what kind of assistant you should become."
            ),
        }
    )
    return quick


def _make_plan(values: dict[str, Any]) -> SetupPlan:
    values = _apply_quick_start_defaults(values) if values.get("quick_start") else dict(values)
    provider = str(values["provider"])
    mode = str(values["mode"])
    archetype = str(values["archetype"])
    agent_name = (str(values["agent_name"]).strip() or _default_agent_name(mode, archetype)).strip()
    emoji = (str(values["emoji"]).strip() or _default_emoji(mode, archetype)).strip()

    return SetupPlan(
        home=Path(values["home"]),
        provider=provider,
        model=str(values["model"]).strip(),
        api_key=str(values["api_key"]),
        safety_preset=str(values["safety_preset"]),
        mode=mode,
        archetype=archetype,
        agent_name=agent_name,
        emoji=emoji,
        user_name=(str(values["user_name"]).strip() or "Olano User"),
        timezone=(str(values["timezone"]).strip() or "UTC"),
        preferences=str(values["preferences"]).strip(),
        secrets_strategy=str(values["secrets_strategy"]),
        vault_mode=str(values["vault_mode"]),
        vault_passphrase=str(values["vault_passphrase"]),
        import_env_credentials=bool(values["import_env_credentials"]),
        skip_verify=bool(values["skip_verify"]),
        run_doctor=bool(values["run_doctor"]),
        custom_description=(
            str(values["custom_description"]).strip() or "General-purpose Olano assistant"
        ),
        custom_system_prompt=(
            str(values["custom_system_prompt"]).strip()
            or f"You are {agent_name}, a helpful AI assistant."
        ),
        quick_start=bool(values.get("quick_start")),
    )


def plan_to_wizard_state(plan: SetupPlan) -> dict[str, Any]:
    provider_env_var = _provider_env_var(plan.provider)
    import_env_vars = [provider_env_var] if plan.import_env_credentials and provider_env_var else []
    return {
        "user_name": plan.user_name,
        "timezone": plan.timezone,
        "preferences": plan.preferences,
        "secrets_strategy": plan.secrets_strategy,
        "vault_mode": plan.vault_mode,
        "vault_passphrase": plan.vault_passphrase,
        "import_env_vars": import_env_vars,
        "provider": plan.provider,
        "model": plan.model,
        "api_key": plan.api_key,
        "preset": plan.safety_preset,
        "mode": plan.mode,
        "archetype": plan.archetype,
        "agent_name": plan.agent_name,
        "emoji": plan.emoji,
        "description": plan.custom_description,
        "system_prompt": plan.custom_system_prompt,
        "custom_tools": [],
        "custom_subagents": [],
        "quick_start": plan.quick_start,
    }


def _validate_plan(plan: SetupPlan) -> list[str]:
    errors: list[str] = []
    if not plan.model:
        errors.append("Model is required.")
    if not plan.agent_name:
        errors.append("Agent name is required.")
    if plan.mode == "specialized" and not plan.archetype:
        errors.append("Choose a specialist archetype.")
    if (
        plan.secrets_strategy == "vault"
        and plan.vault_mode == "passphrase"
        and not plan.vault_passphrase
    ):
        errors.append("Vault passphrase is required when vault mode is passphrase.")
    return errors


def _render_summary(values: dict[str, Any]) -> str:
    plan = _make_plan(values)
    main_cli = _main_cli()
    providers = _provider_info()
    provider_name = providers[plan.provider].name if plan.provider in providers else plan.provider
    env_var = providers[plan.provider].env_var if plan.provider in providers else None

    if plan.quick_start:
        agent_line = f"{plan.emoji} default (fresh starter)"
    elif plan.mode == "general":
        agent_line = f"{plan.emoji} {plan.agent_name} (merli)"
    elif plan.mode == "specialized":
        agent_line = f"{plan.emoji} {plan.agent_name} ({plan.archetype})"
    else:
        agent_line = f"{plan.emoji} {plan.agent_name} (custom)"

    secret_line = plan.secrets_strategy
    if plan.secrets_strategy == "vault":
        secret_line = f"vault via {plan.vault_mode}"
    elif plan.secrets_strategy == "later":
        secret_line = "configure later"

    command_prefix = main_cli._setup_command_prefix(plan.home)
    agent_id = (
        _QUICK_START_AGENT_ID
        if plan.quick_start
        else (main_cli._slugify_agent_name(plan.agent_name) or "merli")
    )
    doctor_command = f"{command_prefix} doctor --agent {agent_id}"
    dashboard_command = f"{command_prefix} dashboard"

    return "\n".join(
        [
            "Ready to install",
            "",
            f"Home: {plan.home}",
            "Personal identity: handled later in chat onboarding",
            f"Agent: {agent_line}",
            f"Model: {provider_name} / {plan.model}",
            f"Safety: {main_cli._safety_label(plan.safety_preset)}",
            f"Quick start: {'yes' if plan.quick_start else 'no'}",
            f"Secrets: {secret_line}",
            f"Provider env: {env_var or 'none'}",
            f"Import env key: {'yes' if plan.import_env_credentials else 'no'}",
            f"Verify model: {'skip' if plan.skip_verify else 'run'}",
            f"Doctor: {'run' if plan.run_doctor else 'skip'}",
            "",
            "After setup",
            "",
            doctor_command,
            dashboard_command,
        ]
    )


def _install_quick_start_default_agent(plan: SetupPlan) -> Path:
    import json

    from olano.security.sharing import default_shared_grant

    main_cli = _main_cli()
    written_path = main_cli._install_or_update_archetype_agent(
        archetype_name=_QUICK_START_ARCHETYPE,
        agent_id=_QUICK_START_AGENT_ID,
        emoji=_QUICK_START_EMOJI,
        security_preset=_QUICK_START_SAFETY,
        overwrite_existing=False,
        main_model=_QUICK_START_MODEL,
        subagent_models={},
        force_all_subagents=True,
    )
    config_data = json.loads(written_path.read_text(encoding="utf-8"))
    tools = [tool for tool in config_data.get("tools", []) if tool]
    if "safe_shell" not in tools:
        tools.insert(0, "safe_shell")
    config_data["tools"] = tools
    config_data["description"] = "Fresh Olano starter agent"
    config_data["shared_paths"] = [default_shared_grant().model_dump()]
    config_data["subagents"] = [
        subagent
        for subagent in config_data.get("subagents", [])
        if subagent.get("name") in _QUICK_START_SUBAGENTS
    ]
    config_data["name"] = _QUICK_START_AGENT_ID
    config_data["emoji"] = _QUICK_START_EMOJI
    written_path.write_text(
        json.dumps(config_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cast(Path, written_path)


_STEP_ORDER = ["identity", "secrets", "model", "safety", "agent", "review"]
_STEP_TITLES = {
    "identity": "Your identity",
    "secrets": "Secrets",
    "model": "Model",
    "safety": "Safety",
    "agent": "Your agent identity",
    "review": "Review",
}
_STEP_HINTS = {
    "identity": "Personal onboarding is better handled conversationally inside chat.",
    "secrets": "Choose how Olano should handle credentials and private data.",
    "model": "Choose the provider and model for your first agent.",
    "safety": "Pick the default safety posture for the first agent.",
    "agent": "Choose the kind of agent you want to start with.",
    "review": "Review the configuration, then launch setup.",
}


def _provider_select_options() -> list[tuple[str, str]]:
    providers = _provider_info()
    main_cli = _main_cli()
    return [(providers[key].name, key) for key in main_cli._SETUP_PROVIDER_CHOICES]


def _provider_env_var(provider: str) -> str | None:
    providers = _provider_info()
    info = providers.get(provider)
    return info.env_var if info else None


def _safety_select_options() -> list[tuple[str, str]]:
    main_cli = _main_cli()
    return [(option["label"], option["key"]) for option in main_cli._SETUP_SAFETY_OPTIONS]


def _safety_help_text(preset: str) -> str:
    main_cli = _main_cli()
    for option in main_cli._SETUP_SAFETY_OPTIONS:
        if option["key"] == preset:
            return f"{option['label']} — {option['summary']}\n{option['detail']}"
    return ""


def _agent_mode_options() -> list[tuple[str, str]]:
    main_cli = _main_cli()
    return [(option["label"], option["key"]) for option in main_cli._SETUP_AGENT_OPTIONS]


def _agent_mode_help_text(mode: str) -> str:
    main_cli = _main_cli()
    for option in main_cli._SETUP_AGENT_OPTIONS:
        if option["key"] == mode:
            return f"{option['label']} — {option['summary']}\n{option['detail']}"
    return ""


def _specialist_options(archetypes: list[str]) -> list[tuple[str, str]]:
    return [(name, name) for name in _ordered_specialists(archetypes)] or [("lionDev", "lionDev")]


def _specialist_help_text(archetype: str) -> str:
    from olano.archetypes.installer import load_archetype_template

    try:
        template = load_archetype_template(archetype)
    except Exception:
        return ""
    return str(template.get("description", ""))


def _model_picker_state(
    provider: str, current_model: str
) -> tuple[list[tuple[str, str]], str, str]:
    main_cli = _main_cli()
    suggestions = list(main_cli._suggested_models_for_provider(provider))
    raw_current = main_cli._model_name_for_provider(provider, current_model)
    raw_current = raw_current or main_cli._model_name_for_provider(
        provider, main_cli._default_model_for_provider(provider)
    )

    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in [*suggestions, raw_current]:
        if candidate and candidate not in seen:
            options.append((candidate, candidate))
            seen.add(candidate)
    options.append(("Custom model", "__custom__"))

    if raw_current in seen:
        return options, raw_current, raw_current
    return options, "__custom__", raw_current


def _normalize_model_from_choice(provider: str, model_choice: str, custom_model: str) -> str:
    main_cli = _main_cli()
    raw = custom_model.strip() if model_choice == "__custom__" else model_choice.strip()
    return cast(str, main_cli._normalize_model_string(provider, raw))


def _page_widget_id(page: str) -> str:
    return f"#page-{page.replace('_', '-')}"


class OlanoSetupPrototype(App[SetupPlan | None]):
    """Minimal page-by-page Textual wizard for Olano setup."""

    CSS = """
    Screen {
        background: #f5f5f7;
        color: #111111;
    }

    #shell {
        align: center middle;
        height: 1fr;
        width: 1fr;
        padding: 2;
    }

    #card {
        width: 84;
        max-width: 84;
        height: auto;
        background: #ffffff;
        border: solid #d9dde3;
        padding: 1 2;
    }

    #eyebrow {
        color: #6b7280;
    }

    #page-counter {
        color: #6b7280;
        text-align: right;
        width: 1fr;
    }

    #title {
        color: #111111;
        text-style: bold;
        margin: 1 0 0 0;
    }

    #subtitle {
        color: #6b7280;
        margin: 0 0 1 0;
    }

    #status {
        color: #b42318;
        min-height: 1;
        margin: 0 0 1 0;
    }

    .page {
        height: auto;
    }

    .field-label {
        color: #111111;
        text-style: bold;
        margin: 1 0 0 0;
    }

    .field-help {
        color: #6b7280;
        margin: 0 0 1 0;
    }

    Input, Select, TextArea {
        margin: 0 0 1 0;
        border: solid #d9dde3;
        background: #ffffff;
        color: #111111;
    }

    Input {
        height: 1;
        min-height: 1;
        padding: 0 1;
    }

    TextArea {
        height: 7;
    }

    #review-summary {
        color: #111111;
        margin: 1 0 1 0;
    }

    .toggle-row {
        height: auto;
        margin: 0 0 1 0;
    }

    .toggle-label {
        width: 1fr;
        color: #111111;
        padding: 0 1 0 0;
    }

    #actions {
        height: auto;
        margin: 1 0 0 0;
    }

    Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "cancel", "Cancel"),
        ("left", "back", "Back"),
        ("right", "next", "Next"),
        ("f5", "submit", "Run Setup"),
    ]

    def __init__(self, initial_values: dict[str, Any]) -> None:
        super().__init__()
        self.initial_values = initial_values
        self.page_index = 0

    def compose(self) -> ComposeResult:
        vault_modes = self.initial_values["vault_modes"] or [("Passphrase", "passphrase")]
        with Vertical(id="shell"):
            with Vertical(id="card"):
                with Horizontal():
                    yield Static("Olano Setup", id="eyebrow")
                    yield Static("", id="page-counter")
                yield Static("", id="title")
                yield Static("", id="subtitle")
                yield Static("", id="status")

                with Vertical(id="page-identity", classes="page"):
                    yield Static(
                        "Personal onboarding can happen naturally in chat. Setup stays focused on technical configuration.",
                        classes="field-help",
                    )
                    yield Static(
                        "Once chat starts, the agent can learn your identity and preferences conversationally.",
                        classes="field-help",
                    )

                with Vertical(id="page-secrets-strategy", classes="page"):
                    yield Static("Secrets strategy", classes="field-label")
                    yield Static(
                        "Choose how Olano should handle credentials and private data.",
                        classes="field-help",
                    )
                    yield Select(
                        [
                            ("Local encrypted vault", "vault"),
                            ("Environment variables", "env"),
                            ("Configure later", "later"),
                        ],
                        value=self.initial_values["secrets_strategy"],
                        id="secrets-strategy",
                        allow_blank=False,
                    )

                with Vertical(id="page-vault-mode", classes="page"):
                    yield Static("Vault unlock mode", classes="field-label")
                    yield Static(
                        "Choose how Olano should unlock the local encrypted vault on this machine.",
                        classes="field-help",
                    )
                    yield Select(
                        vault_modes,
                        value=self.initial_values["vault_mode"],
                        id="vault-mode",
                        allow_blank=False,
                    )

                with Vertical(id="page-vault-passphrase", classes="page"):
                    yield Static("Vault passphrase", classes="field-label")
                    yield Static(
                        "Use a passphrase only if you do not want to rely on the system keychain.",
                        classes="field-help",
                    )
                    yield Input(
                        value=self.initial_values["vault_passphrase"],
                        password=True,
                        id="vault-passphrase",
                    )
                    yield Static("Confirm vault passphrase", classes="field-label")
                    yield Input(password=True, id="vault-passphrase-confirm")

                with Vertical(id="page-import-env", classes="page"):
                    yield Static("Import existing credential", classes="field-label")
                    yield Static("", classes="field-help", id="import-env-help")
                    with Horizontal(classes="toggle-row"):
                        yield Static("", classes="toggle-label", id="import-env-label")
                        yield Switch(
                            value=self.initial_values["import_env_credentials"],
                            id="import-env",
                        )

                with Vertical(id="page-provider", classes="page"):
                    yield Static("Model provider", classes="field-label")
                    yield Static(
                        "Choose the provider for the first agent.",
                        classes="field-help",
                    )
                    yield Select(
                        _provider_select_options(),
                        value=self.initial_values["provider"],
                        id="provider",
                        allow_blank=False,
                    )

                with Vertical(id="page-model", classes="page"):
                    yield Static("Model", classes="field-label")
                    yield Static(
                        "Pick a suggested model or enter a custom one.",
                        classes="field-help",
                    )
                    yield Select(
                        [("Custom model", "__custom__")],
                        value="__custom__",
                        id="model-choice",
                        allow_blank=False,
                    )
                    yield Input(id="custom-model")

                with Vertical(id="page-api-key", classes="page"):
                    yield Static("Provider API key", classes="field-label")
                    yield Static("", classes="field-help", id="api-key-help")
                    yield Input(
                        value=self.initial_values["api_key"],
                        password=True,
                        id="api-key",
                    )

                with Vertical(id="page-safety", classes="page"):
                    yield Static("Safety preset", classes="field-label")
                    yield Static(
                        "Choose the default safety posture for the first agent.",
                        classes="field-help",
                    )
                    yield Select(
                        _safety_select_options(),
                        value=self.initial_values["safety_preset"],
                        id="safety-preset",
                        allow_blank=False,
                    )
                    yield Static("", classes="field-help", id="safety-help")

                with Vertical(id="page-agent-mode", classes="page"):
                    yield Static("Your agent identity", classes="field-label")
                    yield Static(
                        "Choose what kind of agent you want to start with.",
                        classes="field-help",
                    )
                    yield Select(
                        _agent_mode_options(),
                        value=self.initial_values["mode"],
                        id="mode",
                        allow_blank=False,
                    )
                    yield Static("", classes="field-help", id="mode-help")

                with Vertical(id="page-specialist", classes="page"):
                    yield Static("Built-in specialist", classes="field-label")
                    yield Static(
                        "Pick a specialist archetype.",
                        classes="field-help",
                    )
                    yield Select(
                        _specialist_options(self.initial_values["archetypes"]),
                        value=self.initial_values["archetype"],
                        id="archetype",
                        allow_blank=False,
                    )
                    yield Static("", classes="field-help", id="archetype-help")

                with Vertical(id="page-agent-name", classes="page"):
                    yield Static("Agent name", classes="field-label")
                    yield Static(
                        "Choose the visible name for the agent.",
                        classes="field-help",
                    )
                    yield Input(value=self.initial_values["agent_name"], id="agent-name")

                with Vertical(id="page-emoji", classes="page"):
                    yield Static("Emoji", classes="field-label")
                    yield Static(
                        "Choose the emoji shown for this agent.",
                        classes="field-help",
                    )
                    yield Input(value=self.initial_values["emoji"], id="emoji")

                with Vertical(id="page-custom-description", classes="page"):
                    yield Static("Custom description", classes="field-label")
                    yield Static(
                        "Only used when you create a custom agent base.",
                        classes="field-help",
                    )
                    yield Input(
                        value=self.initial_values["custom_description"],
                        id="custom-description",
                    )

                with Vertical(id="page-custom-prompt", classes="page"):
                    yield Static("Custom system prompt", classes="field-label")
                    yield Static(
                        "Only used when you create a custom agent base.",
                        classes="field-help",
                    )
                    yield TextArea(
                        self.initial_values["custom_system_prompt"],
                        id="custom-system-prompt",
                    )

                with Vertical(id="page-review", classes="page"):
                    yield Static("Review", classes="field-label")
                    yield Static(
                        "Choose what should happen after setup completes.",
                        classes="field-help",
                    )
                    with Horizontal(classes="toggle-row"):
                        yield Static("Skip live model verification", classes="toggle-label")
                        yield Switch(value=self.initial_values["skip_verify"], id="skip-verify")
                    with Horizontal(classes="toggle-row"):
                        yield Static("Run doctor after setup", classes="toggle-label")
                        yield Switch(value=self.initial_values["run_doctor"], id="run-doctor")
                    yield Static("", id="review-summary")

                with Horizontal(id="actions"):
                    yield Button("Cancel", id="cancel", variant="default")
                    yield Button("Back", id="back", variant="default")
                    yield Button("Next", id="next", variant="primary")
                    yield Button("Run Setup", id="submit", variant="success")

    def on_mount(self) -> None:
        self.title = "Olano Setup"
        self.sub_title = "Textual wizard prototype"
        self._refresh_model_options(reset_from_initial=True)
        self._sync_provider_help()
        self._sync_safety_help()
        self._sync_agent_help(reset_defaults=False)
        self._sync_import_env_page()
        self._render_current_page()
        self._refresh_review()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "back":
            self._go_back()
        elif event.button.id == "next":
            self.action_next()
        elif event.button.id == "submit":
            self.action_submit()

    def on_input_changed(self, _: Input.Changed) -> None:
        self._clear_status()
        self._refresh_review()

    def on_text_area_changed(self, _: TextArea.Changed) -> None:
        self._clear_status()
        self._refresh_review()

    def on_switch_changed(self, _: Switch.Changed) -> None:
        self._render_current_page()
        self._refresh_review()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider":
            self._refresh_model_options(reset_from_initial=False)
            self._sync_provider_help()
            self._sync_import_env_page()
            self._render_current_page()
        elif event.select.id == "model-choice":
            self.query_one("#custom-model", Input).display = (
                str(self.query_one("#model-choice", Select).value) == "__custom__"
            )
            self._refresh_review()
        elif event.select.id == "safety-preset":
            self._sync_safety_help()
        elif event.select.id == "mode":
            self._sync_agent_help(reset_defaults=True)
            self._render_current_page()
        elif event.select.id == "archetype":
            self._sync_agent_help(reset_defaults=True)
        elif event.select.id in {"secrets-strategy", "vault-mode"}:
            self._render_current_page()
            self._sync_import_env_page()
        self._clear_status()
        self._refresh_review()

    def action_cancel(self) -> None:
        self.exit(None)

    async def action_back(self) -> None:
        self._go_back()

    def _go_back(self) -> None:
        self._clear_status()
        if self.page_index > 0:
            self.page_index -= 1
            self._render_current_page()

    def action_next(self) -> None:
        errors = self._validate_current_page()
        if errors:
            self._show_errors(errors)
            return
        if self.page_index < len(self._page_sequence()) - 1:
            self.page_index += 1
            self._clear_status()
            self._render_current_page()

    def action_submit(self) -> None:
        errors = self._validate_all_pages()
        if errors:
            first_invalid = self._first_invalid_page_index()
            if first_invalid is not None:
                self.page_index = first_invalid
                self._render_current_page()
            self._show_errors(errors[:3])
            return
        self.exit(_make_plan(self._collect_values()))

    def _page_sequence(self) -> list[str]:
        values = self._collect_values()
        pages = ["identity", "secrets_strategy"]
        if values["secrets_strategy"] == "vault":
            pages.append("vault_mode")
            if values["vault_mode"] == "passphrase":
                pages.append("vault_passphrase")
            if self._provider_env_var_detected():
                pages.append("import_env")
        pages.extend(["provider", "model"])
        if self._should_show_api_key_page():
            pages.append("api_key")
        pages.extend(["safety", "agent_mode"])
        if values["mode"] == "specialized":
            pages.append("specialist")
        pages.extend(["agent-name", "emoji"])
        if values["mode"] == "custom":
            pages.extend(["custom-description", "custom-prompt"])
        pages.append("review")
        return pages

    def _page_titles(self) -> dict[str, tuple[str, str]]:
        return {
            "identity": ("Your identity", "Personal onboarding happens later in chat."),
            "secrets_strategy": ("Secrets", "Choose how Olano should handle credentials."),
            "vault_mode": ("Secrets", "Choose how the vault should unlock."),
            "vault_passphrase": ("Secrets", "Set a vault passphrase."),
            "import_env": ("Secrets", "Import an existing provider credential if available."),
            "provider": ("Model", "Choose the model provider."),
            "model": ("Model", "Choose the model name."),
            "api_key": ("Model", "Add a provider credential if needed."),
            "safety": ("Safety", "Choose the default safety preset."),
            "agent_mode": ("Your agent identity", "Choose the starting agent type."),
            "specialist": ("Your agent identity", "Choose a built-in specialist."),
            "agent-name": ("Your agent identity", "Choose the agent name."),
            "emoji": ("Your agent identity", "Choose the agent emoji."),
            "custom-description": ("Your agent identity", "Set the custom agent description."),
            "custom-prompt": ("Your agent identity", "Set the custom system prompt."),
            "review": ("Review", "Confirm the setup and launch behavior."),
        }

    def _render_current_page(self) -> None:
        pages = self._page_sequence()
        if self.page_index >= len(pages):
            self.page_index = len(pages) - 1
        current = pages[self.page_index]

        for page_id in [
            "identity",
            "secrets_strategy",
            "vault_mode",
            "vault_passphrase",
            "import_env",
            "provider",
            "model",
            "api_key",
            "safety",
            "agent_mode",
            "specialist",
            "agent-name",
            "emoji",
            "custom-description",
            "custom-prompt",
            "review",
        ]:
            self.query_one(_page_widget_id(page_id), Vertical).display = page_id == current

        title, subtitle = self._page_titles()[current]
        self.query_one("#title", Static).update(title)
        self.query_one("#subtitle", Static).update(subtitle)
        self.query_one("#page-counter", Static).update(f"{self.page_index + 1} of {len(pages)}")

        self.query_one("#back", Button).disabled = self.page_index == 0
        self.query_one("#next", Button).display = current != "review"
        self.query_one("#submit", Button).display = current == "review"
        self._refresh_review()

    def _collect_values(self) -> dict[str, Any]:
        provider = str(self.query_one("#provider", Select).value)
        model_choice = str(self.query_one("#model-choice", Select).value)
        custom_model = self.query_one("#custom-model", Input).value
        return {
            "home": self.initial_values["home"],
            "provider": provider,
            "model": _normalize_model_from_choice(provider, model_choice, custom_model),
            "api_key": self.query_one("#api-key", Input).value,
            "safety_preset": str(self.query_one("#safety-preset", Select).value),
            "mode": str(self.query_one("#mode", Select).value),
            "archetype": str(self.query_one("#archetype", Select).value),
            "agent_name": self.query_one("#agent-name", Input).value,
            "emoji": self.query_one("#emoji", Input).value,
            "user_name": self.initial_values["user_name"],
            "timezone": self.initial_values["timezone"],
            "preferences": self.initial_values["preferences"],
            "secrets_strategy": str(self.query_one("#secrets-strategy", Select).value),
            "vault_mode": str(self.query_one("#vault-mode", Select).value),
            "vault_passphrase": self.query_one("#vault-passphrase", Input).value,
            "import_env_credentials": self.query_one("#import-env", Switch).value,
            "skip_verify": self.query_one("#skip-verify", Switch).value,
            "run_doctor": self.query_one("#run-doctor", Switch).value,
            "custom_description": self.query_one("#custom-description", Input).value,
            "custom_system_prompt": self.query_one("#custom-system-prompt", TextArea).text,
        }

    def _clear_status(self) -> None:
        self.query_one("#status", Static).update("")

    def _show_errors(self, errors: list[str]) -> None:
        self.query_one("#status", Static).update("\n".join(errors))

    def _validate_current_page(self) -> list[str]:
        return self._validate_page(self._page_sequence()[self.page_index])

    def _validate_all_pages(self) -> list[str]:
        errors: list[str] = []
        for page in self._page_sequence():
            errors.extend(self._validate_page(page))
        return errors

    def _first_invalid_page_index(self) -> int | None:
        pages = self._page_sequence()
        for index, page in enumerate(pages):
            if self._validate_page(page):
                return index
        return None

    def _validate_page(self, page: str) -> list[str]:
        values = self._collect_values()
        if page == "vault_passphrase":
            passphrase = self.query_one("#vault-passphrase", Input).value
            confirm = self.query_one("#vault-passphrase-confirm", Input).value
            if not passphrase:
                return ["Vault passphrase is required when vault mode is passphrase."]
            if passphrase != confirm:
                return ["Vault passphrase confirmation does not match."]
        if page == "model" and not str(values["model"]).strip():
            return ["Model is required."]
        if page == "agent-name" and not str(values["agent_name"]).strip():
            return ["Agent name is required."]
        if page == "emoji" and not str(values["emoji"]).strip():
            return ["Emoji is required."]
        return []

    def _provider_env_var_detected(self) -> bool:
        env_var = _provider_env_var(str(self.query_one("#provider", Select).value))
        return bool(env_var and os.environ.get(env_var))

    def _should_show_api_key_page(self) -> bool:
        provider = str(self.query_one("#provider", Select).value)
        env_var = _provider_env_var(provider)
        if env_var is None:
            return False
        return not (
            self._collect_values()["secrets_strategy"] == "vault"
            and self.query_one("#import-env", Switch).value
            and self._provider_env_var_detected()
        )

    def _refresh_model_options(self, *, reset_from_initial: bool) -> None:
        provider = str(self.query_one("#provider", Select).value)
        current_model = (
            self.initial_values["model"] if reset_from_initial else self._collect_values()["model"]
        )
        options, selected, custom_value = _model_picker_state(provider, str(current_model))
        model_choice = self.query_one("#model-choice", Select)
        model_choice.set_options(options)
        model_choice.value = selected
        self.query_one("#custom-model", Input).value = custom_value
        self.query_one("#custom-model", Input).display = selected == "__custom__"

    def _sync_provider_help(self) -> None:
        provider = str(self.query_one("#provider", Select).value)
        provider_name = next(
            label for label, value in _provider_select_options() if value == provider
        )
        env_var = _provider_env_var(provider)
        help_text = (
            f"Optional. Hidden input. Olano will store this securely or use {env_var} if it already exists."
            if env_var
            else f"{provider_name} does not require an API key."
        )
        self.query_one("#api-key-help", Static).update(help_text)

    def _sync_safety_help(self) -> None:
        preset = str(self.query_one("#safety-preset", Select).value)
        self.query_one("#safety-help", Static).update(_safety_help_text(preset))

    def _sync_agent_help(self, *, reset_defaults: bool) -> None:
        mode = str(self.query_one("#mode", Select).value)
        archetype = str(self.query_one("#archetype", Select).value)
        self.query_one("#mode-help", Static).update(_agent_mode_help_text(mode))
        self.query_one("#archetype-help", Static).update(_specialist_help_text(archetype))
        if reset_defaults:
            agent_name = self.query_one("#agent-name", Input)
            emoji = self.query_one("#emoji", Input)
            if not agent_name.value.strip() or agent_name.value in {
                "Merli",
                "lionDev",
                "lionFinance",
                "lionMarketing",
                "lionLife",
                "Olano",
            }:
                agent_name.value = _default_agent_name(mode, archetype)
            if not emoji.value.strip() or emoji.value in {"🦁", "🤖"}:
                emoji.value = _default_emoji(mode, archetype)

    def _sync_import_env_page(self) -> None:
        provider = str(self.query_one("#provider", Select).value)
        env_var = _provider_env_var(provider)
        if env_var and os.environ.get(env_var):
            self.query_one("#import-env-label", Static).update(f"Import detected {env_var}")
            self.query_one("#import-env-help", Static).update(
                "Olano can copy the existing environment secret into the local encrypted vault without showing it."
            )
        else:
            self.query_one("#import-env-label", Static).update("No provider credential detected")
            self.query_one("#import-env-help", Static).update(
                "No matching environment variable is currently available for import."
            )

    def _refresh_review(self) -> None:
        self.query_one("#review-summary", Static).update(_render_summary(self._collect_values()))


def run_setup_textual_prototype(args: argparse.Namespace) -> SetupPlan | None:
    from olano.cli.setup_textual_app import OlanoSetupPrototype

    initial_values = _resolve_initial_values(args)
    app = OlanoSetupPrototype(initial_values)
    return app.run()


def apply_setup_plan(plan: SetupPlan) -> SetupOutcome:
    main_cli = _main_cli()
    console = main_cli.console
    providers = _provider_info()
    home_paths = ensure_olano_home(plan.home)
    home_config = load_home_config()

    console.print()
    console.print(
        main_cli.Panel(
            "[bold #7cc6ff]🦁  Olano Setup Prototype[/bold #7cc6ff]\n\n"
            "[#dbeafe]Applying the Textual setup plan.[/#dbeafe]",
            border_style="#4c7eff",
            padding=(1, 3),
        )
    )
    console.print()

    selected_archetype = "merli" if plan.mode == "general" else plan.archetype
    entered_emoji = plan.emoji or _default_emoji(plan.mode, selected_archetype)
    agent_id = (
        _QUICK_START_AGENT_ID
        if plan.quick_start
        else (main_cli._slugify_agent_name(plan.agent_name) or "merli")
    )

    active_vault_mode = "disabled"
    imported_env_vars: list[str] = []
    provider_env_var = providers[plan.provider].env_var if plan.provider in providers else None
    if plan.import_env_credentials and provider_env_var and os.environ.get(provider_env_var):
        imported_env_vars = [provider_env_var]

    vault = None
    if plan.secrets_strategy == "vault":
        try:
            vault, vault_status = _initialize_vault_for_plan(plan)
            active_vault_mode = plan.vault_mode
            console.print(f"[green]✓[/green]  {vault_status}")
            if imported_env_vars:
                imported = _import_env_credentials(vault, imported_env_vars)
                if imported:
                    console.print(
                        f"[green]✓[/green]  Imported {', '.join(imported)} into the local encrypted vault."
                    )
        except Exception as exc:
            raise RuntimeError(f"Vault initialization failed: {exc}") from exc
    elif plan.secrets_strategy == "env":
        console.print("[green]✓[/green]  Secrets strategy: [bold]environment variables[/bold].")
    else:
        console.print("[yellow]⚠[/yellow]  Secrets will be configured later.")
    console.print()

    if plan.api_key:
        storage_result = _store_provider_credential_for_plan(
            plan.provider,
            plan.api_key,
            vault=vault
            if plan.secrets_strategy == "vault" and active_vault_mode != "ephemeral"
            else None,
        )
        console.print(f"[green]✓[/green]  {storage_result}")
    elif provider_env_var and os.environ.get(provider_env_var):
        console.print(
            f"[green]✓[/green]  Using existing [bold]{provider_env_var}[/bold] from the environment."
        )
    elif provider_env_var:
        console.print(f"[yellow]⚠[/yellow]  {provider_env_var} is not configured yet.")
    else:
        console.print(
            f"[green]✓[/green]  {providers[plan.provider].name} does not require an API key."
        )
    console.print()

    if plan.quick_start:
        installed_path = _install_quick_start_default_agent(plan)
    elif plan.mode in {"general", "specialized"}:
        installed_path = main_cli._install_or_update_archetype_agent(
            archetype_name=selected_archetype or "merli",
            agent_id=agent_id,
            emoji=entered_emoji,
            security_preset=plan.safety_preset,
            overwrite_existing=False,
            main_model=plan.model,
            subagent_models={},
            force_all_subagents=True,
            # Only a name that differs from its own slug is a human display
            # name — "Merli" survives, a typed slug defers to the archetype's
            # label exactly as the dashboard install does.
            display_name=(
                plan.agent_name
                if plan.agent_name
                and main_cli._slugify_agent_name(plan.agent_name) != plan.agent_name
                else ""
            ),
        )
    else:
        installed_path = main_cli._build_custom_agent(
            agent_name=agent_id,
            display_name=plan.agent_name or "",
            model=plan.model,
            description=plan.custom_description,
            emoji=entered_emoji,
            security_preset=plan.safety_preset,
            system_prompt=plan.custom_system_prompt,
            tools=["safe_shell", "node_run", "web_search", "page_read", "http_request"],
            subagents=[],
            overwrite_existing=False,
        )

    workspace_root = installed_path.parent / "workspace"
    if not plan.quick_start:
        main_cli._write_setup_profile(
            workspace_root,
            plan.user_name,
            plan.timezone,
            plan.preferences,
            include_name=False,
        )

    console.print(
        f"[green]✓[/green]  Agent installed at [{main_cli._SETUP_HINT}]{installed_path.parent}[/{main_cli._SETUP_HINT}]"
    )
    console.print(
        f"[green]✓[/green]  Workspace ready at [{main_cli._SETUP_HINT}]{workspace_root}[/{main_cli._SETUP_HINT}]"
    )
    console.print()

    save_home_config(
        HomeConfig(
            default_agent=agent_id,
            preferred_model=plan.model,
            runtime_api_port=home_config.runtime_api_port,
            dashboard_port=home_config.dashboard_port,
        )
    )

    can_verify_model = main_cli._credentials_available_for_setup(
        plan.provider,
        secrets_strategy=plan.secrets_strategy,
        selected_api_key=plan.api_key or None,
        imported_env_vars=imported_env_vars,
    )
    verification_skipped = plan.skip_verify or not can_verify_model
    if not verification_skipped:
        ok, detail = main_cli._probe_model_connectivity(plan.model)
        console.print(
            main_cli.Panel(
                "\n".join(
                    [
                        f"[bold]{'PASS' if ok else 'WARN'}[/bold]",
                        f"Model: [bold]{plan.model}[/bold]",
                        f"Detail: {detail}",
                    ]
                ),
                title="Model Verification",
                border_style="green" if ok else "yellow",
                padding=(1, 2),
            )
        )
        console.print()
    elif not plan.skip_verify and not can_verify_model:
        console.print(
            main_cli.Panel(
                "\n".join(
                    [
                        "[bold]SKIPPED[/bold]",
                        f"Model: [bold]{plan.model}[/bold]",
                        "Reason: credentials are not configured yet.",
                    ]
                ),
                title="Model Verification",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        console.print()

    command_prefix = main_cli._setup_command_prefix(home_paths.root)
    console.print(
        main_cli.Panel(
            "\n".join(
                [
                    f"[bold #7cc6ff]{entered_emoji} {agent_id}[/bold #7cc6ff] is ready.",
                    "",
                    "[bold]Commands:[/bold]",
                    f"  {command_prefix} dashboard",
                    f"  {command_prefix} doctor --agent {agent_id}",
                    f"  {command_prefix} agents list",
                ]
            ),
            title="Prototype Complete",
            border_style="#4c7eff",
            padding=(1, 2),
        )
    )
    console.print()

    return SetupOutcome(
        home=home_paths.root,
        agent_id=agent_id,
        entered_name=plan.agent_name,
        emoji=entered_emoji,
        verification_model=plan.model,
        verification_skipped=verification_skipped,
    )


def _initialize_vault_for_plan(plan: SetupPlan):
    from olano.vault.store import VaultStore, default_vault_path

    vault_path = default_vault_path()
    existed = vault_path.exists()

    if plan.vault_mode == "passphrase":
        if not plan.vault_passphrase:
            raise RuntimeError("Vault passphrase is required.")
        vault = VaultStore.open(
            passphrase=plan.vault_passphrase,
            db_path=vault_path,
            use_keyring=False,
        )
        label = "vault passphrase"
    elif plan.vault_mode == "env":
        from olano.vault.store import VAULT_KEY_ENV, vault_key_from_env

        env_key = vault_key_from_env()
        if not env_key:
            raise RuntimeError(f"{VAULT_KEY_ENV} is not set.")
        vault = VaultStore.open(passphrase=env_key, db_path=vault_path, use_keyring=False)
        label = VAULT_KEY_ENV
    elif plan.vault_mode == "keyring":
        main_cli = _main_cli()
        if not main_cli._system_keyring_available():
            raise RuntimeError("System keyring is not available.")
        vault = VaultStore.open(db_path=vault_path, use_keyring=True)
        label = "system keychain"
    else:
        raise RuntimeError(f"Unsupported vault mode: {plan.vault_mode}")

    status = "ready" if existed else "initialized"
    return vault, f"Vault {status} at {vault._db_path} using {label}"


def _import_env_credentials(vault, env_var_names: list[str]) -> list[str]:
    imported: list[str] = []
    for env_var in env_var_names:
        value = os.environ.get(env_var)
        if not value:
            continue
        vault.set(
            env_var,
            value,
            description=f"Imported from environment variable {env_var}",
            category="imported-env",
        )
        imported.append(env_var)
    return imported


def _store_provider_credential_for_plan(provider: str, api_key: str, *, vault=None) -> str:
    providers = _provider_info()
    main_cli = _main_cli()
    info = providers.get(provider)
    env_var = info.env_var if info else None
    if info is None or not env_var:
        return "No credential required for this provider."

    os.environ[env_var] = api_key
    if vault is not None:
        vault.set(
            env_var,
            api_key,
            description=f"{info.name} API key",
            category="model-provider",
        )
        return f"Stored securely in the Olano Vault at {vault._db_path}"

    env_path = main_cli._lionclaw_env_path()
    main_cli._upsert_env_var(env_path, env_var, api_key)
    return f"Stored in env config at {env_path}"


def run_doctor_if_requested(plan: SetupPlan, outcome: SetupOutcome) -> bool:
    if not plan.run_doctor:
        return True

    main_cli = _main_cli()
    main_cli.console.print(
        f"[bold {main_cli._SETUP_TITLE}]Running doctor for {outcome.agent_id}…[/bold {main_cli._SETUP_TITLE}]"
    )
    rows, hard_fail = main_cli._run_doctor_checks(
        outcome.agent_id,
        outcome.verification_model,
        outcome.verification_skipped,
    )
    main_cli._render_doctor_report(rows, hard_fail)
    main_cli.console.print()
    if hard_fail:
        main_cli.console.print(
            f"[yellow]⚠[/yellow]  Doctor reported a blocking issue for [bold]{outcome.agent_id}[/bold]."
        )
        return False
    return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m olano.cli.setup_textual",
        description="Prototype Textual-based Olano setup flow.",
    )
    parser.add_argument("--home", default=None, help="Olano home directory")
    parser.add_argument("--provider", default=None, help="Initial model provider")
    parser.add_argument("--model", default=None, help="Initial model string")
    parser.add_argument("--api-key", default=None, help="Initial provider API key")
    parser.add_argument("--safety-preset", default=None, help="Initial safety preset")
    parser.add_argument("--mode", default=None, help="Initial agent mode")
    parser.add_argument("--archetype", default=None, help="Initial specialist archetype")
    parser.add_argument("--agent-name", default=None, help="Initial agent name")
    parser.add_argument("--emoji", default=None, help="Initial emoji")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    active_home = set_olano_home(args.home or get_olano_home())
    ensure_olano_home(active_home)

    main_cli = _main_cli()
    main_cli._load_env(active_home)
    main_cli._configure_home_logging(active_home)

    plan = run_setup_textual_prototype(args)
    if plan is None:
        return 0

    try:
        outcome = apply_setup_plan(plan)
    except Exception as exc:
        main_cli.console.print(f"[red]Setup prototype failed:[/red] {exc}")
        return 1

    if run_doctor_if_requested(plan, outcome):
        command_prefix = main_cli._setup_command_prefix(outcome.home)
        main_cli.console.print(
            f"Chat with [bold]{outcome.agent_id}[/bold] in the dashboard: "
            f"[bold]{command_prefix} dashboard[/bold]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
