"""The "This is a Customer Service Agent" switch.

Converting an existing agent to customer service used to be impossible from the
dashboard. The only thing that gated the customer tools was
``memory.per_customer``, which had no UI control at all, while the unrelated
``human_only`` flag — which closes the agent's ``talk_to_agent`` inbox and does
nothing to channels or tools — sat in Access & Safety looking like the switch.
Operators ticked it and got no ``remember``, no ``forget_me``.

``customer_service.enabled`` is that switch. It drives ``memory.per_customer``
so the tools arrive through the existing builder path, and appends the
operator's ``prompt_extension`` to the system prompt — filling a blank one with
the support-desk default, so a shared agent never runs with no privacy or scope
rules, while text the operator wrote is kept verbatim.
"""

from __future__ import annotations

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.customer_service import (
    CUSTOMER_SERVICE_TOOLS,
    DEFAULT_MULTI_USER_PROMPT_EXTENSION,
    DEFAULT_PROMPT_EXTENSION_VARIANT,
    SUGGESTED_MULTI_USER_PROMPT_EXTENSION,
    SUGGESTED_PROMPT_EXTENSIONS,
    SUGGESTED_SUPPORT_PROMPT_EXTENSION,
    resolve_prompt_extension,
)
from olano.config.workspace import get_workspace
from olano.core.builder import _build_memory_tools
from olano.core.prompt_assembler import PromptAssembler

CUSTOMER_MEMORY_TOOLS = {"remember", "recall", "recall_all", "forget_me"}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "MEMORY.md").write_text("# shared memory\n")
    (tmp_path / "memories").mkdir(exist_ok=True)
    return tmp_path


def _memory_tool_names(config: AgentConfig, root) -> set[str]:
    cats = _build_memory_tools(config, root, categorized=True)
    return {t.name for t in cats["memory"]}


# ── the reported bug ─────────────────────────────────────────────────────────


def test_human_only_does_not_enable_customer_service(ws):
    """The exact thing the operator tried. It is an A2A flag, not a mode switch."""
    config = AgentConfig(name="acme", workspace=str(ws), human_only=True)

    assert config.customer_service.enabled is False
    assert config.memory.per_customer is False
    assert not (_memory_tool_names(config, ws) & CUSTOMER_MEMORY_TOOLS)


# ── the switch ───────────────────────────────────────────────────────────────


def test_switch_adds_the_customer_tools(ws):
    config = AgentConfig(name="acme", workspace=str(ws), customer_service={"enabled": True})

    names = _memory_tool_names(config, ws)

    assert CUSTOMER_MEMORY_TOOLS <= names
    assert set(CUSTOMER_SERVICE_TOOLS) <= names


def test_switch_isolates_memory_and_drops_the_shared_tools(ws):
    """Per-customer mode replaces shared memory; it must not stack with it."""
    config = AgentConfig(name="acme", workspace=str(ws), customer_service={"enabled": True})

    assert config.memory.per_customer is True
    assert not any(n.startswith("memory_") for n in _memory_tool_names(config, ws))


def test_switch_off_by_default_and_keeps_shared_memory(ws):
    config = AgentConfig(name="acme", workspace=str(ws))

    assert config.customer_service.enabled is False
    names = _memory_tool_names(config, ws)
    assert "memory_save" in names
    assert not (names & CUSTOMER_MEMORY_TOOLS)


def test_existing_per_customer_agents_read_back_as_enabled(ws):
    """The archetype (and pre-switch hand-edited configs) set per_customer
    directly; the toggle must not show those agents as "off"."""
    config = AgentConfig(name="cs", workspace=str(ws), memory={"per_customer": True})

    assert config.customer_service.enabled is True


# ── prompt extension ─────────────────────────────────────────────────────────


def _prompt(config: AgentConfig, root) -> str:
    return PromptAssembler(config, get_workspace(str(root)))._effective_system_prompt()


def test_prompt_extension_is_appended_after_the_system_prompt(ws):
    config = AgentConfig(
        name="acme",
        workspace=str(ws),
        system_prompt="BASE PROMPT",
        customer_service={"enabled": True, "prompt_extension": "SCOPE RULES"},
    )

    prompt = _prompt(config, ws)

    assert "BASE PROMPT" in prompt and "SCOPE RULES" in prompt
    # Last word wins: the scope/privacy rules must qualify what precedes them.
    assert prompt.index("SCOPE RULES") > prompt.index("BASE PROMPT")


