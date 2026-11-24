"""Tests for P5.9: Knowledge Vault with [[wikilinks]], and P5.10: Hybrid search."""

from __future__ import annotations

import pytest


@pytest.fixture
def vault(tmp_path):
    from olano.memory.knowledge_vault import KnowledgeVault

    return KnowledgeVault(vault_dir=tmp_path / "vault")


class TestWikilinkParsing:
    def test_simple_wikilink(self):
        from olano.memory.knowledge_vault import KnowledgeVault

        links = KnowledgeVault.parse_wikilinks("See [[Kubernetes]] for details.")
        assert links == ["Kubernetes"]

    def test_multiple_wikilinks(self):
        from olano.memory.knowledge_vault import KnowledgeVault

        links = KnowledgeVault.parse_wikilinks(
            "[[Docker]] is used with [[Kubernetes]] and [[Helm]]."
        )
        assert set(links) == {"Docker", "Kubernetes", "Helm"}

    def test_no_wikilinks(self):
        from olano.memory.knowledge_vault import KnowledgeVault

        assert KnowledgeVault.parse_wikilinks("No links here.") == []

    def test_wikilink_deduplication(self):
        from olano.memory.knowledge_vault import KnowledgeVault

        links = KnowledgeVault.parse_wikilinks("[[Python]] is great, see [[Python]] docs.")
        assert links.count("Python") == 1

    def test_wikilink_with_alias(self):
        from olano.memory.knowledge_vault import KnowledgeVault

        # [[target|display text]] — target is captured, display text ignored
        links = KnowledgeVault.parse_wikilinks("See [[Kubernetes|k8s]] for more.")
        assert links == ["Kubernetes"]


