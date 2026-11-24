"""Tests for the /context breakdown estimator (olano.runtime.context_report)."""

from __future__ import annotations

from datetime import date

from olano.config.workspace import get_workspace
from olano.runtime.context_report import (
    estimate_tokens,
    model_context_window,
    render_context_report_text,
    report_for,
    session_message_tokens,
)


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1  # ceil(1/4)
    assert estimate_tokens("a" * 8) == 2


def _categories(report: dict) -> dict[str, dict]:
    return {c["key"]: c for c in report["categories"]}


def test_report_has_all_categories(base_config):
    ws = get_workspace(base_config.workspace)
    report = report_for(base_config, ws)

    assert report["model"] == base_config.model
    cats = _categories(report)
    assert set(cats) == {
        "system_prompt",
        "system_tools",
        "subagents",
        "memory_files",
        "skills",
        "mcp_tools",
    }
    # Every category reports a non-negative integer token estimate.
    for c in report["categories"]:
        assert isinstance(c["tokens"], int)
        assert c["tokens"] >= 0


def test_system_prompt_counts_instructions(base_config):
    ws = get_workspace(base_config.workspace)
    cats = _categories(report_for(base_config, ws))
    # base_config has a non-empty system_prompt → the bucket is populated.
    assert cats["system_prompt"]["tokens"] > 0


def test_memory_files_counted(base_config):
    # conftest's tmp_workspace seeds MEMORY.md.
    ws = get_workspace(base_config.workspace)
    cats = _categories(report_for(base_config, ws))
    assert cats["memory_files"]["count"] >= 1
    assert cats["memory_files"]["tokens"] > 0


def test_memory_picks_up_daily_log(base_config, tmp_workspace):
    today = date.today().isoformat()
    (tmp_workspace / "memories" / f"{today}.md").write_text(
        "# Today\n\n" + ("log entry. " * 50), encoding="utf-8"
    )
    ws = get_workspace(base_config.workspace)
    cats = _categories(report_for(base_config, ws))
    # MEMORY.md + today's log → at least 2 files.
    assert cats["memory_files"]["count"] >= 2


def test_system_tools_present(base_config):
    # Every agent runs with the universal + ops default toolset, so the resolved
    # toolset is never empty even though agent.json5 lists no extra tools.
    ws = get_workspace(base_config.workspace)
    cats = _categories(report_for(base_config, ws))
    assert cats["system_tools"]["count"] > 0
    assert cats["system_tools"]["tokens"] > 0


def test_mcp_tools_zero_without_a_live_summary(base_config):
    """No connect summary (agent not running) ⇒ nothing is bound ⇒ 0 tokens."""
    ws = get_workspace(base_config.workspace)
    cats = _categories(report_for(base_config, ws))
    assert cats["mcp_tools"]["tokens"] == 0
    assert cats["mcp_tools"]["count"] == 0
    assert cats["mcp_tools"]["servers"] == 0
    assert "on_demand" not in cats["mcp_tools"]


def test_mcp_tools_priced_from_the_live_summary(base_config):
    """Every tool a connected server exposes is bound into every request, so it
    is priced like a system tool — it used to be hardcoded to 0 as "on-demand",
    which pushed Composio's and Apollo's schemas into the Messages bar."""
    ws = get_workspace(base_config.workspace)
    summary = [
        {
            "url": "https://mcp.apollo.io/mcp",
            "ok": True,
            "tools": ["apollo_people_search", "apollo_org_lookup"],
            "tool_details": [
                {"name": "apollo_people_search", "description": "Search people", "tokens": 900},
                {"name": "apollo_org_lookup", "description": "Look up orgs", "tokens": 400},
            ],
        },
        # Connected but built by an older runtime: names only ⇒ flat baseline.
        {"url": "https://mcp.lusha.com/mcp", "ok": True, "tools": ["lusha_enrich"]},
        # Not connected ⇒ nothing bound ⇒ nothing charged.
        {
            "url": "https://connect.composio.dev/mcp",
            "ok": False,
            "status": "auth_required",
            "tools": [],
            "tool_details": [{"name": "x", "tokens": 5000}],
        },
    ]
    cats = _categories(report_for(base_config, ws, mcp_summary=summary))
    assert cats["mcp_tools"]["tokens"] == 900 + 400 + 80
    assert cats["mcp_tools"]["count"] == 3
    assert cats["mcp_tools"]["servers"] == 2


def test_tool_schema_tokens_prices_name_description_and_args():
    from olano.runtime.context_report import tool_schema_tokens

    class _Tool:
        name = "apollo_people_search"
        description = "Search people " * 20
        args = {"person_titles": {"type": "array"}, "page": {"type": "integer"}}

    priced = tool_schema_tokens(_Tool())
    # name + description + json schema, at 4 chars/token, plus the envelope
    assert priced > estimate_tokens(_Tool.description) + 40
    assert (
        tool_schema_tokens(type("Bare", (), {"name": "t", "description": "", "args": None})()) == 41
    )


