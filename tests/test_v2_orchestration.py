"""Phase 6a orchestration test suite."""

from __future__ import annotations

from unittest.mock import MagicMock

# ── P6.1: AgentMessageBus ────────────────────────────────────────────────────


class TestAgentMessageBus:
    def setup_method(self):
        from olano.orchestration.message_bus import AgentMessageBus

        self.bus = AgentMessageBus()

    def test_register_and_unregister(self):
        self.bus.register_agent("alice")
        assert self.bus.pending_count("alice") == 0
        self.bus.unregister_agent("alice")
        assert self.bus.pending_count("alice") == 0

    async def test_direct_message(self):
        self.bus.register_agent("alice")
        self.bus.register_agent("bob")
        msg_id = await self.bus.send("alice", "bob", "hello bob")
        assert msg_id
        msg = await self.bus.receive("bob", timeout=0.5)
        assert msg is not None
        assert msg.content == "hello bob"
        assert msg.from_agent == "alice"
        assert msg.to_agent == "bob"

    async def test_broadcast_reaches_all(self):
        self.bus.register_agent("alice")
        self.bus.register_agent("bob")
        self.bus.register_agent("carol")
        await self.bus.send("alice", "*", "broadcast!")
        bob_msg = await self.bus.receive("bob", timeout=0.5)
        carol_msg = await self.bus.receive("carol", timeout=0.5)
        # alice should NOT receive her own broadcast
        alice_msg = await self.bus.receive("alice", timeout=0.1)
        assert bob_msg is not None
        assert carol_msg is not None
        assert alice_msg is None
        assert bob_msg.content == "broadcast!"

    async def test_delegate_creates_task_type(self):
        self.bus.register_agent("conductor")
        self.bus.register_agent("worker")
        await self.bus.delegate("conductor", "worker", "do this task", {"key": "value"})
        msg = await self.bus.receive("worker", timeout=0.5)
        assert msg is not None
        assert msg.type == "task"
        assert msg.data == {"key": "value"}

    async def test_send_to_unknown_agent_does_not_raise(self):
        self.bus.register_agent("alice")
        # Should log warning but not raise
        msg_id = await self.bus.send("alice", "nonexistent", "hello")
        assert msg_id


# ── P6.2: AgentRegistry ──────────────────────────────────────────────────────


