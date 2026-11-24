"""The ``context_window`` policy: settings, resolution, the deepagents patches,
the catalog profile hand-off and the MCP result cap.

Background: with a model the installed provider package does not know (no
``profile``), deepagents kept **6 messages** after a compaction, offloaded
large tool results to an unwritable host-root path, and nothing capped MCP
results — a five-message conversation reached 150k tokens and then forgot
its own start. These pin the replacement policy end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from olano.config.agent_config import AgentConfig
from olano.config.system_settings import (
    CONTEXT_KEEP_FRACTION_DEFAULT,
    CONTEXT_KEEP_TOKENS_DEFAULT,
    MCP_RESULT_TOKEN_LIMIT_DEFAULT,
    ContextWindowSettings,
    effective_context_window,
    load_context_window_settings,
    set_context_window_settings,
)

# ── settings block ───────────────────────────────────────────────────────────


class TestContextWindowSettings:
    def test_defaults_are_not_deepagents_defaults(self):
        s = ContextWindowSettings()
        assert s.keep_fraction == CONTEXT_KEEP_FRACTION_DEFAULT == 0.35
        assert s.keep_tokens == CONTEXT_KEEP_TOKENS_DEFAULT == 40_000
        assert s.mcp_result_token_limit == MCP_RESULT_TOKEN_LIMIT_DEFAULT == 5_000

    def test_clamps_and_garbage(self):
        s = ContextWindowSettings.model_validate(
            {
                "keep_fraction": "nope",
                "keep_tokens": -5,
                "trigger_fraction": 5,
                "trigger_tokens": "x",
                "mcp_result_token_limit": 10**9,
            }
        )
        assert s.keep_fraction == CONTEXT_KEEP_FRACTION_DEFAULT
        assert s.keep_tokens == 2_000
        assert s.trigger_fraction == 0.98
        assert s.trigger_tokens == 170_000
        assert s.mcp_result_token_limit == 200_000

    def test_keep_is_pinned_below_trigger(self):
        """A keep at or above its trigger compacts nothing and then compacts
        again next turn; the block never persists that shape."""
        s = ContextWindowSettings(keep_fraction=0.8, trigger_fraction=0.5)
        assert s.keep_fraction < s.trigger_fraction
        s = ContextWindowSettings(keep_tokens=500_000, trigger_tokens=100_000)
        assert s.keep_tokens < s.trigger_tokens

    def test_round_trips_through_config_yaml(self):
        set_context_window_settings({"keep_fraction": 0.5, "mcp_result_token_limit": 0})
        loaded = load_context_window_settings()
        assert loaded.keep_fraction == 0.5
        assert loaded.mcp_result_token_limit == 0
        assert loaded.trigger_fraction == 0.85  # untouched keys keep their defaults


class TestEffectiveContextWindow:
    def test_agent_wins_when_it_has_an_opinion(self):
        set_context_window_settings({"keep_fraction": 0.2, "keep_tokens": 20_000})
        cfg = AgentConfig(name="t", model="fake:instant", context_window={"keep_fraction": 0.6})
        p = effective_context_window(cfg)
        assert p.keep_fraction == 0.6  # the override
        assert p.keep_tokens == 20_000  # inherited from the home block
        assert p.mcp_result_token_limit == MCP_RESULT_TOKEN_LIMIT_DEFAULT

    def test_agent_override_goes_through_the_same_clamps(self):
        cfg = AgentConfig(name="t", model="fake:instant", context_window={"keep_fraction": 7})
        assert effective_context_window(cfg).keep_fraction <= 0.8

    def test_no_agent_means_home_block(self):
        p = effective_context_window(None)
        assert p.keep_fraction == CONTEXT_KEEP_FRACTION_DEFAULT

    def test_context_sizes_fraction_when_window_known_tokens_otherwise(self):
        p = effective_context_window(None)
        assert p.context_sizes(272_000) == (("fraction", 0.85), ("fraction", 0.35))
        assert p.context_sizes(None) == (("tokens", 170_000.0), ("tokens", 40_000.0))
        assert p.context_sizes(0) == p.context_sizes(None)


# ── catalog profile hand-off ─────────────────────────────────────────────────


class TestAttachContextProfile:
    def test_attaches_the_catalog_window_when_the_client_has_none(self, monkeypatch):
        from olano import models as models_mod
        from olano.models.fake import build_fake_model

        monkeypatch.setattr(models_mod, "catalog_context_window", lambda s: 400_000)
        model = build_fake_model("fake:instant")
        assert not (getattr(model, "profile", None) or {}).get("max_input_tokens")
        out = models_mod.attach_context_profile(model, "openai-codex:gpt-6-luna")
        assert out.profile["max_input_tokens"] == 400_000

    def test_never_overwrites_a_resolved_profile(self, monkeypatch):
        from olano import models as models_mod
        from olano.models.fake import build_fake_model

        monkeypatch.setattr(models_mod, "catalog_context_window", lambda s: 400_000)
        model = build_fake_model("fake:instant")
        model.profile = {"max_input_tokens": 272_000}
        assert (
            models_mod.attach_context_profile(model, "x:y").profile["max_input_tokens"] == 272_000
        )

    def test_unknown_model_is_left_alone(self, monkeypatch):
        from olano import models as models_mod
        from olano.models.fake import build_fake_model

        monkeypatch.setattr(models_mod, "catalog_context_window", lambda s: None)
        model = build_fake_model("fake:instant")
        out = models_mod.attach_context_profile(model, "x:y")
        assert not (getattr(out, "profile", None) or {}).get("max_input_tokens")

    def test_catalog_context_window_reads_the_catalog_and_the_tier_table(self):
        from olano.models import catalog_context_window

        assert catalog_context_window("olano:pro") == 1_000_000
        assert catalog_context_window("") is None
        assert catalog_context_window("nosuch:model-xyz") is None


# ── the deepagents patches ───────────────────────────────────────────────────


def _fs_backend(root: Path):
    from deepagents.backends import FilesystemBackend

    return FilesystemBackend(root_dir=str(root), virtual_mode=False)


class TestSummarizationPolicy:
    def test_tokens_policy_without_a_profile(self, tmp_path):
        from olano.core.builder import _make_summarization_middleware
        from olano.models.fake import build_fake_model

        model = build_fake_model("fake:instant")
        mw = _make_summarization_middleware(
            model,
            _fs_backend(tmp_path),
            policy=effective_context_window(None),
            offload_root=tmp_path / ".olano",
        )
        # Never deepagents' ("messages", 6): an absolute token budget instead.
        assert mw._lc_helper.keep == ("tokens", 40_000.0)
        assert mw._lc_helper._trigger_clauses == [{"tokens": 170_000.0}]
        # Same as deepagents' factory: the whole evicted span is summarized, not
        # the last 4000 tokens of it (the class's own default).
        assert mw._lc_helper.trim_tokens_to_summarize is None
        assert mw._history_path_prefix == str(tmp_path / ".olano" / "conversation_history")
        # The other two paths deepagents derives from the same root follow it:
        # summary media, and the overflow tail the input-budget check offloads.
        assert mw._media_prefix == str(tmp_path / ".olano" / "conversation_history" / "media")
        assert mw._large_tool_results_prefix == str(tmp_path / ".olano" / "large_tool_results")

    def test_fraction_policy_with_a_profile(self, tmp_path):
        from olano.core.builder import _make_summarization_middleware
        from olano.models.fake import build_fake_model

        model = build_fake_model("fake:instant")
        model.profile = {"max_input_tokens": 272_000}
        mw = _make_summarization_middleware(
            model, _fs_backend(tmp_path), policy=effective_context_window(None), offload_root=None
        )
        assert mw._lc_helper.keep == ("fraction", 0.35)
        assert mw._lc_helper._trigger_clauses == [{"fraction": 0.85}]

    def test_compact_tool_middleware_uses_the_same_policy(self, tmp_path):
        from olano.core.builder import _build_compact_tool_middleware
        from olano.models.fake import build_fake_model

        mw = _build_compact_tool_middleware(
            build_fake_model("fake:instant"),
            _fs_backend(tmp_path),
            policy=effective_context_window(None),
            offload_root=tmp_path / ".olano",
        )
        assert mw is not None
        assert mw._summarization._lc_helper.keep == ("tokens", 40_000.0)
        assert [t.name for t in mw.tools] == ["compact_conversation"]

    def test_deepagents_factory_takes_the_policy_only_inside_a_build(self, tmp_path):
        from olano.core import builder
        from olano.models.fake import build_fake_model

        builder._ensure_summarization_offload_patch()
        import deepagents.graph as dg

        model = build_fake_model("fake:instant")
        backend = _fs_backend(tmp_path)
        # Outside a build: deepagents' own answer for a profile-less model.
        plain = dg.create_summarization_middleware(model, backend)
        assert plain._lc_helper.keep == ("messages", 6)
        # Inside one: olano's policy.
        token = builder._ACTIVE_CONTEXT_POLICY.set(
            {"policy": effective_context_window(None), "offload_root": tmp_path / ".olano"}
        )
        try:
            ours = dg.create_summarization_middleware(model, backend)
        finally:
            builder._ACTIVE_CONTEXT_POLICY.reset(token)
        assert ours._lc_helper.keep == ("tokens", 40_000.0)


class TestFilesystemOffloadPatch:
    def test_large_result_prefix_moves_under_the_workspace(self, tmp_path):
        from deepagents.middleware.filesystem import FilesystemMiddleware

        from olano.core import builder

        builder._ensure_filesystem_offload_patch()
        backend = _fs_backend(tmp_path)
        # Outside a build: deepagents' default (the unwritable host root).
        assert FilesystemMiddleware(backend=backend)._large_tool_results_prefix == (
            "/large_tool_results"
        )
        token = builder._ACTIVE_CONTEXT_POLICY.set(
            {"policy": effective_context_window(None), "offload_root": tmp_path / ".olano"}
        )
        try:
            mw = FilesystemMiddleware(backend=backend)
        finally:
            builder._ACTIVE_CONTEXT_POLICY.reset(token)
        assert mw._large_tool_results_prefix == str(tmp_path / ".olano" / "large_tool_results")
        assert mw._conversation_history_prefix == str(tmp_path / ".olano" / "conversation_history")
        assert mw._blobs_prefix == str(tmp_path / ".olano" / "blobs")

    def test_virtual_backend_is_never_repointed(self, tmp_path):
        from deepagents.backends import StateBackend
        from deepagents.middleware.filesystem import FilesystemMiddleware

        from olano.core import builder

        builder._ensure_filesystem_offload_patch()
        token = builder._ACTIVE_CONTEXT_POLICY.set(
            {"policy": effective_context_window(None), "offload_root": tmp_path / ".olano"}
        )
        try:
            mw = FilesystemMiddleware(backend=StateBackend())
        finally:
            builder._ACTIVE_CONTEXT_POLICY.reset(token)
        assert mw._large_tool_results_prefix == "/large_tool_results"


# ── MCP result cap ───────────────────────────────────────────────────────────


class _Req:
    def __init__(self, name: str, call_id: str = "call_1") -> None:
        self.tool_call = {"name": name, "id": call_id, "args": {}}


@pytest.fixture
def cap(tmp_path):
    from olano.core.mcp_result_cap_middleware import McpResultCapMiddleware

    return McpResultCapMiddleware(
        tool_names={"apollo_people_search"},
        token_limit=100,
        offload_dir=tmp_path / ".olano" / "large_tool_results",
        agent_name="t",
    )


class TestMcpResultCap:
    def test_small_result_passes_through_untouched(self, cap):
        msg = ToolMessage(content="[trusted=false]\nshort", tool_call_id="call_1")
        out = cap.wrap_tool_call(_Req("apollo_people_search"), lambda r: msg)
        assert out is msg

    def test_non_mcp_tool_is_never_capped(self, cap):
        msg = ToolMessage(content="x" * 10_000, tool_call_id="call_1")
        out = cap.wrap_tool_call(_Req("read_file"), lambda r: msg)
        assert out is msg

    async def test_large_result_is_offloaded_with_marker_and_preview(self, cap, tmp_path):
        body = "\n".join(f'{{"id": {i}, "name": "person {i}"}}' for i in range(200))
        text = f"[trusted=false]\n{body}"
        msg = ToolMessage(content=text, tool_call_id="call_9")

        async def handler(_r):
            return msg

        out = await cap.awrap_tool_call(_Req("apollo_people_search", "call_9"), handler)
        assert out is not msg
        assert out.tool_call_id == "call_9"
        assert out.content.startswith("[trusted=false]\n")  # the security marker survives
        path = tmp_path / ".olano" / "large_tool_results" / "call_9"
        assert path.read_text(encoding="utf-8") == text  # the full original, verbatim
        assert str(path) in out.content
        assert "read_file" in out.content and "limit is 100" in out.content
        # The content-aware preview: JSON Lines, ends kept, the run summarized.
        assert "Preview (JSON Lines, 200 lines" in out.content
        assert "lines left out" in out.content and "range id=" in out.content
        assert len(out.content) < len(text)

    def test_write_failure_keeps_the_original(self, tmp_path):
        from olano.core.mcp_result_cap_middleware import McpResultCapMiddleware

        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory")
        mw = McpResultCapMiddleware(
            tool_names={"apollo_people_search"}, token_limit=10, offload_dir=blocker
        )
        msg = ToolMessage(content="y" * 1000, tool_call_id="c")
        assert mw.wrap_tool_call(_Req("apollo_people_search"), lambda r: msg) is msg

    def test_non_toolmessage_results_pass_through(self, cap):
        from langgraph.types import Command

        cmd = Command(update={"messages": []})
        assert cap.wrap_tool_call(_Req("apollo_people_search"), lambda r: cmd) is cmd

    def test_builder_wires_only_connected_tools(self, tmp_path):
        from olano.core.mcp_result_cap_middleware import build_mcp_result_cap_middleware

        summary = [
            {"url": "a", "ok": True, "tools": ["apollo_people_search"]},
            {"url": "b", "ok": False, "tools": ["never_bound"]},
        ]
        state_dir = tmp_path / "state"
        (mw,) = build_mcp_result_cap_middleware(
            mcp_summary=summary, token_limit=5000, state_dir=state_dir, agent_name="t"
        )
        assert mw._names == {"apollo_people_search"}
        # Beneath the workspace's state folder, wherever the builder says it is.
        assert mw._dir == state_dir / "large_tool_results"
        # Off switch and no-MCP agents cost nothing.
        assert (
            build_mcp_result_cap_middleware(mcp_summary=summary, token_limit=0, state_dir=state_dir)
            == []
        )
        assert (
            build_mcp_result_cap_middleware(mcp_summary=None, token_limit=5000, state_dir=state_dir)
            == []
        )
        assert (
            build_mcp_result_cap_middleware(
                mcp_summary=[summary[1]], token_limit=5000, state_dir=state_dir
            )
            == []
        )

    def test_builder_offload_root_is_the_workspace_state_dir(self, base_config):
        """The offload folder follows ``Workspace.state_dir`` (one constant,
        ``WORKSPACE_STATE_DIRNAME``), never a literal folder name."""
        from olano.config.workspace import WORKSPACE_STATE_DIRNAME, get_workspace
        from olano.core.builder import _workspace_offload_prefix

        ws = get_workspace(base_config.workspace)
        assert ws.state_dir == ws.root / WORKSPACE_STATE_DIRNAME
        assert _workspace_offload_prefix(_fs_backend(ws.root)) == str(
            ws.state_dir / "conversation_history"
        )


# ── the agent-facing listing + prompt block ──────────────────────────────────


def _summary_with_disabled():
    return [
        {
            "url": "https://mcp.apollo.io/mcp",
            "transport": "http",
            "ok": True,
            "status": "connected",
            "tools": ["apollo_people_search"],
            "tool_details": [
                {"name": "apollo_people_search", "description": "Search people", "tokens": 500}
            ],
            "disabled": [{"name": "apollo_bulk_enrich", "description": "Enrich in bulk"}],
            "message": "1 tool",
        },
        {
            "url": "https://connect.composio.dev/mcp",
            "transport": "http",
            "ok": False,
            "status": "auth_required",
            "tools": [],
            "message": "OAuth not authorized",
        },
    ]


class TestSelfListMcpTools:
    def test_lists_enabled_and_switched_off_tools(self, lionclaw_home, monkeypatch):
        from olano.core import builder
        from olano.tools.self_config_tools import self_list_mcp_tools
        from olano.tools.talk_to import current_agent

        monkeypatch.setitem(builder._MCP_SUMMARY, "merli", _summary_with_disabled())
        token = current_agent.set("merli")
        try:
            out = json.loads(self_list_mcp_tools.invoke({"server": "apollo"}))
        finally:
            current_agent.reset(token)
        (row,) = out["servers"]
        assert row["connected"] is True
        assert [t["name"] for t in row["enabled_tools"]] == ["apollo_people_search"]
        assert [t["name"] for t in row["disabled_tools"]] == ["apollo_bulk_enrich"]
        assert "suggest enabling" in row["note"]

    def test_unconnected_server_reports_status_not_tools(self, lionclaw_home, monkeypatch):
        from olano.core import builder
        from olano.tools.self_config_tools import self_list_mcp_tools
        from olano.tools.talk_to import current_agent

        monkeypatch.setitem(builder._MCP_SUMMARY, "merli", _summary_with_disabled())
        token = current_agent.set("merli")
        try:
            everything = json.loads(self_list_mcp_tools.invoke({}))
            missing = json.loads(self_list_mcp_tools.invoke({"server": "hubspot"}))
        finally:
            current_agent.reset(token)
        composio = next(r for r in everything["servers"] if "composio" in r["url"])
        assert composio["connected"] is False and composio["status"] == "auth_required"
        assert "enabled_tools" not in composio
        assert "error" in missing

    def test_registered_under_connect_mcps(self):
        from olano.config.autonomy_defaults import SELF_CONFIG_TOOL_CAPABILITY
        from olano.tools.self_config_tools import get_tool

        assert SELF_CONFIG_TOOL_CAPABILITY["self_list_mcp_tools"] == "connect_mcps"
        assert get_tool("self_list_mcp_tools") is not None


class TestPromptBlockNamesSwitchedOffTools:
    def test_block_mentions_disabled_count_and_the_listing_tool(self, base_config):
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        ws = get_workspace(base_config.workspace)
        block = PromptAssembler(
            base_config, ws, mcp_summary=_summary_with_disabled()
        )._build_mcp_block()
        assert "apollo_people_search" in block
        assert "apollo_bulk_enrich" not in block  # never offered as callable
        assert "1 more switched off" in block
        assert "self_list_mcp_tools" in block

    def test_no_hint_when_nothing_is_switched_off(self, base_config):
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        ws = get_workspace(base_config.workspace)
        summary = _summary_with_disabled()
        summary[0]["disabled"] = []
        block = PromptAssembler(base_config, ws, mcp_summary=summary)._build_mcp_block()
        assert "switched off" not in block


# ── live server state + reconnect from the agent's own tools ─────────────────


def _write_agent_with_servers(name: str) -> None:
    from olano.config.home import get_olano_paths

    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": name,
                "model": "fake:instant",
                "mcp_servers": [
                    {"url": "https://mcp.apollo.io/mcp", "transport": "http", "auth": "oauth"},
                    {"url": "https://connect.composio.dev/mcp", "transport": "http"},
                    {"url": "https://mcp.lusha.com/mcp", "transport": "http", "enabled": False},
                ],
            }
        ),
        encoding="utf-8",
    )


class TestSelfListMcpServersLiveState:
    def test_merges_config_with_the_live_summary_and_token_state(self, lionclaw_home, monkeypatch):
        from olano.core import builder
        from olano.tools import self_config_tools
        from olano.tools.self_config_tools import self_list_mcp_servers
        from olano.tools.talk_to import current_agent

        _write_agent_with_servers("merli")
        monkeypatch.setitem(
            builder._MCP_SUMMARY,
            "merli",
            [
                {
                    "url": "https://mcp.apollo.io/mcp",
                    "ok": True,
                    "status": "connected",
                    "tools": ["a", "b"],
                    "message": "2 tools",
                },
                {
                    "url": "https://connect.composio.dev/mcp",
                    "ok": False,
                    "status": "timeout",
                    "tools": [],
                    "message": "Connection timed out",
                },
                {
                    "url": "https://mcp.lusha.com/mcp",
                    "ok": False,
                    "status": "disabled",
                    "tools": [],
                    "message": "Disabled in config",
                },
            ],
        )
        # The stored-token classification is read per (agent, url); pin it.
        import olano.core.mcp_setup as mcp_setup

        monkeypatch.setattr(
            mcp_setup,
            "oauth_token_state",
            lambda agent, url: {"authorized": True, "state": "valid"},
        )
        assert self_config_tools  # imported for the patch target's module identity
        token = current_agent.set("merli")
        try:
            out = json.loads(self_list_mcp_servers.invoke({}))
        finally:
            current_agent.reset(token)
        by_url = {r["url"]: r for r in out["servers"]}
        apollo = by_url["https://mcp.apollo.io/mcp"]
        assert apollo["connected"] is True and apollo["bound_tools"] == 2
        assert apollo["authorized"] is True and apollo["token_state"] == "valid"
        assert "connected" in apollo["fix"]
        composio = by_url["https://connect.composio.dev/mcp"]
        assert composio["status"] == "timeout" and composio["connected"] is False
        assert "self_reload_mcp_servers" in composio["fix"]
        lusha = by_url["https://mcp.lusha.com/mcp"]
        assert lusha["enabled"] is False and lusha["status"] == "disabled"
        assert "MCP tab" in lusha["fix"]
        assert all("oauth_client_secret" not in r for r in out["servers"])

    def test_not_running_is_said_rather_than_guessed(self, lionclaw_home, monkeypatch):
        from olano.core import builder
        from olano.tools.self_config_tools import self_list_mcp_servers
        from olano.tools.talk_to import current_agent

        _write_agent_with_servers("merli")
        monkeypatch.setitem(builder._MCP_SUMMARY, "merli", [])
        token = current_agent.set("merli")
        try:
            out = json.loads(self_list_mcp_servers.invoke({}))
        finally:
            current_agent.reset(token)
        assert {r["status"] for r in out["servers"]} == {"not_running"}
        assert all(r["connected"] is False for r in out["servers"])


class TestSelfReloadMcpServers:
    async def test_reloads_through_the_bound_runtime(self, lionclaw_home, monkeypatch):
        from olano.tools import talk_to
        from olano.tools.self_config_tools import self_reload_mcp_servers
        from olano.tools.talk_to import current_agent

        _write_agent_with_servers("merli")
        calls: list[str] = []

        class _Runtime:
            async def reload_agent_mcp(self, name: str) -> dict:
                calls.append(name)
                return {
                    "ok": True,
                    "running": True,
                    "servers": [
                        {
                            "url": "https://connect.composio.dev/mcp",
                            "ok": True,
                            "status": "connected",
                            "tools": ["x"],
                            "message": "1 tool",
                        },
                        {
                            "url": "https://mcp.apollo.io/mcp",
                            "ok": False,
                            "status": "auth_required",
                            "tools": [],
                            "message": "OAuth not authorized",
                        },
                    ],
                    "notice": "composio came online",
                }

        monkeypatch.setattr(talk_to, "get_bound_runtime", lambda: _Runtime())
        token = current_agent.set("merli")
        try:
            out = json.loads(await self_reload_mcp_servers.ainvoke({}))
        finally:
            current_agent.reset(token)
        assert calls == ["merli"]
        by_url = {r["url"]: r for r in out["servers"]}
        assert by_url["https://connect.composio.dev/mcp"]["connected"] is True
        # A reload cannot invent consent: the answer for that row is a sign-in.
        assert "self_authorize_mcp_server" in by_url["https://mcp.apollo.io/mcp"]["fix"]
        assert out["notice"] == "composio came online"

    async def test_without_a_runtime_it_explains_instead_of_pretending(
        self, lionclaw_home, monkeypatch
    ):
        from olano.tools import talk_to
        from olano.tools.self_config_tools import self_reload_mcp_servers
        from olano.tools.talk_to import current_agent

        _write_agent_with_servers("merli")
        monkeypatch.setattr(talk_to, "get_bound_runtime", lambda: None)
        token = current_agent.set("merli")
        try:
            out = json.loads(await self_reload_mcp_servers.ainvoke({}))
        finally:
            current_agent.reset(token)
        assert "error" in out and "Reload MCP" in out["error"]

    def test_registered_under_connect_mcps(self):
        from olano.config.autonomy_defaults import SELF_CONFIG_TOOL_CAPABILITY
        from olano.tools.self_config_tools import get_tool

        assert SELF_CONFIG_TOOL_CAPABILITY["self_reload_mcp_servers"] == "connect_mcps"
        assert get_tool("self_reload_mcp_servers") is not None