def test_thread_message_tokens_counts_tool_traffic():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from olano.runtime.context_report import thread_message_tokens

    msgs = [
        HumanMessage(content="find 5 contacts"),
        AIMessage(
            content="",
            tool_calls=[{"name": "apollo_people_search", "args": {"q": "hr"}, "id": "c1"}],
        ),
        ToolMessage(content="x" * 4000, tool_call_id="c1"),
        AIMessage(content="done"),
    ]
    total = thread_message_tokens(msgs)
    # The 4000-char tool result alone is 1000 tokens; the rest is small.
    assert 1000 < total < 1100
    assert thread_message_tokens([]) == 0


# ── model_context_window ──────────────────────────────────────────────────────


def test_model_context_window_by_provider():
    assert model_context_window("anthropic:claude-haiku-4-5") == 200_000
    # Models the bundled cloud catalog knows resolve per-model (gpt-5.4 ships a
    # 1M+ window — the old 128K provider-prefix guess was wrong for it).
    assert model_context_window("openai:gpt-5.4") == 1_050_000
    assert model_context_window("openai:o3-mini") == 200_000  # ladder: reasoning window
    assert model_context_window("google:gemini-2.5-pro") == 1_000_000
    assert model_context_window("gemini:gemini-2.5-flash") == 1_000_000
    assert model_context_window("xai:grok-2") == 131_072
    assert model_context_window("ollama:llama3.2") == 32_768


def test_model_context_window_unknown_is_none():
    assert model_context_window("") is None
    assert model_context_window(None) is None
    assert model_context_window("acme:whatever") is None


# ── session_message_tokens ────────────────────────────────────────────────────


def test_session_message_tokens_skips_system_and_sums_turns():
    entries = [
        {"role": "system", "content": "x" * 400},  # skipped (already in system_prompt)
        {"role": "user", "content": "a" * 8},  # 2 tokens
        {"role": "assistant", "content": "b" * 8},  # 2 tokens
    ]
    assert session_message_tokens(entries) == 4


def test_session_message_tokens_tolerates_empty_and_none():
    assert session_message_tokens(None) == 0
    assert session_message_tokens([]) == 0
    assert session_message_tokens([{"role": "user"}]) == 0  # no content key


# ── render_context_report_text ────────────────────────────────────────────────


def test_render_text_includes_categories_and_bar(base_config):
    ws = get_workspace(base_config.workspace)
    report = report_for(base_config, ws)
    text = render_context_report_text(report, message_tokens=1000)

    # The model is NOT named by default: /model is sensitive on chat platforms
    # so a bot doesn't advertise its provider, and /context is exposed there.
    assert base_config.model not in text
    assert "Context" in text
    # Every category is labelled.
    for label in ("System prompt", "System tools", "Subagents", "Memory files", "Skills"):
        assert label in text
    # Anthropic model → known 200k window → percent bar + Free space row.
    assert "200.0k" in text
    assert "Free space" in text
    assert "█" in text or "░" in text
    assert "Estimated footprint" in text


def test_render_text_names_the_model_only_for_the_operator(base_config):
    """``show_model`` is how the local TUI / dashboard terminal opts back in."""
    ws = get_workspace(base_config.workspace)
    report = report_for(base_config, ws)

    assert base_config.model in render_context_report_text(report, show_model=True)
    assert base_config.model not in render_context_report_text(report, show_model=False)


def test_render_text_used_grows_with_messages(base_config):
    ws = get_workspace(base_config.workspace)
    report = report_for(base_config, ws)
    empty = render_context_report_text(report, message_tokens=0)
    filled = render_context_report_text(report, message_tokens=50_000)
    # The Messages row reflects the live fill.
    assert "Messages" in empty
    assert "50.0k" in filled


def test_render_text_unknown_window_omits_percent():
    report = {
        "model": "acme:mystery",
        "categories": [
            {"key": "system_prompt", "tokens": 100},
            {"key": "system_tools", "tokens": 200, "count": 3},
            {"key": "subagents", "tokens": 0, "count": 0},
            {"key": "memory_files", "tokens": 50, "count": 1},
            {"key": "skills", "tokens": 10, "count": 2},
            {"key": "mcp_tools", "tokens": 0, "count": 0, "on_demand": True},
        ],
    }
    text = render_context_report_text(report, message_tokens=0)
    assert "context window unknown" in text
    # No free-space row (window unknown) and no percent signs on rows.
    assert "Free space" not in text
    assert "on-demand" in text  # mcp row still shown
