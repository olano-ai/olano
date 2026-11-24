"""Discovery tags for tools, and the catalog surfaces that expose them.

The motivating failure: an operator agent asked "what tools let a support bot
save customer information privately?" and could not answer, because
``remember`` / ``recall`` / ``recall_all`` / ``forget_me`` are built per-agent
by the builder and therefore appear in no registry listing at all.
"""

from __future__ import annotations

from pathlib import Path

from olano import admin
from olano.tools import registry

CUSTOMER_MEMORY_TOOLS = {"remember", "recall", "recall_all", "forget_me"}


def test_customer_memory_tools_carry_the_customer_service_tag() -> None:
    assert set(registry.find_tools_by_tag("customer_service")) == CUSTOMER_MEMORY_TOOLS
    for name in CUSTOMER_MEMORY_TOOLS:
        assert "customer_service" in registry.get_tool_tags(name)
        assert "privacy" in registry.get_tool_tags(name)
    # GDPR erasure is the one that deletes, and is labelled as such.
    assert "gdpr" in registry.get_tool_tags("forget_me")
    assert "gdpr" not in registry.get_tool_tags("remember")


def test_find_tools_by_tag_is_case_insensitive_and_empty_for_unknown() -> None:
    assert set(registry.find_tools_by_tag("Customer_Service")) == CUSTOMER_MEMORY_TOOLS
    assert registry.find_tools_by_tag("no-such-tag") == []


def test_tag_index_maps_tag_to_tools() -> None:
    index = registry.list_tool_tags()
    assert set(index["customer_service"]) == CUSTOMER_MEMORY_TOOLS
    assert index["gdpr"] == ["forget_me"]


def test_untagged_tools_report_no_tags() -> None:
    assert registry.get_tool_tags("web_search") == []
    assert registry.get_dynamic_tool_source("web_search") == ""


def test_dynamic_tools_are_described_without_a_booted_agent() -> None:
    """The per-customer tools resolve to nothing until an agent binds them, so
    the catalog reads their descriptions from the factory instead of returning
    a blank row."""
    for name in CUSTOMER_MEMORY_TOOLS:
        assert registry.get_tool_description(name), f"{name} has no description"
    assert "customer" in registry.get_tool_description("remember").lower()


def test_describe_tool_reports_tags_and_the_config_that_wires_it() -> None:
    row = admin.describe_tool("forget_me")
    assert row["tags"] == ["customer_service", "privacy", "memory", "gdpr"]
    assert "memory.per_customer" in row["provided_by"]
    assert row["description"]

    # A normal registry tool has tags/provided_by present but empty.
    plain = admin.describe_tool("web_search")
    assert plain["tags"] == []
    assert plain["provided_by"] == ""


def test_find_tools_returns_full_rows_and_available_tags() -> None:
    out = admin.find_tools("customer_service")
    assert out["tag"] == "customer_service"
    assert out["count"] == 4
    assert {row["name"] for row in out["tools"]} == CUSTOMER_MEMORY_TOOLS
    assert all(row["provided_by"] for row in out["tools"])
    assert "gdpr" in out["available_tags"]


def test_find_tools_with_unknown_tag_lists_the_tags_that_exist() -> None:
    out = admin.find_tools("crm")
    assert out["count"] == 0
    assert out["tools"] == []
    assert "customer_service" in out["available_tags"]


def test_list_tools_exposes_the_tag_index() -> None:
    assert set(admin.list_tools()["tags"]["customer_service"]) == CUSTOMER_MEMORY_TOOLS


def test_registering_a_tool_stamps_its_tags_on_the_live_object() -> None:
    """A bound tool must carry the same labels the catalog reports."""
    from olano.memory.customer_memory import (
        make_customer_memory_tools,
        make_shared_knowledge_tools,
    )

    remember = next(t for t in make_customer_memory_tools("tagtest") if t.name == "remember")
    assert "tags" not in (remember.metadata or {})

    previous = registry._STATIC_TOOLS.get("remember")
    try:
        registry.register_tool("remember", remember)
        assert remember.metadata["tags"] == ["customer_service", "privacy", "memory"]
    finally:
        if previous is None:
            registry._STATIC_TOOLS.pop("remember", None)
        else:
            registry._STATIC_TOOLS["remember"] = previous

    # An untagged tool is left alone rather than gaining an empty `tags` list.
    knowledge = next(
        t for t in make_shared_knowledge_tools(Path("/tmp")) if t.name == "knowledge_search"
    )
    registry.apply_tool_tags([knowledge])
    assert "tags" not in (knowledge.metadata or {})
