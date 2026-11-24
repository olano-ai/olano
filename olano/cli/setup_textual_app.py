"""Clean Textual wizard UI for the Olano setup prototype."""

from __future__ import annotations

import os
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from olano.cli import setup_textual as controller

# A choice entry pairs a display label (plain text or a Rich ``Text`` block)
# with the value/id it maps to.
ChoiceEntry = tuple["str | Text", str]


def _choices(entries: list[ChoiceEntry]) -> list[Option]:
    return [Option(label, id=value) for label, value in entries]


def _choice_block(title: str, description: str = "") -> Text:
    block = Text()
    block.append(title, style="bold #111111")
    if description:
        block.append("\n   ")
        block.append(description, style="#6b7280")
    block.append("\n")
    return block


def _safety_entries() -> list[ChoiceEntry]:
    main_cli = controller._main_cli()
    return [
        (
            _choice_block(option["label"], f"{option['summary']}. {option['detail']}"),
            option["key"],
        )
        for option in main_cli._SETUP_SAFETY_OPTIONS
    ]


def _agent_mode_entries() -> list[ChoiceEntry]:
    main_cli = controller._main_cli()
    return [
        (
            _choice_block(option["label"], f"{option['summary']}. {option['detail']}"),
            option["key"],
        )
        for option in main_cli._SETUP_AGENT_OPTIONS
    ]


def _specialist_entries(archetypes: list[str]) -> list[ChoiceEntry]:
    specialists = controller._ordered_specialists(archetypes)
    return [
        (
            _choice_block(
                name,
                controller._specialist_help_text(name) or "Built-in specialist archetype.",
            ),
            name,
        )
        for name in specialists
    ] or [(_choice_block("lionDev", "Built-in specialist archetype."), "lionDev")]


def _provider_entries() -> list[ChoiceEntry]:
    providers = controller._provider_info()
    entries: list[ChoiceEntry] = []
    for label, key in controller._provider_select_options():
        info = providers[key]
        if key == "ollama":
            description = "A smooth default if you want to start quickly. It works with local Ollama models and cloud-tagged ones like Kimi Cloud, and it does not need an API key."
        elif info.env_var:
            description = (
                f"Use this if you already have {info.env_var} set up, or if this is the provider you want to connect now. "
                f"{info.notes or 'This is a hosted model provider.'}"
            ).strip()
        else:
            description = info.notes or "This provider is ready without an API key."
        entries.append((_choice_block(label, description), key))
    return entries


def _vault_mode_entries() -> list[ChoiceEntry]:
    descriptions = {
        "keyring": "Let your computer unlock the vault for you with the system keychain on this machine.",
        "env": "Use OLANO_VAULT_KEY from your current shell environment.",
        "passphrase": "Unlock it yourself with a passphrase whenever Olano needs access.",
    }
    return [
        (_choice_block(label, descriptions.get(value, "")), value)
        for label, value in controller._available_vault_modes()
    ]


def _model_entries(options: list[tuple[str, str]], provider: str) -> list[ChoiceEntry]:
    entries: list[ChoiceEntry] = []
    for label, value in options:
        if value == "__custom__":
            entries.append(
                (
                    _choice_block(
                        "Custom model",
                        "Type the exact model name yourself if you already know what you want to use.",
                    ),
                    value,
                )
            )
            continue
        description = (
            "A strong default for getting started with Ollama."
            if provider == "ollama" and value == "kimi-k2.5:cloud"
            else f"Olano will use {provider}:{value} for your first agent."
        )
        entries.append((_choice_block(label, description), value))
    return entries


class SetupPromptArea(TextArea):
    class Submitted(Message):
        def __init__(self, text_area: SetupPromptArea) -> None:
            self.text_area = text_area
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        self._restart_blink()

        if self.read_only:
            return

        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return

        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return

        await super()._on_key(event)


