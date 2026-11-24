"""Tests for olano.memory.typed_memory and memory_graph."""

from __future__ import annotations

import pytest

# ─────────────────────────────────── TypedFact ───────────────────────────────


class TestTypedFact:
    def test_relevance_score_fresh(self):
        from olano.memory.typed_memory import TypedFact

        fact = TypedFact(
            id="test1",
            type="semantic",
            content="The user prefers dark mode",
            confidence=1.0,
        )
        # Fresh fact should have relevance close to 1.0
        assert fact.relevance_score >= 0.9

    def test_relevance_score_decays_with_age(self):
        from datetime import datetime, timedelta

        from olano.memory.typed_memory import TypedFact

        fact = TypedFact(
            id="test2",
            type="semantic",
            content="Old fact",
            confidence=1.0,
            last_accessed=datetime.utcnow() - timedelta(days=10),
        )
        # Should have decayed
        assert fact.relevance_score < 1.0
        assert fact.relevance_score >= 0.1  # floor

    def test_relevance_score_scales_with_confidence(self):
        from olano.memory.typed_memory import TypedFact

        # Higher confidence → higher relevance score
        low_conf = TypedFact(id="low", type="semantic", content="X", confidence=0.3)
        high_conf = TypedFact(id="high", type="semantic", content="Y", confidence=0.9)
        assert high_conf.relevance_score > low_conf.relevance_score

    def test_to_dict_and_from_dict(self):
        from olano.memory.typed_memory import TypedFact

        fact = TypedFact(
            id="abc",
            type="preference",
            content="Prefers Python over Ruby",
            confidence=0.8,
            source="conversation",
            tags=["language", "preference"],
        )
        d = fact.to_dict()
        restored = TypedFact.from_dict(d)
        assert restored.id == fact.id
        assert restored.content == fact.content
        assert restored.confidence == fact.confidence
        assert restored.tags == fact.tags


# ─────────────────────────────────── TypedMemoryStore ────────────────────────


class TestTypedMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        from olano.memory.typed_memory import TypedMemoryStore

        return TypedMemoryStore(state_memory_dir=tmp_path / "state" / "memory")

    def test_store_and_retrieve(self, store):
        fact = store.store_fact(
            content="The user likes minimal interfaces",
            memory_type="preference",
            confidence=0.9,
            source="session",
        )
        results = store.search("minimal", memory_type="preference")
        assert len(results) >= 1
        assert any(f.id == fact.id for f in results)

    def test_search_all_types(self, store):
        store.store_fact("Python is preferred", "semantic")
        store.store_fact("Do not delete prod data", "safety")
        results = store.search("preferred")
        assert any("Python" in f.content for f in results)

    def test_exact_duplicate_boosts_confidence(self, store):
        store.store_fact("User loves dark mode", "preference", confidence=0.5)
        store.store_fact("User loves dark mode", "preference", confidence=0.5)
        facts = store.get_all("preference")
        # Still just one fact, but with boosted confidence
        matching = [f for f in facts if "dark mode" in f.content]
        assert len(matching) == 1
        assert matching[0].confidence > 0.5

    def test_contradiction_lowers_confidence(self, store):
        # Store two facts with same 30-char prefix but different content
        import uuid

        from olano.memory.typed_memory import TypedFact

        # prefix must be exactly 30 chars so both facts share the same [:30] slice
        prefix = "The user likes working with Py"  # exactly 30 chars
        fact1 = TypedFact(id=str(uuid.uuid4())[:8], type="preference", content=f"{prefix}thon.")
        fact2 = TypedFact(
            id=str(uuid.uuid4())[:8], type="preference", content=f"{prefix}thon and Go."
        )

        store.store(fact1)
        original_confidence = fact1.confidence
        store.store(fact2)

        # fact1 should have lowered confidence (contradicted)
        facts = store.get_all("preference")
        older = next(f for f in facts if f.id == fact1.id)
        assert older.confidence < original_confidence

    def test_persistence(self, tmp_path):
        from olano.memory.typed_memory import TypedMemoryStore

        mem_dir = tmp_path / "memory"
        store1 = TypedMemoryStore(mem_dir)
        store1.store_fact("Persistent fact", "semantic")

        store2 = TypedMemoryStore(mem_dir)
        results = store2.search("Persistent")
        assert len(results) >= 1

    def test_get_all_with_min_confidence(self, store):
        store.store_fact("High confidence fact", "semantic", confidence=0.9)
        store.store_fact("Low confidence fact", "semantic", confidence=0.1)

        high_only = store.get_all("semantic", min_confidence=0.5)
        contents = [f.content for f in high_only]
        assert "High confidence fact" in contents
        assert "Low confidence fact" not in contents

    def test_search_respects_min_confidence(self, store):
        store.store_fact("This is low conf", "semantic", confidence=0.1)
        results = store.search("low conf", min_confidence=0.5)
        assert len(results) == 0

    def test_tags_searchable(self, store):
        store.store_fact("A tagged fact", "semantic", tags=["important", "infra"])
        results = store.search("infra")
        assert len(results) >= 1