class TestVaultCRUD:
    def test_upsert_and_get(self, vault, tmp_path):
        from olano.memory.knowledge_vault import VaultEntry

        entry = VaultEntry(title="Python", content="Python is a programming language.")
        vault.upsert(entry)

        retrieved = vault.get("Python")
        assert retrieved is not None
        assert retrieved.title == "Python"
        assert "programming language" in retrieved.content

    def test_entry_file_on_disk(self, vault, tmp_path):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Redis", content="An in-memory data store."))
        assert (tmp_path / "vault" / "Redis.md").exists()

    def test_get_nonexistent_returns_none(self, vault):
        assert vault.get("NonExistent") is None

    def test_entry_count(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        assert vault.entry_count() == 0
        vault.upsert(VaultEntry(title="A", content="Content A"))
        vault.upsert(VaultEntry(title="B", content="Content B"))
        assert vault.entry_count() == 2

    def test_delete_entry(self, vault, tmp_path):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Temp", content="Temp content."))
        assert vault.get("Temp") is not None
        vault.delete("Temp")
        assert vault.get("Temp") is None
        assert not (tmp_path / "vault" / "Temp.md").exists()

    def test_list_all(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Alpha", content="..."))
        vault.upsert(VaultEntry(title="Beta", content="..."))
        titles = vault.list_all()
        assert "Alpha" in titles
        assert "Beta" in titles

    def test_upsert_updates_existing(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Go", content="Original content."))
        vault.upsert(VaultEntry(title="Go", content="Updated content."))

        retrieved = vault.get("Go")
        assert retrieved is not None
        assert "Updated" in retrieved.content
        assert vault.entry_count() == 1


class TestWikilinkGraph:
    def test_outlinks_populated(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Docker", content="Works with [[Kubernetes]] and [[Helm]]."))
        outlinks = vault.get_outlinks("Docker")
        assert "Kubernetes" in outlinks
        assert "Helm" in outlinks

    def test_backlinks_populated(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Kubernetes", content="Container orchestration."))
        vault.upsert(VaultEntry(title="Docker", content="Works with [[Kubernetes]]."))
        vault.upsert(VaultEntry(title="Helm", content="Package manager for [[Kubernetes]]."))

        backlinks = vault.get_backlinks("Kubernetes")
        assert "Docker" in backlinks
        assert "Helm" in backlinks

    def test_no_self_links(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Python", content="[[Python]] docs are at python.org."))
        assert "Python" not in vault.get_outlinks("Python")

    def test_link_count(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="A", content="See [[B]] and [[C]]."))
        assert vault.link_count() == 2

    def test_bidirectional_links(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="X", content="See [[Y]]."))
        vault.upsert(VaultEntry(title="Y", content="See [[X]]."))

        assert "Y" in vault.get_outlinks("X")
        assert "X" in vault.get_backlinks("Y")
        assert "X" in vault.get_outlinks("Y")
        assert "Y" in vault.get_backlinks("X")


class TestVaultFTSSearch:
    def test_search_finds_matching_entry(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="PostgreSQL", content="A relational database system."))
        vault.upsert(VaultEntry(title="Redis", content="An in-memory data store."))

        results = vault.search("relational database")
        titles = [e.title for e in results]
        assert "PostgreSQL" in titles

    def test_search_returns_empty_on_no_match(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Go", content="A compiled language."))
        results = vault.search("unicorn")
        assert results == []

    def test_search_with_dotted_term_does_not_raise(self, vault):
        """Regression: a query with '.' must not raise fts5 'syntax error near "."'."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="OpenJobs", content="Notes about openjobs.bot project."))

        # Previously raised sqlite3.OperationalError: fts5: syntax error near "."
        results = vault.search("openjobs.bot")
        assert "OpenJobs" in [e.title for e in results]

    def test_search_with_fts_syntax_chars_does_not_raise(self, vault):
        """Punctuation FTS5 treats as operators must be matched literally, not parsed."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Edge", content="A note about c++ and key-value stores."))

        # None of these should raise; each is a valid, safe MATCH expression.
        for query in ["c++", "key-value", "a:b", "(foo", "bar)", "who? what!", "* ^ -"]:
            assert vault.search(query) == vault.search(query)  # second call must not raise

    def test_search_pure_punctuation_returns_empty(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Anything", content="Some content."))
        assert vault.search("...") == []
        assert vault.search("   ") == []
        assert vault.search("") == []

    def test_search_preserves_boolean_or_operator(self, vault):
        """Uppercase OR stays a boolean operator (used by get_relevant_entries)."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Python", content="Variables and loops."))
        vault.upsert(VaultEntry(title="Rust", content="Ownership and borrowing."))

        titles = [e.title for e in vault.search("variables OR ownership")]
        assert "Python" in titles
        assert "Rust" in titles

    def test_search_with_quote_in_query_does_not_raise(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Quoted", content="It is a quote test."))
        # Embedded double quote must be escaped, not break out of the FTS string.
        assert vault.search('say "hello') == vault.search('say "hello')


class TestVaultMarkdownRoundTrip:
    def test_frontmatter_preserved(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        entry = VaultEntry(
            title="Kafka",
            content="A distributed event streaming platform.",
            tags=["streaming", "messaging"],
            aliases=["Apache Kafka"],
            sources=["https://kafka.apache.org"],
        )
        vault.upsert(entry)

        retrieved = vault.get("Kafka")
        assert retrieved is not None
        assert "streaming" in retrieved.tags
        assert "Apache Kafka" in retrieved.aliases

    def test_from_markdown_parses_frontmatter(self):
        from olano.memory.knowledge_vault import VaultEntry

        md = """---
title: Test
tags: ["a", "b"]
aliases: ["Test alias"]
created: 2024-01-01T00:00:00+00:00
updated: 2024-01-02T00:00:00+00:00
auto_inject: true
---

Content here.
"""
        entry = VaultEntry.from_markdown(md)
        assert entry.title == "Test"
        assert "a" in entry.tags
        assert "Test alias" in entry.aliases
        assert entry.auto_inject is True
        assert "Content here" in entry.content


class TestVaultDiskSync:
    def test_sync_from_disk_imports_existing_files(self, tmp_path):
        from olano.memory.knowledge_vault import KnowledgeVault, VaultEntry

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()

        # Write a markdown file before creating the vault
        entry = VaultEntry(title="Preexisting", content="Already on disk.")
        (vault_dir / "Preexisting.md").write_text(entry.to_markdown(), encoding="utf-8")

        # Now create vault — should auto-import
        vault = KnowledgeVault(vault_dir=vault_dir)
        assert vault.get("Preexisting") is not None

    def test_sync_file_updates_index(self, vault, tmp_path):
        from olano.memory.knowledge_vault import VaultEntry

        entry = VaultEntry(title="Dynamic", content="Original.")
        vault.upsert(entry)

        # Simulate file edit
        path = tmp_path / "vault" / "Dynamic.md"
        updated = VaultEntry(title="Dynamic", content="Updated on disk.")
        path.write_text(updated.to_markdown(), encoding="utf-8")

        vault.sync_file(path)
        retrieved = vault.get("Dynamic")
        assert retrieved is not None
        assert "Updated on disk" in retrieved.content


class TestAutoInject:
    def test_auto_inject_entries_returned(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Core Concept", content="Important.", auto_inject=True))
        vault.upsert(VaultEntry(title="Other", content="Less important.", auto_inject=False))

        results = vault.get_auto_inject_entries()
        titles = [e.title for e in results]
        assert "Core Concept" in titles
        assert "Other" not in titles

    def test_get_relevant_entries_by_context(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Python Basics", content="Variables, loops, functions."))
        vault.upsert(VaultEntry(title="Go Basics", content="Goroutines and channels."))

        results = vault.get_relevant_entries("python variables loops")
        titles = [e.title for e in results]
        assert "Python Basics" in titles


# ──────────────── P5.10: Hybrid search and query routing ──────────────────────


class TestHybridSearch:
    def test_hybrid_search_returns_results(self, vault):
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="PostgreSQL", content="A relational database system."))
        vault.upsert(VaultEntry(title="Redis", content="An in-memory key-value store."))

        results = vault.hybrid_search("relational database")
        assert len(results) > 0
        titles = [e.title for e in results]
        assert "PostgreSQL" in titles

    def test_hybrid_search_fts_fallback_when_no_vector(self, vault):
        """hybrid_search should return FTS results even when Chroma/vector is empty."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Kafka", content="Distributed event streaming."))

        results = vault.hybrid_search("streaming")
        assert len(results) > 0

    def test_hybrid_search_deduplicates_results(self, vault):
        """A title appearing in both FTS and vector results should appear once."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Python", content="A versatile programming language."))
        results = vault.hybrid_search("python programming", limit=5)
        titles = [e.title for e in results]
        assert titles.count("Python") == 1

    def test_rrf_scores_combine_ranks(self, vault):
        """An entry that ranks well in both FTS and vector should score higher."""
        from olano.memory.knowledge_vault import VaultEntry

        vault.upsert(VaultEntry(title="Alpha", content="Unique keyword alphazeta."))
        vault.upsert(VaultEntry(title="Beta", content="Common text about databases."))
        vault.upsert(VaultEntry(title="Gamma", content="Databases are common alphazeta."))

        # Gamma matches both the unique term and the common topic
        results = vault.hybrid_search("alphazeta databases", limit=5)
        assert len(results) > 0


class TestQueryRouting:
    def test_relationship_query_routes_to_graph(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("Who works with Alice on this project?") == "graph"
        assert MemoryRouter.route_query("What is the relationship between A and B?") == "graph"
        assert MemoryRouter.route_query("Which modules depend on auth?") == "graph"

    def test_similarity_query_routes_to_rag(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("What is similar to PostgreSQL?") == "rag"
        assert MemoryRouter.route_query("Compare Redis vs Memcached") == "rag"
        assert MemoryRouter.route_query("What's an alternative to Docker?") == "rag"

    def test_concept_query_routes_to_notes(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("What is Kubernetes?") == "notes"
        assert MemoryRouter.route_query("Tell me about Python") == "notes"
        assert MemoryRouter.route_query("Explain the auth flow") == "notes"
