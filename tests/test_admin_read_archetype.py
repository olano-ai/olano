"""``admin.read_archetype`` — read an archetype template without installing it.

``list_archetypes`` only carries picker fields, and templates live outside every
agent's filesystem, so this is the only way an operator agent can answer "what
does this archetype actually ship with?" instead of guessing from the universal
defaults.
"""

from __future__ import annotations

import json

import pytest

from olano import admin


def test_reads_the_template_verbatim() -> None:
    out = admin.read_archetype("customer-service")
    assert out["archetype"] == "customer-service"
    assert out["name"] == "CustomerServiceAgent"
    assert out["path"].endswith("customer-service.json5")

    config = out["config"]
    # The prompt and the security posture come back as authored — the whole
    # point of reading the template rather than the catalog row.
    assert "Privacy is your highest priority" in config["system_prompt"]
    assert config["memory"]["per_customer"] is True
    assert config["human_only"] is True
    assert "talk_to_agent" in config["disabled_tools"]


def test_resolves_by_configured_name_as_well_as_file_stem() -> None:
    by_stem = admin.read_archetype("customer-service")
    by_name = admin.read_archetype("CustomerServiceAgent")
    assert by_stem["path"] == by_name["path"]


def test_reports_the_effective_tools_an_install_would_produce() -> None:
    """`tools` must reflect the universal-defaults merge minus `disabled_tools`
    — not the raw template list, which omits the injected defaults."""
    out = admin.read_archetype("customer-service")
    tools = set(out["tools"])

    assert {
        "get_current_time",
        "get_current_date",
        "notification_send_text",
        "notification_send_photo",
        "notification_send_document",
    } <= tools
    # Denied universal defaults must not survive the merge.
    assert not tools & {"wait_n_seconds", "document_to_text", "pdf_to_text"}
    assert not tools & set(out["disabled_tools"])


def test_surfaces_builder_injected_memory_tools() -> None:
    """The privacy surface is wired from `memory.per_customer`, never from
    `tools` — so it must be reported separately or it looks absent."""
    out = admin.read_archetype("customer-service")
    pinned = out["auto_generated_tools"]["top_level_memory_tools"]
    assert {"remember", "recall", "recall_all", "forget_me"} <= set(pinned)
    # …and they are genuinely not in the plain tool list.
    assert "remember" not in out["tools"]


def test_teams_report_their_subagents() -> None:
    catalog = admin.list_archetypes()
    if not catalog["teams"]:
        pytest.skip("no team archetypes bundled")
    team = catalog["teams"][0]["name"]
    out = admin.read_archetype(team)
    assert out["config"].get("subagents")


def test_unknown_archetype_raises_with_the_available_list() -> None:
    with pytest.raises(FileNotFoundError) as exc:
        admin.read_archetype("no-such-archetype")
    assert "agentfather" in str(exc.value)


def test_agent_tool_returns_json() -> None:
    from olano.tools.olano_admin import olano_read_archetype

    payload = json.loads(olano_read_archetype.invoke({"name": "customer-service"}))
    assert payload["name"] == "CustomerServiceAgent"

    err = json.loads(olano_read_archetype.invoke({"name": "nope"}))
    assert "error" in err