# ─────────────────────────────────── MemoryGraph ─────────────────────────────


class TestMemoryGraph:
    @pytest.fixture
    def graph(self, tmp_path):
        from olano.memory.memory_graph import MemoryGraph

        return MemoryGraph(db_path=tmp_path / "graph.db")

    def test_upsert_entity(self, graph):
        entity_id = graph.upsert_entity("Alice", "person", description="A developer")
        assert entity_id

    def test_upsert_entity_idempotent(self, graph):
        id1 = graph.upsert_entity("Bob", "person")
        id2 = graph.upsert_entity("Bob", "person", description="Updated")
        assert id1 == id2
        assert graph.entity_count() == 1

    def test_get_entity(self, graph):
        eid = graph.upsert_entity("Project X", "project", description="Main project")
        entity = graph.get_entity(eid)
        assert entity is not None
        assert entity.name == "Project X"
        assert entity.type == "project"

    def test_find_entities_by_name(self, graph):
        graph.upsert_entity("Alpha", "concept")
        graph.upsert_entity("Beta", "concept")
        results = graph.find_entities(name="Alpha")
        assert len(results) == 1
        assert results[0].name == "Alpha"

    def test_find_entities_by_type(self, graph):
        graph.upsert_entity("Claude", "tool")
        graph.upsert_entity("VSCode", "tool")
        graph.upsert_entity("OpenAI", "org")
        tools = graph.find_entities(entity_type="tool")
        assert len(tools) == 2

    def test_add_relationship(self, graph):
        alice_id = graph.upsert_entity("Alice", "person")
        project_id = graph.upsert_entity("Project X", "project")
        rel_id = graph.add_relationship(alice_id, project_id, "works_on")
        assert rel_id

    def test_relationship_dedup(self, graph):
        a = graph.upsert_entity("A", "concept")
        b = graph.upsert_entity("B", "concept")
        rel1 = graph.add_relationship(a, b, "related_to")
        rel2 = graph.add_relationship(a, b, "related_to")
        assert rel1 == rel2
        assert graph.relationship_count() == 1

    def test_get_neighbors_outgoing(self, graph):
        dev_id = graph.upsert_entity("Dev", "person")
        tool_id = graph.upsert_entity("Tool", "tool")
        graph.add_relationship(dev_id, tool_id, "uses")

        neighbors = graph.get_neighbors(dev_id, direction="out")
        assert len(neighbors) == 1
        assert neighbors[0]["relation"] == "uses"
        assert neighbors[0]["entity"].name == "Tool"

    def test_get_neighbors_incoming(self, graph):
        dev_id = graph.upsert_entity("Dev", "person")
        tool_id = graph.upsert_entity("Tool", "tool")
        graph.add_relationship(dev_id, tool_id, "uses")

        neighbors = graph.get_neighbors(tool_id, direction="in")
        assert len(neighbors) == 1
        assert neighbors[0]["entity"].name == "Dev"

    def test_get_neighbors_filtered_by_relation(self, graph):
        a = graph.upsert_entity("A", "concept")
        b = graph.upsert_entity("B", "concept")
        c = graph.upsert_entity("C", "concept")
        graph.add_relationship(a, b, "knows")
        graph.add_relationship(a, c, "uses")

        knows = graph.get_neighbors(a, relation="knows", direction="out")
        assert len(knows) == 1
        assert knows[0]["entity"].name == "B"

    def test_entity_count(self, graph):
        graph.upsert_entity("X", "concept")
        graph.upsert_entity("Y", "concept")
        assert graph.entity_count() == 2

    def test_relationship_count(self, graph):
        a = graph.upsert_entity("A", "concept")
        b = graph.upsert_entity("B", "concept")
        graph.add_relationship(a, b, "rel1")
        graph.add_relationship(a, b, "rel2")  # different relation, new row
        assert graph.relationship_count() == 2
