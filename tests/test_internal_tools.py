"""The "Olano Internal Tools" catalog — the one declarative description of
every builder/deepagents-provided tool an agent can get without a `tools:`
entry, that now backs the dashboard's transparency section.
"""

from __future__ import annotations

from olano.config.agent_config import AgentConfig, AutonomyConfig, MemoryConfig
from olano.config.internal_tools import INTERNAL_TOOL_CATALOG, effective_internal_tools


def _rows_by_name(config: AgentConfig) -> dict[str, dict[str, object]]:
    return {row["name"]: row for row in effective_internal_tools(config)}


def test_secret_tools_are_not_in_the_catalog():
    """secret_get/secret_list/secret_set are opt-in (agent.json5 `tools`), not
    part of the always-bound internal surface — see core/builder._register_secret_tools."""
    names = {t.name for t in INTERNAL_TOOL_CATALOG}
    assert not ({"secret_get", "secret_list", "secret_set"} & names)


def test_always_on_tool_is_on_by_default(base_config: AgentConfig):
    rows = _rows_by_name(base_config)
    assert rows["whoami"]["on"] is True
    assert rows["notification_send_text"]["on"] is True


def test_disabled_tools_turns_off_a_catalog_entry(base_config: AgentConfig):
    base_config.disabled_tools = ["whoami"]
    rows = _rows_by_name(base_config)
    assert rows["whoami"]["on"] is False
    assert rows["notification_send_text"]["on"] is True


def test_every_entry_is_reported_for_every_agent(base_config: AgentConfig):
    """The whole catalog shows on any agent: rows whose condition doesn't hold
    are off + locked with a "requires <mode>" note instead of invisible, so an
    operator can discover HOW to get a tool (e.g. `remember` needs Customer
    Service mode) without already having the mode on."""
    base_config.memory = MemoryConfig(markdown=True, per_customer=False, rag=False)
    rows = _rows_by_name(base_config)
    assert len(rows) == len(INTERNAL_TOOL_CATALOG)

    # Shared markdown memory applies; customer memory doesn't — but both show.
    assert rows["memory_save"]["on"] is True
    assert rows["remember"]["on"] is False
    assert rows["remember"]["toggleable"] is False
    assert "Customer Service" in str(rows["remember"]["note"])
    # RAG off → rag tools present, off, with the pointer.
    assert rows["rag_search"]["on"] is False
    assert "RAG" in str(rows["rag_search"]["note"])

    base_config.memory = MemoryConfig(markdown=True, per_customer=True, rag=False)
    rows = _rows_by_name(base_config)
    assert rows["remember"]["on"] is True
    # Shared markdown tools flip to the locked-off side in Customer Service mode.
    assert rows["memory_save"]["on"] is False
    assert rows["memory_save"]["toggleable"] is False
    assert "Customer Service off" in str(rows["memory_save"]["note"])


def test_stale_top_level_memory_tools_shows_as_excluded_not_on(base_config: AgentConfig):
    """The reported bug: a top_level_memory_tools pin left over from before
    Customer Service was enabled filters the newly-built per-customer tools
    down to nothing. The catalog must reflect that — not report them as "on"
    when the real builder would actually drop them."""
    base_config.memory = MemoryConfig(markdown=True, per_customer=True, rag=False)
    base_config.top_level_memory_tools = ["user_profile_save", "identity_preference_save"]

    rows = _rows_by_name(base_config)

    assert rows["remember"]["on"] is False
    assert rows["remember"]["toggleable"] is False
    assert "top_level_memory_tools" in str(rows["remember"]["note"])


def test_null_allowlist_means_no_filter(base_config: AgentConfig):
    base_config.memory = MemoryConfig(markdown=True, per_customer=True, rag=False)
    base_config.top_level_memory_tools = None
    rows = _rows_by_name(base_config)
    assert rows["remember"]["on"] is True


def test_filesystem_and_task_are_toggleable_and_honor_disabled_tools(base_config: AgentConfig):
    """The deepagents-attached tools used to be locked ("transparency, not
    control"); core/disabled_tools_middleware now removes a disabled name from
    every model bind, so the catalog reports them as real toggles."""
    rows = _rows_by_name(base_config)
    assert rows["read_file"]["toggleable"] is True
    assert rows["task"]["toggleable"] is True
    assert rows["write_todos"]["toggleable"] is True

    base_config.disabled_tools = ["read_file", "task"]
    rows = _rows_by_name(base_config)
    assert rows["read_file"]["on"] is False
    assert rows["task"]["on"] is False
    assert rows["ls"]["on"] is True


def test_execute_requires_a_shell_backend(base_config: AgentConfig):
    # base_config uses a plain filesystem backend → execute exists but is
    # locked off with the pointer to what enables it.
    rows = _rows_by_name(base_config)
    assert rows["execute"]["on"] is False
    assert rows["execute"]["toggleable"] is False
    assert "shell" in str(rows["execute"]["note"])


def test_every_catalog_entry_has_a_description():
    """The dashboard tool browser renders these as first-class cards now, so a
    blank description is a regression (the card would say "No description
    provided" for a tool we ship). Catches a name added to ops_defaults or a
    category dict without a matching description entry."""
    missing = [t.name for t in INTERNAL_TOOL_CATALOG if not t.description.strip()]
    assert not missing, f"internal tools missing a description: {missing}"


def test_rows_carry_descriptions_even_when_excluded(base_config: AgentConfig):
    base_config.memory = MemoryConfig(markdown=True, per_customer=True, rag=False)
    base_config.top_level_memory_tools = ["user_profile_save"]
    rows = _rows_by_name(base_config)
    # Normal row and allowlist-excluded row both keep the catalog description.
    assert rows["whoami"]["description"]
    assert rows["remember"]["on"] is False
    assert rows["remember"]["description"]


# ── magic links ──────────────────────────────────────────────────────────────


def test_magic_link_tools_are_on_by_default_under_their_own_category(base_config: AgentConfig):
    """A link is the default way an agent connects itself to anything, so the
    family is granted to every agent — visibly, as toggleable rows."""
    rows = _rows_by_name(base_config)
    for name in (
        "magic_link_connect_service",
        "magic_link_request_secret",
        "magic_link_connect_channels",
        "magic_link_status",
    ):
        assert rows[name]["on"] is True, name
        assert rows[name]["category"] == "Magic links"
        assert rows[name]["toggleable"] is True


def test_magic_links_capability_off_locks_the_family_with_a_note(base_config: AgentConfig):
    base_config.autonomy = AutonomyConfig(magic_links=False)
    rows = _rows_by_name(base_config)
    row = rows["magic_link_request_secret"]
    assert row["on"] is False
    assert row["toggleable"] is False
    assert "magic_links" in str(row["note"])
    # An unrelated self_* row keeps its own capability.
    assert rows["self_update_prompt"]["on"] is True


def test_disabled_tools_drops_one_magic_link_tool(base_config: AgentConfig):
    base_config.disabled_tools = ["magic_link_open_dashboard"]
    rows = _rows_by_name(base_config)
    assert rows["magic_link_open_dashboard"]["on"] is False
    assert rows["magic_link_request_secret"]["on"] is True