def test_blank_extension_defaults_to_the_support_desk_text(ws):
    """A shared agent must never face strangers with no privacy or scope rules."""
    config = AgentConfig(name="acme", workspace=str(ws), customer_service={"enabled": True})

    assert config.customer_service.prompt_extension == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    # The assembler strips the block it appends, hence the strip() on both sides.
    assert DEFAULT_MULTI_USER_PROMPT_EXTENSION.strip() in _prompt(config, ws)


def test_the_default_is_the_support_variant(ws):
    """The blank-field default is the support-desk text, not the privacy-only one."""
    assert DEFAULT_MULTI_USER_PROMPT_EXTENSION == SUGGESTED_SUPPORT_PROMPT_EXTENSION
    assert DEFAULT_MULTI_USER_PROMPT_EXTENSION != SUGGESTED_MULTI_USER_PROMPT_EXTENSION
    assert SUGGESTED_PROMPT_EXTENSIONS[DEFAULT_PROMPT_EXTENSION_VARIANT] == (
        DEFAULT_MULTI_USER_PROMPT_EXTENSION
    )


@pytest.mark.parametrize("blank", ["", "   ", "\n\n  \t"])
def test_whitespace_only_extension_counts_as_blank(ws, blank):
    """Clearing the dashboard box restores the default instead of no rules."""
    config = AgentConfig(
        name="acme",
        workspace=str(ws),
        customer_service={"enabled": True, "prompt_extension": blank},
    )

    assert config.customer_service.prompt_extension == DEFAULT_MULTI_USER_PROMPT_EXTENSION


def test_per_customer_agents_get_the_default_too(ws):
    """The archetype (and hand-edited configs) enable the mode via memory.per_customer."""
    config = AgentConfig(name="acme", workspace=str(ws), memory={"per_customer": True})

    assert config.customer_service.prompt_extension == DEFAULT_MULTI_USER_PROMPT_EXTENSION


def test_no_default_while_the_switch_is_off(ws):
    """A single-user agent's box stays empty — support-desk rules it never asked for."""
    config = AgentConfig(name="acme", workspace=str(ws))

    assert config.customer_service.prompt_extension == ""
    assert DEFAULT_MULTI_USER_PROMPT_EXTENSION not in _prompt(config, ws)


def test_operator_text_is_never_overwritten(ws):
    """Round-tripping the config must leave the operator's wording byte-identical."""
    mine = "Only answer questions about ACME lawn mowers."
    config = AgentConfig(
        name="acme", workspace=str(ws), customer_service={"enabled": True, "prompt_extension": mine}
    )

    reloaded = AgentConfig.model_validate(config.model_dump())

    assert reloaded.customer_service.prompt_extension == mine


def test_extension_ignored_while_the_switch_is_off(ws):
    """Text left behind after disabling must not keep shaping the prompt."""
    config = AgentConfig(
        name="acme",
        workspace=str(ws),
        system_prompt="BASE",
        customer_service={"enabled": False, "prompt_extension": "SHOULD NOT APPLY"},
    )

    assert "SHOULD NOT APPLY" not in _prompt(config, ws)


# ── the prompt the dashboard offers ──────────────────────────────────────────


def test_suggested_prompt_covers_tools_privacy_and_scope_guardrails():
    text = SUGGESTED_SUPPORT_PROMPT_EXTENSION.lower()

    for tool in ("remember", "recall", "forget_me", "knowledge_search"):
        assert tool in text, tool
    # Refuses to be a free general-purpose assistant.
    for guard in ("stocks", "mathematics", "homework", "medical", "roleplay"):
        assert guard in text, guard
    # …without refusing legitimate support questions that merely sound general.
    assert "in scope" in text


def test_resolver_fills_blanks_and_passes_operator_text_through_verbatim():
    assert resolve_prompt_extension(None) == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    assert resolve_prompt_extension("") == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    assert resolve_prompt_extension("  \n ") == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    # Verbatim: leading/trailing whitespace an operator typed is theirs to keep.
    assert resolve_prompt_extension("  Only ACME mowers.\n") == "  Only ACME mowers.\n"


def test_prompt_visible_tool_names_match_what_is_registered(ws):
    """The prompt must not advertise shared-memory tools a CS agent cannot call.

    A converted agent keeps ``memory.markdown = True`` from before the switch,
    and the assembler used to key off that flag independently of the builder's
    per-customer precedence.
    """
    config = AgentConfig(name="acme", workspace=str(ws), customer_service={"enabled": True})
    assert config.memory.markdown is True  # untouched by the switch

    listed = set(PromptAssembler(config, get_workspace(str(ws)))._visible_tool_names())
    registered = _memory_tool_names(config, ws)

    assert CUSTOMER_MEMORY_TOOLS <= listed
    assert not any(n.startswith("memory_") for n in listed)
    assert not (listed & {"memory_save", "memory_search"}) - registered