class OlanoSetupPrototype(App[controller.SetupPlan | None]):
    CSS = """
    Screen {
        background: #f8f8f8;
        color: #111827;
    }

    #shell {
        align: center middle;
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }

    #content {
        width: 1fr;
        max-width: 96;
        height: 1fr;
        padding: 0 1;
    }

    #header-line {
        color: #6b7280;
        text-style: bold;
        margin: 0 0 2 0;
    }

    #body-scroll {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        margin: 0;
    }

    #status {
        color: #b42318;
        min-height: 1;
        margin: 0 0 1 0;
    }

    #start-hint {
        color: #2563eb;
        text-style: bold;
        min-height: 1;
        margin: 1 0;
    }

    .page {
        width: 1fr;
        height: auto;
        align-horizontal: left;
    }

    #page-splash {
        height: 1fr;
        align: center middle;
    }

    .page-body {
        width: 1fr;
        max-width: 1fr;
        height: auto;
    }

    .field-label {
        color: #0f172a;
        text-style: bold;
        margin: 1 0 1 0;
    }

    .field-help {
        color: #6b7280;
        margin: 0 0 2 0;
    }

    Input, TextArea, OptionList {
        margin: 0 0 1 0;
        border: none;
        background: transparent;
        color: #111827;
        width: 1fr;
    }

    Input {
        height: 1;
        min-height: 1;
        padding: 0 2;
        background: #ffffff;
    }

    OptionList {
        height: auto;
        max-height: 12;
        padding: 0;
    }

    OptionList > .option-list--option {
        padding: 0 0 3 0;
        color: #475569;
    }

    OptionList > .option-list--option-highlighted {
        background: transparent;
        color: #93c5fd;
        text-style: bold;
    }

    OptionList > .option-list--option-hover {
        background: transparent;
    }

    TextArea {
        height: 6;
        border: solid #d7dde5;
        background: #ffffff;
    }

    Input:focus {
        background: #eef7ff;
        color: #0f172a;
    }

    TextArea:focus {
        border: solid #bfdbfe;
        background: #f8fbff;
    }

    .hero-kicker {
        display: none;
    }

    .hero-title {
        color: #0f172a;
        text-style: bold;
        text-align: center;
        margin: 0 0 1 0;
    }

    .hero-subtitle {
        color: #475569;
        margin: 0 0 2 0;
    }

    .hero-instruction {
        color: #2563eb;
        text-style: bold;
        text-align: center;
        margin: 0;
    }

    .hero-point {
        color: #475569;
        margin: 0 0 1 0;
    }

    .key-hints {
        color: #64748b;
        min-height: 1;
        margin: 1 0 1 0;
    }

    #review-summary {
        color: #111111;
        margin: 1 0 1 0;
    }

    .compact #shell {
        padding: 0 1;
    }

    .compact OptionList {
        max-height: 8;
    }

    .compact OptionList > .option-list--option {
        padding: 0 0 2 0;
    }

    .compact TextArea {
        height: 4;
    }
    """

    BINDINGS = [
        ("ctrl+c", "cancel", "Cancel"),
        ("shift+tab", "back", "Back"),
        ("tab", "next", "Next"),
        ("left", "back", "Back"),
        ("right", "next", "Next"),
        ("f5", "submit", "Run Setup"),
    ]

    def __init__(self, initial_values: dict[str, Any]) -> None:
        super().__init__()
        self.initial_values = initial_values
        self.values = dict(initial_values)
        self.page_index = 0
        self._start_hint_emphasis = True
        self._option_blink_emphasis = True
        self._choice_entries: dict[str, list[ChoiceEntry]] = {}
        self._guided_values_snapshot: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            with Vertical(id="content"):
                yield Static("", id="header-line")
                yield Static("", id="status")
                yield Static("", id="start-hint")
                with VerticalScroll(id="body-scroll"):
                    with Vertical(id="page-splash", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Welcome to Olano", classes="hero-title")
                            yield Static("Press Enter to continue", classes="hero-instruction")

                    with Vertical(id="page-secrets_strategy", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("How should Olano handle secrets?", classes="field-label")
                            yield Static(
                                "This is how Olano will keep passwords and API keys safe while it works for you.",
                                classes="field-help",
                            )
                            yield OptionList(id="secrets-options")

                    with Vertical(id="page-vault_mode", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("How should the vault unlock?", classes="field-label")
                            yield Static(
                                "Choose the way you want to unlock your local encrypted vault on this machine.",
                                classes="field-help",
                            )
                            yield OptionList(id="vault-mode-options")

                    with Vertical(id="page-vault_passphrase", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Create a vault passphrase", classes="field-label")
                            yield Static(
                                "Olano's vault is a private encrypted place on your machine where it can keep API keys and other secrets, so you do not have to paste them every time. This passphrase is the key that unlocks that vault when Olano needs to use or save a secret.",
                                classes="field-help",
                            )
                            yield Input(
                                value=self.values["vault_passphrase"],
                                password=True,
                                id="vault-passphrase",
                                placeholder="Vault passphrase",
                            )
                            yield Input(
                                password=True,
                                id="vault-passphrase-confirm",
                                placeholder="Confirm vault passphrase",
                            )

                    with Vertical(id="page-quick_start", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("How do you want to continue?", classes="field-label")
                            yield Static(
                                "You can let Olano create a ready-to-go default agent for you, or you can keep going and choose each setting yourself.",
                                classes="field-help",
                            )
                            yield OptionList(id="quick-start-options")

                    with Vertical(id="page-provider", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Which model provider do you want?", classes="field-label")
                            yield Static(
                                "This is the model service your first agent will use to think and respond.",
                                classes="field-help",
                            )
                            yield OptionList(id="provider-options")

                    with Vertical(id="page-import_env", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Import an existing credential?", classes="field-label")
                            yield Static(
                                "",
                                classes="field-help",
                                id="import-env-help",
                            )
                            yield OptionList(id="import-env-options")

                    with Vertical(id="page-model", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Which model should Olano use?", classes="field-label")
                            yield Static(
                                "You can pick one of the suggested models here, or choose your own if you already know what you want.",
                                classes="field-help",
                            )
                            yield OptionList(id="model-options")

                    with Vertical(id="page-custom_model", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Enter a custom model", classes="field-label")
                            yield Static(
                                "If you already know the exact model name, type it here. Olano will save it in the right format for you.",
                                classes="field-help",
                            )
                            yield Input(id="custom-model", placeholder="Custom model")

                    with Vertical(id="page-api_key", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Provider API key", classes="field-label")
                            yield Static("", classes="field-help", id="api-key-help")
                            yield Input(
                                value=self.values["api_key"],
                                password=True,
                                id="api-key",
                                placeholder="API key",
                            )

                    with Vertical(id="page-safety", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Choose agent permissions", classes="field-label")
                            yield Static(
                                "This decides how much access your first agent gets to local files and shell commands. You can start carefully and relax it later if you want.",
                                classes="field-help",
                            )
                            yield OptionList(id="safety-options")

                    with Vertical(id="page-agent_mode", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("What kind of agent do you want?", classes="field-label")
                            yield Static(
                                "Choose whether you want a general helper, a built-in specialist, or a custom agent you define yourself.",
                                classes="field-help",
                            )
                            yield OptionList(id="agent-mode-options")

                    with Vertical(id="page-specialist", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Choose a built-in specialist", classes="field-label")
                            yield Static(
                                "Pick the kind of specialist you want Olano to start as.",
                                classes="field-help",
                            )
                            yield OptionList(id="specialist-options")

                    with Vertical(id="page-agent_name", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Name your agent", classes="field-label")
                            yield Static(
                                "This is the name you will see in chat and around the workspace.",
                                classes="field-help",
                            )
                            yield Input(
                                value=self.values["agent_name"],
                                id="agent-name",
                                placeholder="Agent name",
                            )

                    with Vertical(id="page-emoji", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Choose an emoji", classes="field-label")
                            yield Static(
                                "Pick a small visual marker for your agent. You can change it later.",
                                classes="field-help",
                            )
                            yield Input(value=self.values["emoji"], id="emoji", placeholder="Emoji")

                    with Vertical(id="page-custom_description", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Describe the custom agent", classes="field-label")
                            yield Static(
                                "Write a short plain-English description of what this custom agent is for.",
                                classes="field-help",
                            )
                            yield Input(
                                value=self.values["custom_description"],
                                id="custom-description",
                                placeholder="Custom description",
                            )

                    with Vertical(id="page-custom_prompt", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Write the custom system prompt", classes="field-label")
                            yield Static(
                                "Give your custom agent its starting instructions. Keep it simple for now if you want; you can refine it later.",
                                classes="field-help",
                            )
                            yield SetupPromptArea(
                                self.values["custom_system_prompt"], id="custom-system-prompt"
                            )

                    with Vertical(id="page-review", classes="page"):
                        with Vertical(classes="page-body"):
                            yield Static("Review setup", classes="field-label")
                            yield Static(
                                "Take one last look. When you continue, Olano will install the agent, verify the model and run a quick health check.",
                                classes="field-help",
                            )
                            yield Static("", id="review-summary")
                yield Static("", id="key-hints", classes="key-hints")

    def on_mount(self) -> None:
        self.title = "Olano Setup"
        self.sub_title = "Setup wizard"
        self._update_layout_mode()
        self.set_interval(0.7, self._tick_emphasis)
        self._set_choice_list(
            "#secrets-options",
            [
                (
                    _choice_block(
                        "Olano Vault",
                        "Best for most people. Olano keeps your secrets in an encrypted local vault and can reuse them next time.",
                    ),
                    "vault",
                ),
                (
                    _choice_block(
                        "Environment variables",
                        "Use credentials that are already set in your current shell. This is handy if you already manage them yourself.",
                    ),
                    "env",
                ),
                (
                    _choice_block(
                        "Configure later",
                        "Finish setup now and add credentials later. This is fine if you just want to get oriented first.",
                    ),
                    "later",
                ),
            ],
            str(self.values["secrets_strategy"]),
        )
        self._set_choice_list(
            "#vault-mode-options",
            _vault_mode_entries(),
            str(self.values["vault_mode"]),
        )
        self._set_choice_list(
            "#quick-start-options",
            [
                (
                    _choice_block(
                        "Super quick start",
                        "Start with a blank agent right away, plus a small built-in team for web search, browser tasks, and Gmail. It is set up around Ollama, so you can use free open-source models as you grow from there.",
                    ),
                    "quick",
                ),
                (
                    _choice_block(
                        "Customize setup",
                        "Keep going step by step so you can choose the model, permissions, and agent shape yourself.",
                    ),
                    "guided",
                ),
            ],
            "quick" if self.values.get("quick_start") else "guided",
        )
        self._set_choice_list(
            "#provider-options", _provider_entries(), str(self.values["provider"])
        )
        self._set_choice_list(
            "#safety-options", _safety_entries(), str(self.values["safety_preset"])
        )
        self._set_choice_list(
            "#agent-mode-options", _agent_mode_entries(), str(self.values["mode"])
        )
        self._set_choice_list(
            "#specialist-options",
            _specialist_entries(self.initial_values["archetypes"]),
            str(self.values["archetype"]),
        )
        self._sync_provider_dependent_state()
        self._render_current_page()
        self._refresh_review()

    def on_resize(self, _: events.Resize) -> None:
        self._update_layout_mode()

    def on_key(self, event: events.Key) -> None:
        current = self._page_sequence()[self.page_index]
        if current == "splash" and event.key == "enter":
            event.stop()
            self.action_next()
            return
        if current == "review" and event.key == "enter":
            event.stop()
            self.action_submit()
            return

    def on_input_changed(self, _: Input.Changed) -> None:
        self._sync_text_values()
        self._clear_status()
        self._refresh_review()

    def on_text_area_changed(self, _: TextArea.Changed) -> None:
        self.values["custom_system_prompt"] = self.query_one("#custom-system-prompt", TextArea).text
        self._clear_status()
        self._refresh_review()

    def on_setup_prompt_area_submitted(self, _: SetupPromptArea.Submitted) -> None:
        self._sync_text_values()
        self.action_next()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self._sync_text_values()
        self.action_next()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        selector = f"#{event.option_list.id}"
        self._refresh_choice_list(selector)
        self._render_key_hints(self._page_sequence()[self.page_index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        list_id = event.option_list.id or ""
        if list_id == "secrets-options":
            self.values["secrets_strategy"] = option_id
            if option_id != "vault":
                self.values["import_env_credentials"] = False
                self.values["vault_passphrase"] = ""
            self.action_next()
        elif list_id == "vault-mode-options":
            self.values["vault_mode"] = option_id
            if option_id != "passphrase":
                self.values["vault_passphrase"] = ""
                self.query_one("#vault-passphrase", Input).value = ""
                self.query_one("#vault-passphrase-confirm", Input).value = ""
            self.action_next()
        elif list_id == "quick-start-options":
            if option_id == "quick":
                self._guided_values_snapshot = dict(self.values)
                self.values.update(controller._apply_quick_start_defaults(self.values))
                self.values["quick_start"] = True
                self._sync_provider_dependent_state()
            else:
                if self._guided_values_snapshot is not None:
                    self.values.update(self._guided_values_snapshot)
                self.values["quick_start"] = False
                self._sync_provider_dependent_state()
            self.action_next()
        elif list_id == "provider-options":
            self.values["provider"] = option_id
            self.values["model"] = controller._default_model_for_setup(option_id)
            self.values["api_key"] = ""
            self._sync_provider_dependent_state()
            self.action_next()
        elif list_id == "import-env-options":
            self.values["import_env_credentials"] = option_id == "yes"
            self.action_next()
        elif list_id == "model-options":
            self.values["model_choice"] = option_id
            if option_id != "__custom__":
                self.values["model"] = controller._normalize_model_from_choice(
                    str(self.values["provider"]),
                    option_id,
                    str(self.values.get("custom_model", "")),
                )
            self.action_next()
        elif list_id == "safety-options":
            self.values["safety_preset"] = option_id
            self.action_next()
        elif list_id == "agent-mode-options":
            self.values["mode"] = option_id
            self._sync_agent_defaults()
            self.action_next()
        elif list_id == "specialist-options":
            self.values["archetype"] = option_id
            self._sync_agent_defaults()
            self.action_next()
        self._clear_status()
        self._refresh_review()

    async def action_back(self) -> None:
        self._clear_status()
        if self.page_index > 0:
            self.page_index -= 1
            self._render_current_page()

    def action_cancel(self) -> None:
        self.exit(None)

    def action_next(self) -> None:
        errors = self._validate_current_page()
        if errors:
            self._show_errors(errors)
            return
        current = self._page_sequence()[self.page_index]
        if current == "quick_start" and self.values.get("quick_start"):
            self.action_submit()
            return
        if self.page_index < len(self._page_sequence()) - 1:
            self.page_index += 1
            self._render_current_page()

    def action_submit(self) -> None:
        self._sync_text_values()
        errors = self._validate_all_pages()
        if errors:
            first_invalid = self._first_invalid_page_index()
            if first_invalid is not None:
                self.page_index = first_invalid
                self._render_current_page()
            self._show_errors(errors[:3])
            return
        self.exit(controller._make_plan(self._collect_values()))

    def _page_sequence(self) -> list[str]:
        pages = ["splash", "secrets_strategy"]
        if self.values["secrets_strategy"] == "vault":
            if self._should_show_vault_mode_page():
                pages.append("vault_mode")
            if self.values["vault_mode"] == "passphrase":
                pages.append("vault_passphrase")
            pages.append("quick_start")
            if self.values.get("quick_start"):
                return pages
        if self.values.get("quick_start") and self.values["secrets_strategy"] != "vault":
            self.values["quick_start"] = False
        pages.append("provider")
        if self.values["secrets_strategy"] == "vault" and self._provider_env_var_detected():
            pages.append("import_env")
        pages.append("model")
        if self.values.get("model_choice") == "__custom__":
            pages.append("custom_model")
        if self._should_show_api_key_page():
            pages.append("api_key")
        pages.extend(["safety", "agent_mode"])
        if self.values["mode"] == "specialized":
            pages.append("specialist")
        pages.extend(["agent_name", "emoji"])
        if self.values["mode"] == "custom":
            pages.extend(["custom_description", "custom_prompt"])
        pages.append("review")
        return pages

    def _page_titles(self) -> dict[str, tuple[str, str]]:
        return {
            "splash": ("Welcome", "Let's get Olano ready."),
            "secrets_strategy": (
                "Secrets",
                "Choose how Olano should keep your credentials safe.",
            ),
            "vault_mode": ("Secrets", "Choose how you want to unlock your vault."),
            "vault_passphrase": ("Secrets", "Create a passphrase that Olano can use later."),
            "quick_start": ("Quick Start", "Choose the fast path or keep customizing."),
            "provider": ("Model", "Choose which model provider your first agent will use."),
            "import_env": ("Secrets", "Bring in a credential you already have."),
            "model": ("Model", "Choose the model your first agent should start with."),
            "custom_model": ("Model", "Type the exact model you want to use."),
            "api_key": ("Model", "Add a provider key if this model needs one."),
            "safety": ("Permissions", "Choose how much local access to give the agent."),
            "agent_mode": ("Agent", "Choose what kind of agent you want to start with."),
            "specialist": ("Agent", "Pick the built-in specialist that fits best."),
            "agent_name": ("Agent", "Choose the name you want to see in chat."),
            "emoji": ("Agent", "Pick a small visual marker for the agent."),
            "custom_description": ("Agent", "Describe what your custom agent should do."),
            "custom_prompt": ("Agent", "Give your custom agent its starting instructions."),
            "review": ("Review", "Check everything before Olano installs your agent."),
        }

    def _render_current_page(self) -> None:
        pages = self._page_sequence()
        if self.page_index >= len(pages):
            self.page_index = len(pages) - 1
        current = pages[self.page_index]
        all_pages = [
            "splash",
            "secrets_strategy",
            "vault_mode",
            "vault_passphrase",
            "quick_start",
            "provider",
            "import_env",
            "model",
            "custom_model",
            "api_key",
            "safety",
            "agent_mode",
            "specialist",
            "agent_name",
            "emoji",
            "custom_description",
            "custom_prompt",
            "review",
        ]
        for page_id in all_pages:
            page = self.query_one_optional(f"#page-{page_id}", Vertical)
            if page is not None:
                page.display = page_id == current
        title, _subtitle = self._page_titles()[current]
        header = self.query_one("#header-line", Static)
        is_splash = current == "splash"
        header.display = not is_splash
        if not is_splash:
            header.update(f"LIONCLAW  {self.page_index + 1}/{len(pages)}  {title}")
        start_hint = self.query_one("#start-hint", Static)
        start_hint.display = False
        start_hint.update("")
        self._focus_current_page(current)
        self._render_key_hints(current)
        self._refresh_review()

    def _focus_current_page(self, current: str) -> None:
        focus_map = {
            "secrets_strategy": "#secrets-options",
            "vault_mode": "#vault-mode-options",
            "vault_passphrase": "#vault-passphrase",
            "quick_start": "#quick-start-options",
            "provider": "#provider-options",
            "import_env": "#import-env-options",
            "model": "#model-options",
            "custom_model": "#custom-model",
            "api_key": "#api-key",
            "safety": "#safety-options",
            "agent_mode": "#agent-mode-options",
            "specialist": "#specialist-options",
            "agent_name": "#agent-name",
            "emoji": "#emoji",
            "custom_description": "#custom-description",
            "custom_prompt": "#custom-system-prompt",
        }
        if current in focus_map:
            self.set_focus(self.query_one(focus_map[current]))
            if current in {
                "secrets_strategy",
                "vault_mode",
                "provider",
                "import_env",
                "model",
                "safety",
                "agent_mode",
                "specialist",
            }:
                self._refresh_choice_list(focus_map[current])
        card_body = self.query_one("#body-scroll", VerticalScroll)
        try:
            card_body.scroll_home(animate=False)
        except Exception:
            pass

    def _render_start_hint(self) -> None:
        hint = self.query_one("#start-hint", Static)
        if not hint.display:
            hint.update("")
            return
        if self._start_hint_emphasis:
            hint.update("[#2563eb]Press Enter to begin setup[/#2563eb]")
        else:
            hint.update("[#60a5fa]Press Enter to begin setup[/#60a5fa]")

    def _tick_emphasis(self) -> None:
        current = self._page_sequence()[self.page_index]
        selector = self._page_option_selector(current)
        if selector:
            self._option_blink_emphasis = not self._option_blink_emphasis
            self._refresh_choice_list(selector)

    @staticmethod
    def _is_compact_viewport(width: int, height: int) -> bool:
        return width < 110 or height < 30

    def _update_layout_mode(self) -> None:
        compact = self._is_compact_viewport(self.size.width, self.size.height)
        self.set_class(compact, "compact")

    def _should_show_vault_mode_page(self) -> bool:
        return len(self.initial_values.get("vault_modes", [])) > 1

    def _render_key_hints(self, current: str) -> None:
        hints = self.query_one_optional("#key-hints", Static)
        if hints is None:
            return
        if current == "splash":
            hints.update("")
            return
        if current == "review":
            hints.update("Left: back   Enter: run setup   F5: run setup   Ctrl+C: cancel")
            return
        focused = self.focused
        if isinstance(focused, OptionList):
            hints.update(
                "Up/Down: move   Enter: choose   Left: back   Right: next   Ctrl+C: cancel"
            )
            return
        if isinstance(focused, SetupPromptArea):
            hints.update(
                "Type to edit   Shift+Enter: newline   Enter: continue   Left: back   Right: next   Ctrl+C: cancel"
            )
            return
        if isinstance(focused, Input | TextArea):
            hints.update(
                "Type to edit   Enter: continue   Left: back   Right: next   Ctrl+C: cancel"
            )
            return
        hints.update("Left: back   Right: next   Ctrl+C: cancel")

    def _collect_values(self) -> dict[str, Any]:
        self._sync_text_values()
        values = dict(self.values)
        values["model"] = controller._normalize_model_from_choice(
            str(values["provider"]),
            str(values.get("model_choice", "__custom__")),
            str(values.get("custom_model", "")),
        )
        return values

    def _sync_text_values(self) -> None:
        vault_passphrase = self.query_one_optional("#vault-passphrase", Input)
        custom_model = self.query_one_optional("#custom-model", Input)
        api_key = self.query_one_optional("#api-key", Input)
        agent_name = self.query_one_optional("#agent-name", Input)
        emoji = self.query_one_optional("#emoji", Input)
        custom_description = self.query_one_optional("#custom-description", Input)
        custom_system_prompt = self.query_one_optional("#custom-system-prompt", TextArea)
        if vault_passphrase is not None:
            self.values["vault_passphrase"] = vault_passphrase.value
        if custom_model is not None:
            self.values["custom_model"] = custom_model.value
        if api_key is not None:
            self.values["api_key"] = api_key.value
        if agent_name is not None:
            self.values["agent_name"] = agent_name.value
        if emoji is not None:
            self.values["emoji"] = emoji.value
        if custom_description is not None:
            self.values["custom_description"] = custom_description.value
        if custom_system_prompt is not None:
            self.values["custom_system_prompt"] = custom_system_prompt.text

    def _clear_status(self) -> None:
        status = self.query_one_optional("#status", Static)
        if status is not None:
            status.update("")

    def _show_errors(self, errors: list[str]) -> None:
        status = self.query_one_optional("#status", Static)
        if status is not None:
            status.update("\n".join(errors))

    def _validate_current_page(self) -> list[str]:
        return self._validate_page(self._page_sequence()[self.page_index])

    def _validate_all_pages(self) -> list[str]:
        errors: list[str] = []
        for page in self._page_sequence():
            errors.extend(self._validate_page(page))
        return errors

    def _first_invalid_page_index(self) -> int | None:
        for index, page in enumerate(self._page_sequence()):
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
        if page == "custom_model" and not self.query_one("#custom-model", Input).value.strip():
            return ["Custom model is required."]
        if page == "agent_name" and not str(values["agent_name"]).strip():
            return ["Agent name is required."]
        if page == "emoji" and not str(values["emoji"]).strip():
            return ["Emoji is required."]
        return []

    def _provider_env_var_detected(self) -> bool:
        env_var = controller._provider_env_var(str(self.values["provider"]))
        return bool(env_var and os.environ.get(env_var))

    def _should_show_api_key_page(self) -> bool:
        if self.values["secrets_strategy"] != "vault":
            return False
        env_var = controller._provider_env_var(str(self.values["provider"]))
        if env_var is None:
            return False
        return not (self.values["import_env_credentials"] and self._provider_env_var_detected())

    def _set_choice_list(
        self,
        selector: str,
        entries: list[ChoiceEntry],
        selected_id: str | None = None,
    ) -> None:
        widget = self.query_one(selector, OptionList)
        self._choice_entries[selector] = entries
        widget.set_options(_choices(entries))
        if not entries:
            widget.highlighted = None
            return
        values = [value for _, value in entries]
        widget.highlighted = values.index(selected_id) if selected_id in values else 0
        self._refresh_choice_list(selector)

    def _page_option_selector(self, current: str) -> str | None:
        return {
            "secrets_strategy": "#secrets-options",
            "vault_mode": "#vault-mode-options",
            "quick_start": "#quick-start-options",
            "provider": "#provider-options",
            "import_env": "#import-env-options",
            "model": "#model-options",
            "safety": "#safety-options",
            "agent_mode": "#agent-mode-options",
            "specialist": "#specialist-options",
        }.get(current)

    def _refresh_choice_list(self, selector: str) -> None:
        widget = self.query_one_optional(selector, OptionList)
        entries = self._choice_entries.get(selector)
        if widget is None or not entries:
            return
        highlighted = widget.highlighted if widget.highlighted is not None else 0
        for index, (prompt, _value) in enumerate(entries):
            prompt_text = prompt if isinstance(prompt, Text) else Text(prompt)
            widget.replace_option_prompt_at_index(
                index,
                self._render_choice_prompt(index, prompt_text, index == highlighted),
            )

    def _render_choice_prompt(self, index: int, prompt: Text, active: bool) -> Text:
        render = Text()
        if active:
            accent = "#93c5fd" if self._option_blink_emphasis else "#7dd3fc"
            prefix = f"> {index + 1}. "
            render.append(prefix, style=f"bold {accent}")
            prompt_copy = prompt.copy()
            first_line_end = prompt_copy.plain.find("\n")
            if first_line_end == -1:
                first_line_end = len(prompt_copy.plain)
            if first_line_end > 0:
                prompt_copy.stylize(accent, 0, first_line_end)
            render.append_text(prompt_copy)
        else:
            render.append(f"  {index + 1}. ", style="#94a3b8")
            render.append_text(prompt.copy())
        return render

    def _sync_provider_dependent_state(self) -> None:
        provider = str(self.values["provider"])
        provider_name = next(
            label for label, value in controller._provider_select_options() if value == provider
        )
        env_var = controller._provider_env_var(provider)
        self.query_one("#api-key-help", Static).update(
            f"If you already have {env_var}, you can leave this blank. Otherwise, paste it here and Olano will store it safely for you."
            if env_var
            else f"{provider_name} does not need an API key, so you can just keep going."
        )
        options, selected, custom_value = controller._model_picker_state(
            provider, str(self.values["model"])
        )
        self.values["model_choice"] = selected
        self.values["custom_model"] = custom_value
        self._set_choice_list("#model-options", _model_entries(options, provider), selected)
        self.query_one("#custom-model", Input).value = custom_value
        env_var = controller._provider_env_var(provider)
        if env_var and os.environ.get(env_var):
            self.query_one("#import-env-help", Static).update(
                "Olano found an existing credential in your environment. You can copy it into the local vault now so future runs feel smoother."
            )
            self._set_choice_list(
                "#import-env-options",
                [
                    (
                        _choice_block(
                            "Copy it into the vault",
                            f"Save {env_var} into Olano's local vault so it is available next time too.",
                        ),
                        "yes",
                    ),
                    (
                        _choice_block(
                            "Leave it where it is",
                            f"Keep using {env_var} from your shell without copying it into the vault.",
                        ),
                        "no",
                    ),
                ],
                "yes" if self.values["import_env_credentials"] else "no",
            )
        else:
            self.values["import_env_credentials"] = False
            self.query_one("#import-env-help", Static).update(
                "Olano did not find an existing credential to import here, so we will just keep going."
            )
            self._set_choice_list(
                "#import-env-options",
                [
                    (
                        _choice_block(
                            "Continue", "There is nothing to import on this machine right now."
                        ),
                        "no",
                    )
                ],
                "no",
            )

    def _sync_agent_defaults(self) -> None:
        mode = str(self.values["mode"])
        archetype = str(self.values["archetype"])
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
            agent_name.value = controller._default_agent_name(mode, archetype)
            self.values["agent_name"] = agent_name.value
        if not emoji.value.strip() or emoji.value in {"🦁", "🤖"}:
            emoji.value = controller._default_emoji(mode, archetype)
            self.values["emoji"] = emoji.value

    def _refresh_review(self) -> None:
        review = self.query_one_optional("#review-summary", Static)
        if review is not None:
            review.update(controller._render_summary(self._collect_values()))
