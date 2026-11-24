"""Phase 5 memory tests — context-aware loading, privacy enforcement, workspace files."""

from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path):
    from olano.memory.typed_memory import TypedMemoryStore

    return TypedMemoryStore(state_memory_dir=tmp_path / "state" / "memory")


@pytest.fixture
def router(store):
    from olano.memory.memory_router import MemoryRouter

    return MemoryRouter(store)


# ─────────────────────── P5.2: context-aware privacy ─────────────────────────


class TestPrivacyEnforcement:
    def test_private_semantic_excluded_from_group_chat(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Public project info", "semantic", private=False)
        store.store_fact("Private personal detail", "semantic", private=True)

        result = router.load_for_situation(SituationType.GROUP_CHAT)
        facts = result.get("semantic", [])
        contents = [f.content for f in facts]

        assert "Public project info" in contents
        assert "Private personal detail" not in contents

    def test_private_semantic_visible_in_main_session(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Private personal detail", "semantic", private=True)

        result = router.load_for_situation(SituationType.MAIN_SESSION)
        facts = result.get("semantic", [])
        contents = [f.content for f in facts]

        assert "Private personal detail" in contents

    def test_preference_never_in_group_chat(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("User prefers dark mode", "preference")

        result = router.load_for_situation(SituationType.GROUP_CHAT)
        assert "preference" not in result

    def test_preference_in_main_session(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("User prefers dark mode", "preference")

        result = router.load_for_situation(SituationType.MAIN_SESSION)
        facts = result.get("preference", [])
        assert any("dark mode" in f.content for f in facts)

    def test_safety_always_included_in_group_chat(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Never delete production data", "safety")

        result = router.load_for_situation(SituationType.GROUP_CHAT)
        assert "safety" in result
        contents = [f.content for f in result["safety"]]
        assert "Never delete production data" in contents

    def test_safety_always_included_in_heartbeat(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Never delete production data", "safety")

        result = router.load_for_situation(SituationType.HEARTBEAT)
        assert "safety" in result

    def test_safety_always_included_in_subagent(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Rate limit to 10 API calls/min", "safety")

        result = router.load_for_situation(SituationType.SUBAGENT)
        assert "safety" in result

    def test_private_fact_excluded_from_subagent(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("Confidential personal note", "semantic", private=True)
        store.store_fact("Public API documentation", "semantic", private=False)

        result = router.load_for_situation(SituationType.SUBAGENT)
        facts = result.get("semantic", [])
        contents = [f.content for f in facts]

        assert "Public API documentation" in contents
        assert "Confidential personal note" not in contents

    def test_private_fact_excluded_from_heartbeat(self, store, router):
        from olano.memory.memory_router import SituationType

        store.store_fact("How to run the daily report", "procedural", private=True)
        store.store_fact("Use uv to install packages", "procedural", private=False)

        result = router.load_for_situation(SituationType.HEARTBEAT)
        facts = result.get("procedural", [])
        contents = [f.content for f in facts]

        assert "Use uv to install packages" in contents
        assert "How to run the daily report" not in contents

    def test_unknown_situation_defaults_to_main_session_rules(self, store, router):
        store.store_fact("Some preference", "preference")

        result = router.load_for_situation("unknown_situation_xyz")
        # Should fall back to MAIN_SESSION rules which include preference
        assert "preference" in result


# ─────────────────────── TypedFact private field persistence ─────────────────


class TestPrivateFieldPersistence:
    def test_private_flag_round_trips(self, tmp_path):
        from olano.memory.typed_memory import TypedMemoryStore

        store1 = TypedMemoryStore(tmp_path / "mem")
        store1.store_fact("Secret", "semantic", private=True)
        store1.store_fact("Public", "semantic", private=False)

        store2 = TypedMemoryStore(tmp_path / "mem")
        facts = store2.get_all("semantic")
        by_content = {f.content: f for f in facts}

        assert by_content["Secret"].private is True
        assert by_content["Public"].private is False


# ────────────────────── P5.15: workspace markdown files ──────────────────────


class TestWorkspaceMarkdownFiles:
    NEW_FILES = ["TEAM.md", "LEARNINGS.md", "IDEAS.md", "BRAINSTORMING.md", "CAPABILITIES.md"]

    def test_new_files_created_on_init(self, tmp_path):
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        ws.init()

        for fname in self.NEW_FILES:
            assert (tmp_path / fname).exists(), f"{fname} not created by workspace.init()"

    def test_new_files_have_non_empty_content(self, tmp_path):
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        ws.init()

        for fname in self.NEW_FILES:
            content = (tmp_path / fname).read_text(encoding="utf-8")
            assert content.strip(), f"{fname} was created empty"

    def test_init_is_idempotent_for_new_files(self, tmp_path):
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        ws.init()
        (tmp_path / "TEAM.md").write_text("# Team\n\nCustom content.\n", encoding="utf-8")
        ws.init()

        # Second init must not overwrite user content
        content = (tmp_path / "TEAM.md").read_text(encoding="utf-8")
        assert "Custom content" in content

    def test_team_md_readable_via_workspace(self, tmp_path):
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        ws.init()
        (tmp_path / "TEAM.md").write_text("# Team\n\nAlice: Lead dev\n", encoding="utf-8")

        content = ws.read_file("TEAM.md")
        assert "Alice" in content

    def test_ideas_md_readable_via_workspace(self, tmp_path):
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        ws.init()
        (tmp_path / "IDEAS.md").write_text("# Ideas\n\n- Build a CLI tool\n", encoding="utf-8")

        content = ws.read_file("IDEAS.md")
        assert "CLI tool" in content


# ──────────────────── P5.5: contradiction events accumulate ──────────────────


class TestContradictionEvents:
    # Contradiction heuristic: facts share first 30 chars but differ after that.
    _PREFIX = "The user likes working with Py"  # exactly 30 chars

    def test_contradiction_event_accumulated(self):
        import tempfile
        from pathlib import Path

        from olano.memory.typed_memory import TypedMemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TypedMemoryStore(state_memory_dir=Path(tmp) / "mem")
            store.store_fact(f"{self._PREFIX}thon.", "preference")
            store.store_fact(f"{self._PREFIX}thon and Go.", "preference")

            events = store.flush_contradictions()
            assert len(events) >= 1

    def test_flush_clears_pending(self):
        import tempfile
        from pathlib import Path

        from olano.memory.typed_memory import TypedMemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TypedMemoryStore(state_memory_dir=Path(tmp) / "mem")
            store.store_fact(f"{self._PREFIX}thon.", "preference")
            store.store_fact(f"{self._PREFIX}thon and Go.", "preference")

            events_first = store.flush_contradictions()
            events_second = store.flush_contradictions()
            assert len(events_first) >= 1
            assert len(events_second) == 0

    def test_contradiction_event_pulse_content(self):
        from olano.memory.typed_memory import ContradictionEvent, TypedFact

        existing = TypedFact(id="a1", type="semantic", content="X uses dark mode", confidence=0.9)
        incoming = TypedFact(id="a2", type="semantic", content="X uses light mode")
        event = ContradictionEvent("semantic", existing, incoming)

        content = event.as_pulse_content()
        assert "dark mode" in content
        assert "light mode" in content
        assert "semantic" in content


# ──────────────────────── P5.4: Graph + MEMORY.md sync ───────────────────────


class TestGraphMemorySync:
    def _make_service(self, tmp_path):
        """Create a minimal SelfLearningService with a real workspace."""

        from olano.config.agent_config import AgentConfig
        from olano.memory.markdown_memory import MarkdownMemory
        from olano.memory.self_learning import SelfLearningService

        config = AgentConfig(name="test", model="anthropic:claude-haiku-4-5-20251001")
        mem = MarkdownMemory(tmp_path)
        svc = SelfLearningService(
            agent_config=config,
            workspace_root=tmp_path,
            markdown_memory=mem,
            state_dir=tmp_path / ".olano",
        )
        svc._memory_graph = None  # force lazy init
        return svc, mem

    def test_entities_extracted_from_facts(self, tmp_path):
        svc, _ = self._make_service(tmp_path)
        facts = [
            "Python is a programming language used for data science.",
            "Django is a framework for web development.",
        ]
        svc._extract_entities_from_facts(facts)
        graph = svc._get_memory_graph()
        assert graph.entity_count() > 0

    def test_graph_to_memory_md_section_empty_when_no_entities(self, tmp_path):
        from olano.memory.memory_graph import MemoryGraph

        graph = MemoryGraph(db_path=tmp_path / "g.db")
        assert graph.to_memory_md_section() == ""

    def test_graph_to_memory_md_section_has_heading(self, tmp_path):
        from olano.memory.memory_graph import MemoryGraph

        graph = MemoryGraph(db_path=tmp_path / "g.db")
        graph.upsert_entity("Python", "concept", description="A programming language")
        section = graph.to_memory_md_section()
        assert "## Knowledge Graph" in section
        assert "Python" in section

    def test_sync_appends_graph_section_to_memory_md(self, tmp_path):
        svc, mem = self._make_service(tmp_path)
        graph = svc._get_memory_graph()
        graph.upsert_entity("PostgreSQL", "tool", description="Relational database")

        result = svc.sync_graph_to_memory_md()
        assert result is True

        content = mem.read()
        assert "## Knowledge Graph" in content
        assert "PostgreSQL" in content

    def test_sync_preserves_user_sections(self, tmp_path):
        svc, mem = self._make_service(tmp_path)
        # Write a user section first
        mem.append_fact("User likes dark mode", section="User Preferences")
        graph = svc._get_memory_graph()
        graph.upsert_entity("SQLite", "tool")

        svc.sync_graph_to_memory_md()
        content = mem.read()

        assert "User Preferences" in content
        assert "User likes dark mode" in content
        assert "SQLite" in content

    def test_sync_replaces_existing_graph_section(self, tmp_path):
        svc, mem = self._make_service(tmp_path)
        graph = svc._get_memory_graph()
        graph.upsert_entity("Redis", "tool")
        svc.sync_graph_to_memory_md()

        # Add a new entity and re-sync
        graph.upsert_entity("Kafka", "tool")
        svc.sync_graph_to_memory_md()

        content = mem.read()
        # Both entities should appear; the section should not be duplicated
        assert content.count("## Knowledge Graph") == 1
        assert "Kafka" in content

    def test_all_entities_returns_entities_sorted_by_confidence(self, tmp_path):
        from olano.memory.memory_graph import MemoryGraph

        graph = MemoryGraph(db_path=tmp_path / "g.db")
        graph.upsert_entity("LowConf", "concept", confidence=0.3)
        graph.upsert_entity("HighConf", "concept", confidence=0.9)

        entities = graph.all_entities()
        names = [e.name for e in entities]
        assert names.index("HighConf") < names.index("LowConf")