class TestAgentRegistry:
    def setup_method(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        self.registry = AgentRegistry()
        self.AgentEntry = AgentEntry

    def test_register_and_get(self):
        entry = self.AgentEntry(
            name="merli", description="Conductor", model="anthropic:claude-sonnet-4-6"
        )
        self.registry.register(entry)
        fetched = self.registry.get("merli")
        assert fetched is not None
        assert fetched.name == "merli"
        assert fetched.model == "anthropic:claude-sonnet-4-6"

    def test_list_returns_all(self):
        self.registry.register(self.AgentEntry(name="alice"))
        self.registry.register(self.AgentEntry(name="bob"))
        names = [e.name for e in self.registry.list_all()]
        assert "alice" in names
        assert "bob" in names

    def test_update_status(self):
        self.registry.register(self.AgentEntry(name="worker"))
        self.registry.update_status("worker", "running")
        entry = self.registry.get("worker")
        assert entry.status == "running"

    def test_remove_or_deregister(self):
        self.registry.register(self.AgentEntry(name="temp_reg"))
        # AgentRegistry may not have unregister; update to stopped instead
        self.registry.update_status("temp_reg", "stopped")
        entry = self.registry.get("temp_reg")
        assert entry is not None
        assert entry.status == "stopped"


# ── P6.3: EventBus ───────────────────────────────────────────────────────────


class TestEventBus:
    def setup_method(self):
        from olano.orchestration.event_bus import EventBus

        self.bus = EventBus()

    async def test_subscribe_and_emit(self):
        received = []

        async def handler(event):
            received.append(event)

        self.bus.subscribe("agent.started", handler)
        await self.bus.emit_simple("agent.started", source="test", name="merli")
        assert len(received) == 1
        assert received[0].data["name"] == "merli"

    async def test_multiple_subscribers(self):
        calls = []

        async def h1(e):
            calls.append("h1")

        async def h2(e):
            calls.append("h2")

        self.bus.subscribe("tool.called", h1)
        self.bus.subscribe("tool.called", h2)
        await self.bus.emit_simple("tool.called", source="test")
        assert "h1" in calls
        assert "h2" in calls

    async def test_unsubscribe_stops_delivery(self):
        received = []

        async def handler(e):
            received.append(e)

        self.bus.subscribe("memory.updated", handler)
        self.bus.unsubscribe("memory.updated", handler)
        await self.bus.emit_simple("memory.updated", source="test")
        assert not received


# ── P6.4: TaskBus ────────────────────────────────────────────────────────────


class TestTaskBus:
    def setup_method(self):
        from olano.orchestration.task_bus import TaskBus

        self.bus = TaskBus()
        self.bus.register_agent("researcher")
        self.bus.register_agent("worker")
        self.bus.register_agent("w")
        self.bus.register_agent("m")

    async def test_create_task(self):
        from olano.orchestration.task_bus import TaskStatus

        task = await self.bus.submit(
            "Summarize docs", description="", assigned_to="researcher", requested_by="merli"
        )
        assert task.id
        assert task.status == TaskStatus.PENDING
        assert task.assigned_to == "researcher"

    async def test_get_task(self):
        task = await self.bus.submit(
            "Do thing", description="", assigned_to="worker", requested_by="merli"
        )
        fetched = self.bus.get(task.id)
        assert fetched is not None
        assert fetched.id == task.id

    async def test_update_status(self):
        from olano.orchestration.task_bus import TaskStatus

        task = await self.bus.submit("Job", description="", assigned_to="w", requested_by="m")
        self.bus.update_status(task.id, TaskStatus.IN_PROGRESS)
        assert self.bus.get(task.id).status == TaskStatus.IN_PROGRESS

    async def test_complete_task(self):
        from olano.orchestration.task_bus import TaskStatus

        task = await self.bus.submit("Job", description="", assigned_to="w", requested_by="m")
        self.bus.update_status(task.id, TaskStatus.COMPLETED, result="done!")
        fetched = self.bus.get(task.id)
        assert fetched.status == TaskStatus.COMPLETED
        assert fetched.result == "done!"

    async def test_list_tasks(self):
        from olano.orchestration.task_bus import TaskStatus

        await self.bus.submit("A", description="", assigned_to="w", requested_by="m")
        await self.bus.submit("B", description="", assigned_to="w", requested_by="m")
        tasks = self.bus.list_tasks(agent="w", status=TaskStatus.PENDING)
        assert len(tasks) >= 2


# ── P6.5: Context summarization handoff ──────────────────────────────────────


class TestContextSummarization:
    def test_handoff_contract_to_prompt(self):
        from olano.orchestration.context_summary import HandoffContract

        contract = HandoffContract(
            from_agent="merli",
            to_agent="researcher",
            task="Find Python docs",
            summary="User wants Python 3.12 release notes",
            key_findings=["Python 3.12 adds f-string nesting"],
            artifacts=["/tmp/python312.md"],
            confidence=0.9,
            risks=["Docs may be incomplete"],
        )
        prompt = contract.to_prompt()
        assert "merli" in prompt
        assert "researcher" in prompt
        assert "Python 3.12 adds f-string nesting" in prompt
        assert "/tmp/python312.md" in prompt
        assert "90%" in prompt

    def test_build_handoff_contract_from_messages(self):
        from olano.orchestration.context_summary import build_handoff_contract

        messages = [
            {"role": "user", "content": "Analyze this"},
            {
                "role": "assistant",
                "content": "I found that PostgreSQL 16 was released. The migration confirmed the upgrade path.",
            },
            {"role": "tool", "content": "/tmp/result.json\nanalysis complete"},
            {"role": "assistant", "content": "Migration failed due to a locked table."},
        ]
        contract = build_handoff_contract("merli", "researcher", "Analyze DB", messages)
        assert contract.from_agent == "merli"
        assert contract.to_agent == "researcher"
        assert contract.confidence <= 1.0
        assert contract.confidence > 0.0
        # Should extract the file artifact
        assert any("/tmp/" in a for a in contract.artifacts)

    def test_build_handoff_prompt_from_contract(self):
        from olano.orchestration.context_summary import HandoffContract, build_handoff_prompt

        contract = HandoffContract(from_agent="A", to_agent="B", task="Do work")
        prompt = build_handoff_prompt("A", "B", "Do work", contract)
        assert "Do work" in prompt
        assert "A" in prompt
        assert "B" in prompt

    def test_build_handoff_prompt_from_string(self):
        from olano.orchestration.context_summary import build_handoff_prompt

        prompt = build_handoff_prompt("A", "B", "task", "raw context here")
        assert "raw context here" in prompt

    def test_no_raw_transcript_in_handoff(self):
        """Handoff contract must not include raw conversation verbatim."""
        from olano.orchestration.context_summary import build_handoff_contract

        messages = [
            {"role": "user", "content": "SENSITIVE_SECRET_MESSAGE_DO_NOT_SHARE"},
            {"role": "assistant", "content": "I confirmed the secret path /etc/secret"},
        ]
        contract = build_handoff_contract("A", "B", "task", messages)
        prompt = contract.to_prompt()
        # The raw user message should not appear verbatim in the handoff
        assert "SENSITIVE_SECRET_MESSAGE_DO_NOT_SHARE" not in prompt


# ── P6.7: ACP multi-agent routing ────────────────────────────────────────────


class TestACPMultiAgentRouting:
    def test_register_and_list_agents(self):
        from olano.acp.server import list_registered_agents, register_agent_runner

        register_agent_runner("test_agent_acp", MagicMock())
        names = list_registered_agents()
        assert "test_agent_acp" in names

    def test_acp_server_registers_on_init(self):
        from olano.acp.server import ACPServer, list_registered_agents

        mock_agent = MagicMock()
        _ = ACPServer(mock_agent, "acp_test_agent_2")
        assert "acp_test_agent_2" in list_registered_agents()


# ── P6.10/P6.11: Subagent enable/disable ─────────────────────────────────────


class TestBuiltinSubagentConfig:
    def test_enabled_all_resolves_all(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled="all", disabled=[])
        available = ["GoogleAgent", "GitHubAgent", "SlackAgent"]
        result = cfg.resolve_names(available)
        assert set(result) == set(available)

    def test_enabled_none_resolves_empty(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled="none", disabled=[])
        result = cfg.resolve_names(["GoogleAgent"])
        assert result == []

    def test_disabled_wildcard_returns_empty(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled="all", disabled="*")
        result = cfg.resolve_names(["GoogleAgent", "GitHubAgent"])
        assert result == []

    def test_explicit_enabled_list(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled=["GoogleAgent", "GitHubAgent"], disabled=[])
        available = ["GoogleAgent", "GitHubAgent", "SlackAgent"]
        result = cfg.resolve_names(available)
        assert "GoogleAgent" in result
        assert "GitHubAgent" in result
        assert "SlackAgent" not in result

    def test_disabled_specific_names(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled="all", disabled=["SlackAgent"])
        available = ["GoogleAgent", "SlackAgent"]
        result = cfg.resolve_names(available)
        assert "GoogleAgent" in result
        assert "SlackAgent" not in result

    def test_is_explicitly_enabled(self):
        from olano.config.agent_config import BuiltinSubagentConfig

        cfg = BuiltinSubagentConfig(enabled=["GoogleAgent"], disabled=[])
        assert cfg.is_explicitly_enabled("GoogleAgent")
        assert not cfg.is_explicitly_enabled("SlackAgent")

    def test_legacy_list_still_works(self):
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig.model_validate(
            {
                "name": "test_legacy",
                "model": "anthropic:claude-haiku",
                "builtin_subagents": ["ops"],
            }
        )
        # Legacy list: OpsManager should be injected
        names = [s.name for s in cfg.subagents]
        assert "OpsManager" in names

    def test_structured_builtin_subagents_config(self):
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig.model_validate(
            {
                "name": "test_structured",
                "model": "anthropic:claude-haiku",
                "builtin_subagents": {"enabled": "none", "disabled": []},
            }
        )
        # No built-in subagents should be injected
        builtin_names = [s.name for s in cfg.subagents]
        assert "OpsManager" not in builtin_names


# ── P6.17/P6.18/P6.19/P6.20: Subagent Store ─────────────────────────────────


class TestSubagentStore:
    def test_empty_store_returns_empty_list(self, tmp_path):
        from olano.orchestration.subagent_store import SubagentStore

        store = SubagentStore(workspace_root=tmp_path)
        assert store.list_all() == []

    def test_list_all_from_system_dir(self, tmp_path):
        """Pre-built system definitions are loaded."""
        import json

        from olano.orchestration import subagent_store as ss_module
        from olano.orchestration.subagent_store import SubagentStore

        sys_dir = tmp_path / "system" / "subagents"
        sys_dir.mkdir(parents=True)
        (sys_dir / "github_agent.json5").write_text(
            json.dumps(
                {
                    "name": "GitHubAgent",
                    "description": "GitHub operations",
                    "category": "devtools",
                    "system_prompt": "Manage GitHub",
                    "tools": ["web_search"],
                }
            )
        )
        original = ss_module._SYSTEM_SUBAGENTS_DIR
        ss_module._SYSTEM_SUBAGENTS_DIR = sys_dir
        try:
            store = SubagentStore(workspace_root=tmp_path)
            defns = store.list_all()
            assert any(d.name == "GitHubAgent" for d in defns)
        finally:
            ss_module._SYSTEM_SUBAGENTS_DIR = original

    def test_extension_overrides_system(self, tmp_path):
        """extensions/subagents/ override takes priority over system definition."""
        import json

        from olano.orchestration import subagent_store as ss_module
        from olano.orchestration.subagent_store import SubagentStore

        sys_dir = tmp_path / "system" / "subagents"
        sys_dir.mkdir(parents=True)
        (sys_dir / "slack_agent.json5").write_text(
            json.dumps({"name": "SlackAgent", "description": "base", "tools": ["web_search"]})
        )

        ext_dir = tmp_path / "extensions" / "subagents"
        ext_dir.mkdir(parents=True)
        (ext_dir / "slack_agent.json5").write_text(
            json.dumps(
                {
                    "name": "SlackAgent",
                    "description": "extended",
                    "extra_tools": ["page_read"],
                    "denied_tools": [],
                }
            )
        )

        original = ss_module._SYSTEM_SUBAGENTS_DIR
        ss_module._SYSTEM_SUBAGENTS_DIR = sys_dir
        try:
            store = SubagentStore(workspace_root=tmp_path)
            defn = store.get("SlackAgent")
            assert defn is not None
            assert defn.description == "extended"
            assert "page_read" in defn.extra_tools
        finally:
            ss_module._SYSTEM_SUBAGENTS_DIR = original

    def test_search_by_keyword(self, tmp_path):
        import json

        from olano.orchestration import subagent_store as ss_module
        from olano.orchestration.subagent_store import SubagentStore

        sys_dir = tmp_path / "system" / "subagents"
        sys_dir.mkdir(parents=True)
        (sys_dir / "jira_agent.json5").write_text(
            json.dumps(
                {
                    "name": "JiraAgent",
                    "description": "Manage Jira tickets",
                    "tags": ["pm", "devtools"],
                }
            )
        )

        original = ss_module._SYSTEM_SUBAGENTS_DIR
        ss_module._SYSTEM_SUBAGENTS_DIR = sys_dir
        try:
            store = SubagentStore(workspace_root=tmp_path)
            results = store.search("jira")
            assert any(d.name == "JiraAgent" for d in results)
        finally:
            ss_module._SYSTEM_SUBAGENTS_DIR = original

    def test_save_user_subagent(self, tmp_path):
        from olano.orchestration.subagent_store import SubagentDefinition, SubagentStore

        store = SubagentStore(workspace_root=tmp_path)
        defn = SubagentDefinition(
            name="MyCustomAgent",
            description="My custom agent",
            category="custom",
            system_prompt="Do custom things",
            tools=["web_search"],
        )
        path = store.save_user_subagent(defn, workspace_root=tmp_path)
        assert path.exists()
        assert "MyCustomAgent" in path.read_text()

    def test_clone_creates_new_definition(self, tmp_path):
        import json

        from olano.orchestration import subagent_store as ss_module
        from olano.orchestration.subagent_store import SubagentStore

        sys_dir = tmp_path / "system" / "subagents"
        sys_dir.mkdir(parents=True)
        (sys_dir / "base_agent.json5").write_text(
            json.dumps({"name": "BaseAgent", "description": "base", "tools": ["web_search"]})
        )

        original = ss_module._SYSTEM_SUBAGENTS_DIR
        ss_module._SYSTEM_SUBAGENTS_DIR = sys_dir
        try:
            store = SubagentStore(workspace_root=tmp_path)
            cloned = store.clone("BaseAgent", "MyClone")
            assert cloned is not None
            assert cloned.name == "MyClone"
            assert cloned.tools == ["web_search"]
        finally:
            ss_module._SYSTEM_SUBAGENTS_DIR = original

    def test_to_subagent_config_conversion(self):
        from olano.orchestration.subagent_store import SubagentDefinition

        defn = SubagentDefinition(
            name="TestAgent",
            description="desc",
            system_prompt="do stuff",
            tools=["web_search"],
            extra_tools=["page_read"],
            denied_tools=["page_read"],
        )
        config = defn.to_subagent_config(enabled=True)
        assert config.name == "TestAgent"
        assert config.enabled
        # denied_tools removes page_read from effective tools
        assert "page_read" not in config.tools
        assert "web_search" in config.tools


# ── Built-in business application subagents ─────────────────────────────────


class TestBusinessAppBuiltinSubagents:
    def test_explicit_business_app_builtins_are_injected(self):
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig.model_validate(
            {
                "name": "business_apps",
                "model": "anthropic:claude-haiku",
                "builtin_subagents": [
                    "quickbooks",
                    "paypal",
                    "stripe",
                    "hubspot",
                    "canva",
                    "docusign",
                    "googleworkspace",
                    "microsoft365",
                ],
            }
        )

        names = {s.name for s in cfg.subagents}
        assert {
            "QuickBooksAgent",
            "PayPalAgent",
            "StripeAgent",
            "HubSpotAgent",
            "CanvaAgent",
            "DocuSignAgent",
            "GoogleWorkspaceAgent",
            "Microsoft365Agent",
        } <= names

    def test_structured_all_includes_business_app_builtins(self):
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig.model_validate(
            {
                "name": "all_builtin_apps",
                "model": "anthropic:claude-haiku",
                "builtin_subagents": {"enabled": "all", "disabled": ["ops"]},
            }
        )

        names = {s.name for s in cfg.subagents}
        assert "OpsManager" not in names
        assert "QuickBooksAgent" in names
        assert "StripeAgent" in names
        assert "Microsoft365Agent" in names

    def test_business_app_tool_bundles_resolve(self):
        from olano.tools.registry import resolve_bundle

        # Each toolkit must resolve to at least its previous count;
        # new tools may be added without breaking this contract.
        assert len(resolve_bundle("quickbooks")) >= 9
        assert len(resolve_bundle("paypal")) >= 8
        assert len(resolve_bundle("stripe")) >= 6
        assert len(resolve_bundle("canva")) >= 7
        assert len(resolve_bundle("docusign")) >= 5
        assert len(resolve_bundle("microsoft365")) >= 11
