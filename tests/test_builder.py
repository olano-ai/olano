"""Tests for build_agent — backend selection, tool wiring, checkpointer sharing."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from olano.config.agent_config import AgentConfig, AutonomyConfig, MemoryConfig, SubAgentConfig
from olano.config.backend_config import (
    FilesystemBackendConfig,
    LocalShellBackendConfig,
    StoreBackendConfig,
)
from olano.config.system_settings import AUTONOMY_CAPABILITIES
from olano.core.builder import (
    _RAG_WATCHERS,
    _auto_ingest_workspace,
    _build_all_tools,
    _build_deepagents_backend,
    _build_interrupt_on,
    _build_memory_tools,
    get_checkpointer,
)


class TestCheckpointer:
    def test_same_agent_returns_same_instance(self) -> None:
        c1 = get_checkpointer("my-agent")
        c2 = get_checkpointer("my-agent")
        assert c1 is c2

    def test_different_agents_different_instances(self) -> None:
        c1 = get_checkpointer("agent-alpha")
        c2 = get_checkpointer("agent-beta")
        assert c1 is not c2


class TestBuildMemoryTools:
    def test_markdown_tools_wired(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        tools = _build_memory_tools(base_config, tmp_workspace)
        names = [t.name for t in tools]
        assert "memory_save" in names
        assert "memory_read" in names
        assert "memory_read_today" in names

    def test_rag_disabled_no_rag_tools(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        base_config.memory = MemoryConfig(markdown=True, rag=False)
        tools = _build_memory_tools(base_config, tmp_workspace)
        names = [t.name for t in tools]
        assert not any("rag" in n for n in names)

    def test_no_memory_config(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        tools = _build_memory_tools(base_config, tmp_workspace)
        assert tools == []


class TestInterruptConfig:
    def test_profile_save_tools_are_not_hitl_interrupted(self, base_config: AgentConfig) -> None:
        interrupt_on = _build_interrupt_on(base_config)

        assert interrupt_on is not None
        assert "user_profile_save" not in interrupt_on
        assert "identity_preference_save" not in interrupt_on
        assert "soul_preference_save" not in interrupt_on

    def test_developer_profile_minimal_floor(self, base_config: AgentConfig) -> None:
        # "Trusted — act freely": money + secret writes only. Notably NOT the
        # task tool (H1: subagent delegation is no longer gated by default).
        base_config.security.profile = "developer"
        io = _build_interrupt_on(base_config)
        assert io is not None
        assert "wallet_send" in io
        assert "secret_set" in io
        assert "task" not in io
        assert "write_file" not in io
        assert "gmail_send" not in io
        assert "execute" not in io

    def test_supervised_profile_adds_writes_and_shell(self, base_config: AgentConfig) -> None:
        base_config.security.profile = "safe"
        io = _build_interrupt_on(base_config) or {}
        assert {"gmail_send", "execute", "secret_get", "wallet_send"} <= set(io)
        assert "write_file" not in io  # filesystem writes only at locked-down
        assert "task" not in io

    def test_lockdown_profile_adds_fs_writes_and_delegation(self, base_config: AgentConfig) -> None:
        base_config.security.profile = "production"
        io = _build_interrupt_on(base_config) or {}
        assert {"write_file", "edit_file", "task", "gmail_send", "execute"} <= set(io)

    def test_write_toggle_adds_external_writes(self, base_config: AgentConfig) -> None:
        base_config.security.profile = "developer"
        base_config.security.hitl_write_tools = True
        io = _build_interrupt_on(base_config) or {}
        assert "gmail_send" in io
        assert "execute" not in io

    def test_shell_toggle_adds_shell(self, base_config: AgentConfig) -> None:
        base_config.security.profile = "developer"
        base_config.security.hitl_shell = True
        io = _build_interrupt_on(base_config) or {}
        assert "execute" in io

    def test_master_switch_disables_all_hitl(self, base_config: AgentConfig) -> None:
        base_config.security.hitl_enabled = False
        assert _build_interrupt_on(base_config) is None

    def test_per_tool_override_forces_on_and_off(self, base_config: AgentConfig) -> None:
        base_config.security.profile = "developer"
        base_config.security.tools = {
            "task": {"approval": "always"},  # force on despite Trusted default
            "wallet_send": {"approval": "never"},  # force off despite floor
        }
        io = _build_interrupt_on(base_config) or {}
        assert "task" in io
        assert "wallet_send" not in io

    def test_read_only_tools_never_interrupted(self, base_config: AgentConfig) -> None:
        # M4: coinbase_list_orders is a read; it must never be gated, even with
        # the strictest profile and both toggles on.
        base_config.security.profile = "production"
        base_config.security.hitl_write_tools = True
        base_config.security.hitl_shell = True
        io = _build_interrupt_on(base_config) or {}
        assert "coinbase_list_orders" not in io


class TestBackendTools:
    """
    BackendConfig dispatches to deepagents' FilesystemBackend / LocalShellBackend via
    _build_deepagents_backend(). Extra tools (memory, custom) are passed separately.
    _build_all_tools() only returns extra tools, not shell tools.

    The exact-list tests pin chat self-configuration OFF via _autonomy_off():
    with it on (the shipped default) the builder also grants the 17 self_*
    tools, which are covered by tests/test_agent_autonomy.py — here they would
    only bury the secret-tools opt-in rule these tests exist to guard.
    """

    @staticmethod
    def _autonomy_off(config: AgentConfig) -> None:
        config.autonomy = AutonomyConfig(**dict.fromkeys(AUTONOMY_CAPABILITIES, False))

    def test_filesystem_backend_no_extra_tools_by_default(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = []
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        self._autonomy_off(base_config)
        tools = _build_all_tools(base_config, tmp_workspace)
        # Credentials-vault secret_* tools are opt-in like any other integration
        # tool — an agent that never asked for vault access shouldn't have it,
        # even with memory disabled.
        names = sorted(getattr(t, "name", "") for t in tools)
        assert names == []

    def test_secret_tools_are_opt_in_via_config_tools(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = ["secret_get", "secret_list"]
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        self._autonomy_off(base_config)
        tools = _build_all_tools(base_config, tmp_workspace)
        names = sorted(getattr(t, "name", "") for t in tools)
        # Only the two explicitly listed names are bound — secret_set, which
        # wasn't asked for, is not silently along for the ride.
        assert names == ["secret_get", "secret_list"]

    def test_disabled_tools_strip_explicitly_requested_secret_tools(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = ["secret_get", "secret_set"]
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        base_config.disabled_tools = ["secret_get"]
        self._autonomy_off(base_config)
        tools = _build_all_tools(base_config, tmp_workspace)
        names = sorted(getattr(t, "name", "") for t in tools)
        # disabled_tools still wins over an explicit `tools` request.
        assert names == ["secret_set"]

    def test_custom_tool_included(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = ["safe_shell"]
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        tools = _build_all_tools(base_config, tmp_workspace)
        names = [getattr(t, "name", "") for t in tools]
        # The Python function is `safe_shell_tool` but @tool("safe_shell")
        # registers the LangChain tool name as "safe_shell" so the LLM sees
        # the friendlier identifier. Tests assert the registered name.
        assert "safe_shell" in names

    def test_memory_tools_included(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = []
        tools = _build_all_tools(base_config, tmp_workspace)
        names = [getattr(t, "name", "") for t in tools]
        assert "memory_save" in names
        assert "memory_read_today" in names

    def test_deduplication(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        base_config.backend = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        base_config.tools = ["safe_shell", "safe_shell"]
        base_config.memory = MemoryConfig(markdown=False, rag=False)
        tools = _build_all_tools(base_config, tmp_workspace)
        names = [getattr(t, "name", "") for t in tools]
        assert names.count("safe_shell") == 1

    def test_deepagents_backend_filesystem(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        from deepagents.backends import FilesystemBackend

        cfg = FilesystemBackendConfig(root_dir=str(tmp_workspace))
        backend = _build_deepagents_backend(cfg, tmp_workspace)
        assert isinstance(backend, FilesystemBackend)

    def test_deepagents_backend_local_shell(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        from deepagents.backends import LocalShellBackend

        cfg = LocalShellBackendConfig(root_dir=str(tmp_workspace))
        backend = _build_deepagents_backend(cfg, tmp_workspace)
        assert isinstance(backend, LocalShellBackend)

    def test_deepagents_backend_store_namespace_is_callable(self, tmp_workspace: Path) -> None:
        from deepagents.backends import StoreBackend

        cfg = StoreBackendConfig(namespace=["agent", "memory"])
        backend = _build_deepagents_backend(cfg, tmp_workspace)

        assert isinstance(backend, StoreBackend)
        assert backend._get_namespace() == ("agent", "memory")


class TestSummarizationOffloadPatch:
    """The deepagents conversation-history offload defaults to the absolute
    /conversation_history path, which is unwritable on a real filesystem backend
    (virtual_mode=False). The builder repoints it under the workspace root.

    These tests exercise the REAL deepagents backend classes on purpose: the
    original bug was that the repoint read ``backend.root_dir``, but
    ``FilesystemBackend`` / ``LocalShellBackend`` expose their root as
    ``backend.cwd`` — so a ``SimpleNamespace(root_dir=...)`` stub hid the
    regression while production silently wrote to the host root and got EACCES.
    """

    def test_prefix_helper_uses_real_filesystem_backend_root(self, tmp_path) -> None:
        from deepagents.backends import FilesystemBackend, LocalShellBackend

        from olano.core.builder import _workspace_offload_prefix

        expected = str(tmp_path.resolve() / ".olano" / "conversation_history")
        fb = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)
        ls = LocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
        assert _workspace_offload_prefix(fb) == expected
        assert _workspace_offload_prefix(ls) == expected

    def test_prefix_helper_skips_virtual_backends(self, tmp_path) -> None:
        from deepagents.backends import FilesystemBackend, StateBackend

        from olano.core.builder import _workspace_offload_prefix

        # virtual_mode=True resolves /conversation_history under the root already;
        # StateBackend is in-memory. Both must be left at the deepagents default.
        assert (
            _workspace_offload_prefix(FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True))
            is None
        )
        assert _workspace_offload_prefix(StateBackend()) is None

    def test_offload_repointed_under_workspace(self, monkeypatch, tmp_path) -> None:
        import types

        import deepagents.graph as dg_graph
        from deepagents.backends import FilesystemBackend

        from olano.core import builder

        # A stand-in for deepagents' factory result: it sets the broken default prefix.
        def fake_factory(model, backend):
            mw = types.SimpleNamespace()
            mw._history_path_prefix = "/conversation_history"
            return mw

        monkeypatch.setattr(dg_graph, "create_summarization_middleware", fake_factory)
        # Force (re)install of the wrapper for this test, restored afterward.
        monkeypatch.setattr(builder, "_SUMMARIZATION_OFFLOAD_PATCHED", False)
        builder._ensure_summarization_offload_patch()

        backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)
        mw = dg_graph.create_summarization_middleware(model="m", backend=backend)
        assert mw._history_path_prefix == str(
            tmp_path.resolve() / ".olano" / "conversation_history"
        )
        # A stand-in without the other two prefixes is not given them.
        assert not hasattr(mw, "_media_prefix")

    def test_offload_left_alone_for_virtual_backend(self, monkeypatch) -> None:
        import types

        import deepagents.graph as dg_graph
        from deepagents.backends import StateBackend

        from olano.core import builder

        def fake_factory(model, backend):
            mw = types.SimpleNamespace()
            mw._history_path_prefix = "/conversation_history"
            return mw

        monkeypatch.setattr(dg_graph, "create_summarization_middleware", fake_factory)
        monkeypatch.setattr(builder, "_SUMMARIZATION_OFFLOAD_PATCHED", False)
        builder._ensure_summarization_offload_patch()

        # StateBackend (virtual/in-memory) handles /conversation_history fine — leave it.
        mw = dg_graph.create_summarization_middleware(model="m", backend=StateBackend())
        assert mw._history_path_prefix == "/conversation_history"


class TestBuildAgentIntegration:
    def test_build_agent_calls_create_deep_agent(self, base_config: AgentConfig) -> None:
        mock_graph = MagicMock()
        # create_deep_agent is imported inside build_agent() from deepagents,
        # so we patch it in the deepagents namespace
        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent

            result = build_agent(base_config)

        mock_create.assert_called_once()
        assert result is mock_graph
        middleware = mock_create.call_args.kwargs["middleware"]
        assert middleware
        names = [type(m).__name__ for m in middleware]
        # deepagents 0.7 dropped TodoListMiddleware from the create_deep_agent
        # default stack, so olano restores it explicitly. It leads the custom
        # list (before the security chain), matching its former position as a
        # base-stack middleware that ran ahead of olano's spliced-in security.
        assert names[0] == "TodoListMiddleware"
        assert "OlanoSecurityMiddleware" in names
        # Mid-turn steering (core/steer_middleware.py) sits ahead of the
        # security middleware: langchain makes the FIRST middleware defining
        # `before_model` the node the `tools` edge returns to, so a steer is
        # appended before the next model call — and security still scans it.
        assert names.index("SteerMiddleware") < names.index("OlanoSecurityMiddleware")
        # Never on a subagent: a `task` helper must not consume the user's aside.
        for sub in mock_create.call_args.kwargs.get("subagents") or []:
            sub_names = [type(m).__name__ for m in (sub.get("middleware") or [])]
            assert "SteerMiddleware" not in sub_names

    def test_build_agent_wraps_tools_in_error_boundary(self, base_config: AgentConfig) -> None:
        """Every tool handed to create_deep_agent must be error-boundary wrapped so a
        raising tool body degrades to a message instead of crashing the turn."""
        mock_graph = MagicMock()
        base_config.tools = ["safe_shell"]
        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        tools = mock_create.call_args.kwargs["tools"]
        assert tools, "expected extra tools to be passed to create_deep_agent"
        assert all(getattr(t, "_lionclaw_error_boundary_wrapped", False) for t in tools), (
            "all tools reaching create_deep_agent must be guarded by the error boundary"
        )

    def test_build_agent_builds_plain_subagent_model(self, base_config: AgentConfig) -> None:
        mock_graph = MagicMock()
        base_config.model = "openai-codex:gpt-5.4-mini"
        base_config.subagents = [
            SubAgentConfig(
                name="helper",
                description="Helps with focused tasks",
                system_prompt="Help with focused tasks.",
            )
        ]

        def fake_build_model(
            model_str: str,
            model_kwargs: dict[str, Any],
            reasoning_effort: str | None = None,
            max_output_tokens: int | None = None,
        ):
            # `max_output_tokens` (message_policy) is passed by the main-agent
            # path only — subagent output is working material, not the reply
            # the cap is about — but the fake stands in for both calls.
            return f"built:{model_str}"

        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", side_effect=fake_build_model),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        subagents = mock_create.call_args.kwargs["subagents"]
        assert subagents[0]["model"] == "built:openai-codex:gpt-5.4-mini"

    def test_build_agent_adds_document_tools_to_subagents(self, base_config: AgentConfig) -> None:
        mock_graph = MagicMock()
        base_config.subagents = [
            SubAgentConfig(
                name="reader",
                description="Reads files",
                system_prompt="Read files.",
                tools=["web_search"],
            )
        ]

        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        subagent_tools = mock_create.call_args.kwargs["subagents"][0]["tools"]
        subagent_tool_names = {tool.name for tool in subagent_tools}
        assert "web_search" in subagent_tool_names
        assert "document_to_text" in subagent_tool_names
        assert "pdf_to_text" in subagent_tool_names

    def test_build_agent_store_backend_passes_store_and_defaults_namespace(
        self, base_config: AgentConfig
    ) -> None:
        """The persistent "Virtual File System" mode (StoreBackend) must receive a
        store= on create_deep_agent and be namespaced by agent name."""
        from olano.config.backend_config import StoreBackendConfig

        base_config.backend = StoreBackendConfig()
        sentinel_store = MagicMock()
        mock_graph = MagicMock()

        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
            patch("olano.core.builder.get_store", return_value=sentinel_store),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        assert mock_create.call_args.kwargs.get("store") is sentinel_store
        backend = mock_create.call_args.kwargs["backend"]
        assert backend._get_namespace() == (base_config.name,)

    def test_build_agent_non_store_backend_passes_no_store(self, base_config: AgentConfig) -> None:
        """Real-filesystem backends never receive a store= argument."""
        mock_graph = MagicMock()

        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        assert mock_create.call_args.kwargs.get("store") is None

    def test_build_agent_initializes_phase1_workspace_state(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        mock_graph = MagicMock()
        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph),
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        assert (tmp_workspace / ".olano" / "state" / "workspace").exists()
        assert (tmp_workspace / ".olano" / "cache").exists()
        assert (tmp_workspace / ".olano" / "workspace_map.md").exists()
        assert (tmp_workspace / ".olano" / "workspace_bridge.md").exists()

    def test_build_agent_refreshes_cartography_before_prompt_assembly(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        def _write_fresh_state(workspace: Any) -> None:
            workspace.workspace_map_path.write_text(
                "# Workspace Map\nfresh-map\n", encoding="utf-8"
            )
            workspace.workspace_bridge_path.write_text(
                "# Workspace Bridge\nfresh-bridge\n", encoding="utf-8"
            )

        mock_graph = MagicMock()
        with (
            patch("deepagents.create_deep_agent", return_value=mock_graph) as mock_create,
            patch("olano.core.builder._build_model", return_value=MagicMock()),
            patch(
                "olano.core.builder._run_cartography_incremental", side_effect=_write_fresh_state
            ),
        ):
            from olano.core.builder import build_agent

            build_agent(base_config)

        system_prompt = mock_create.call_args.kwargs["system_prompt"]
        assert "fresh-map" in system_prompt
        assert "fresh-bridge" in system_prompt


class TestRagWatcherLifecycle:
    def test_auto_ingest_workspace_does_not_duplicate_watcher(self, tmp_workspace: Path) -> None:
        rag = MagicMock()
        mock_watcher = MagicMock()
        mock_watcher.is_running = True
        mock_watcher.start.return_value = True

        _RAG_WATCHERS.clear()
        with patch("olano.memory.rag_watcher.RAGDirectoryWatcher", return_value=mock_watcher):
            _auto_ingest_workspace(rag, tmp_workspace)
            _auto_ingest_workspace(rag, tmp_workspace)

        assert len(_RAG_WATCHERS) == 1
        mock_watcher.start.assert_called_once()
        _RAG_WATCHERS.clear()

    def test_system_knowledge_is_synced_so_removed_files_leave_the_index(
        self, tmp_workspace: Path
    ) -> None:
        """`system/knowledge` goes through `sync_directory(delete_removed=True)`.

        Per-file `ingest_file` never forgets a file: the legacy ARCHITECTURE /
        COMMANDS / TROUBLESHOOTING copies an upgrade purges from disk would have
        stayed searchable in the `knowledgebase` collection forever.
        """
        rag = MagicMock()
        knowledge_dir = tmp_workspace / "system" / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "USER_GUIDE.md").write_text("# guide\n", encoding="utf-8")

        _RAG_WATCHERS.clear()
        with patch("olano.memory.rag_watcher.RAGDirectoryWatcher", return_value=MagicMock()):
            _auto_ingest_workspace(rag, tmp_workspace)
        _RAG_WATCHERS.clear()

        knowledge_syncs = [
            call
            for call in rag.sync_directory.call_args_list
            if call.kwargs.get("directory") == knowledge_dir
        ]
        assert len(knowledge_syncs) == 1
        assert knowledge_syncs[0].kwargs["collection"] == "knowledgebase"
        assert knowledge_syncs[0].kwargs["delete_removed"] is True
        ingested = [str(c.args[0]) for c in rag.ingest_file.call_args_list]
        assert not any(str(knowledge_dir) in path for path in ingested)


class TestAsyncBuildOffLoop:
    """The async build must not block the event loop.

    Agent build is seconds of synchronous CPU work (toolkit imports, the
    deepagents patch, skill discovery, graph compilation). Inline on the loop it
    starves uvicorn for the whole boot, so `/api/status` goes unanswered and a
    healthy runtime reads as offline in the dashboard. These tests pin the two
    properties that make moving it off the loop safe: the loop keeps running, and
    the one step that cannot move — the per-agent credential overlay written into
    the global `os.environ` — never becomes observable to concurrent code.

    Not covered here: the async checkpointer's loop affinity. It binds to the
    loop that opens it, so `build_agent_async` awaits it on the loop *before*
    handing it to the off-loop compile. Exercising that end to end needs a real
    `aget_state`, whose aiosqlite connection runs on a non-daemon thread and
    hangs interpreter shutdown for the whole run.
    """

    @staticmethod
    async def _ticks_during(coro: Any) -> tuple[Any, int]:
        """Run `coro`, counting how many times a 10ms heartbeat gets to run."""
        ticks = 0
        done = False

        async def heartbeat() -> None:
            nonlocal ticks
            while not done:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            result = await coro
        finally:
            done = True
            await beat
        return result, ticks

    async def test_build_does_not_block_the_event_loop(self, base_config: AgentConfig) -> None:
        # Stand in for the real cost: ~5.8s of toolkit imports on a cold process.
        def slow_tools(*_a: Any, **_k: Any) -> list[Any]:
            time.sleep(0.4)
            return []

        with (
            patch("deepagents.create_deep_agent", return_value=MagicMock()),
            patch("olano.core.builder._build_model", return_value=MagicMock()),
            patch("olano.core.builder._build_extra_tools", side_effect=slow_tools),
        ):
            from olano.core.builder import build_agent_async

            _, ticks = await self._ticks_during(build_agent_async(base_config))

        # On the loop this whole stretch is one uninterruptible block and the
        # heartbeat cannot run at all; off it, ~40 ticks fit in 0.4s.
        assert ticks >= 10, f"event loop was starved during build (only {ticks} ticks)"

    async def test_graph_is_compiled_off_the_main_thread(self, base_config: AgentConfig) -> None:
        seen: dict[str, str] = {}

        def record(**kwargs: Any) -> Any:
            seen["thread"] = threading.current_thread().name
            return MagicMock()

        with (
            patch("deepagents.create_deep_agent", side_effect=record),
            patch("olano.core.builder._build_model", return_value=MagicMock()),
        ):
            from olano.core.builder import build_agent_async

            await build_agent_async(base_config)

        assert seen["thread"] != threading.main_thread().name

    async def test_credential_overlay_never_leaks_to_concurrent_readers(
        self, base_config: AgentConfig, monkeypatch: Any
    ) -> None:
        """The overlay window stays on the loop, so nothing else can observe it.

        `_transient_env` writes the agent's provider key into the process-global
        `os.environ` and restores it. Moved into a worker thread that window
        would be visible to every concurrent request handler — one agent's key
        answering another agent's turn. It must reach the model build and be
        invisible everywhere else.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
        base_config.env = {"ANTHROPIC_API_KEY": "per-agent-key"}

        # Every _build_model call, in order. The first is the agent's own model
        # (the one the overlay is for); later calls are subagent models, which
        # have always been built from the ambient env — they sit outside the
        # transient window by design, unchanged by the move off the loop.
        model_env_seen: list[str | None] = []

        def capture_model(*_a: Any, **_k: Any) -> Any:
            model_env_seen.append(os.environ.get("ANTHROPIC_API_KEY"))
            return MagicMock()

        # Hold the build in its off-loop phase long enough for the sampler below
        # to run many times against the live process env.
        def slow_tools(*_a: Any, **_k: Any) -> list[Any]:
            time.sleep(0.3)
            return []

        samples: list[str | None] = []
        done = False

        async def sample_env() -> None:
            while not done:
                samples.append(os.environ.get("ANTHROPIC_API_KEY"))
                await asyncio.sleep(0.005)

        with (
            patch("deepagents.create_deep_agent", return_value=MagicMock()),
            patch("olano.core.builder._build_model", side_effect=capture_model),
            patch("olano.core.builder._build_extra_tools", side_effect=slow_tools),
        ):
            from olano.core.builder import build_agent_async

            sampler = asyncio.create_task(sample_env())
            try:
                await build_agent_async(base_config)
            finally:
                done = True
                await sampler

        assert model_env_seen[0] == "per-agent-key", "overlay must reach the agent's model build"
        assert samples, "sampler never ran — the loop was blocked"
        assert set(samples) == {"ambient-key"}, (
            "a concurrent reader observed the per-agent overlay in os.environ"
        )
        assert os.environ["ANTHROPIC_API_KEY"] == "ambient-key", "overlay was not restored"

    async def test_async_and_sync_paths_prepare_the_same_shape(
        self, base_config: AgentConfig
    ) -> None:
        with patch("olano.core.builder._build_model", return_value=MagicMock()):
            from olano.core.builder import _prepare_agent_build, _prepare_agent_build_async

            sync_prepared = _prepare_agent_build(base_config)
            async_prepared = await _prepare_agent_build_async(base_config)

        assert set(sync_prepared) == set(async_prepared)
        assert sync_prepared["system_prompt"] == async_prepared["system_prompt"]
        assert [t.name for t in sync_prepared["extra_tools"]] == [
            t.name for t in async_prepared["extra_tools"]
        ]


def test_local_shell_backend_gets_a_scrubbed_env_with_the_interpreter_shims(
    tmp_workspace: Path, lionclaw_home: Path, monkeypatch
) -> None:
    """deepagents defaults LocalShellBackend to env={} — no PATH, no HOME — so a
    bare `python` only worked by dash's compiled default path and no shim dir
    could ever reach it. It now gets the same scrubbed env `shell_guard.run_local`
    hands the same `execute` tool on the host lane, plus the shims appended."""
    from deepagents.backends import LocalShellBackend

    from olano.security import shell_guard as sg

    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "supersecret")
    monkeypatch.setenv("HOME", "/tmp/home-x")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    cfg = LocalShellBackendConfig(root_dir=str(tmp_workspace))
    backend = _build_deepagents_backend(cfg, tmp_workspace)

    assert isinstance(backend, LocalShellBackend)
    env = backend._env
    shim = str(lionclaw_home / "sandbox" / "tools" / "shims")
    assert env["PATH"] == f"/usr/bin:/bin:{shim}"
    assert env["HOME"] == "/tmp/home-x"
    for key in sg._ALWAYS_STRIP_ENV:
        assert key not in env
    assert (Path(shim) / "python").exists()
