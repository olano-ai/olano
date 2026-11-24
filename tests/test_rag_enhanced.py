"""Tests for RAG memory with ChromaDB."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def rag(tmp_path):
    from olano.memory.rag_memory import RAGMemory

    return RAGMemory(persist_dir=tmp_path / "rag")


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.similarity_search_with_relevance_scores.return_value = [
        (
            MagicMock(page_content="Python is great", metadata={"source": "test.md", "chunk": 0}),
            0.92,
        ),
    ]
    store.similarity_search.return_value = [
        MagicMock(page_content="Python is great", metadata={"source": "test.md", "chunk": 0}),
    ]
    store.get.return_value = {"ids": ["id1"], "metadatas": [{"source": "test.md"}]}
    return store


# ── Splitter ──────────────────────────────────────────────────────────────────


def test_get_splitter():
    from olano.memory.rag_memory import _get_splitter

    splitter = _get_splitter(chunk_size=500, chunk_overlap=50)
    text = "A" * 1200
    chunks = splitter.split_text(text)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


def test_get_splitter_short():
    from olano.memory.rag_memory import _get_splitter

    splitter = _get_splitter()
    chunks = splitter.split_text("Short text")
    assert chunks == ["Short text"]


# ── Ingestion ─────────────────────────────────────────────────────────────────


def test_ingest_file(tmp_path, rag, mock_store):
    test_file = tmp_path / "notes.md"
    test_file.write_text("# Notes\n\nPython is a programming language.", encoding="utf-8")

    with patch.object(rag, "_get_store", return_value=mock_store):
        n = rag.ingest_file(test_file)

    assert n >= 1
    mock_store.add_documents.assert_called_once()


def test_ingest_file_not_found(rag):
    with pytest.raises(FileNotFoundError):
        rag.ingest_file("/nonexistent/path/file.md")


def test_ingest_file_skip_unchanged(tmp_path, rag, mock_store):
    test_file = tmp_path / "notes.md"
    test_file.write_text("Same content", encoding="utf-8")

    with patch.object(rag, "_get_store", return_value=mock_store):
        n1 = rag.ingest_file(test_file)
        n2 = rag.ingest_file(test_file)

    assert n1 >= 1
    assert n2 == 0


def test_ingest_text(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        n = rag.ingest_text("Hello knowledge base", source_id="manual://test", collection="memory")

    assert n == 1
    mock_store.add_documents.assert_called_once()


# ── Search ────────────────────────────────────────────────────────────────────


def test_search(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        results = rag.search("Python programming", k=3)

    assert len(results) == 1
    assert results[0]["content"] == "Python is great"
    assert results[0]["score"] == 0.92


def test_search_below_threshold_filtered(rag, mock_store):
    """Results below score_threshold should be filtered out."""
    mock_store.similarity_search_with_relevance_scores.return_value = [
        (MagicMock(page_content="weak match", metadata={"source": "x.md", "chunk": 0}), 0.05),
    ]
    with patch.object(rag, "_get_store", return_value=mock_store):
        results = rag.search("query", k=5, score_threshold=0.20)
    assert results == []


def test_search_mmr(rag, mock_store):
    mock_store.max_marginal_relevance_search.return_value = [
        MagicMock(page_content="diverse result", metadata={"source": "a.md", "chunk": 0}),
    ]
    with patch.object(rag, "_get_store", return_value=mock_store):
        results = rag.search("query", k=3, mode="mmr")

    assert len(results) == 1
    assert results[0]["score"] is None  # MMR doesn't return scores


def test_search_all(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        results = rag.search_all("Python", k=5)

    assert len(results) >= 1


# ── Management ────────────────────────────────────────────────────────────────


def test_list_sources(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        sources = rag.list_sources()

    assert "test.md" in sources


def test_delete_source(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        count = rag.delete_source("test.md")

    assert count == 1
    mock_store.delete.assert_called_once_with(ids=["id1"])


def test_collection_stats(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        stats = rag.collection_stats()

    assert stats["chunks"] == 1
    assert stats["sources"] == 1


def test_hash_index_persisted(tmp_path):
    """Hash index should survive re-instantiation (no re-ingest on restart)."""
    from olano.memory.rag_memory import RAGMemory

    rag1 = RAGMemory(persist_dir=tmp_path / "rag")
    test_file = tmp_path / "doc.txt"
    test_file.write_text("content", encoding="utf-8")
    source_id = str(test_file.resolve())

    mock_store = MagicMock()
    mock_store.get.return_value = {"ids": [], "metadatas": []}

    with patch.object(rag1, "_get_store", return_value=mock_store):
        n1 = rag1.ingest_file(test_file)

    # New instance — should load hash index from disk and skip unchanged file
    rag2 = RAGMemory(persist_dir=tmp_path / "rag")
    with patch.object(rag2, "_get_store", return_value=mock_store):
        n2 = rag2.ingest_file(test_file)

    assert n1 >= 1
    assert n2 == 0  # skipped because hash persisted


def test_sync_directory_skips_store_init_when_unchanged(tmp_path):
    from olano.memory.rag_memory import RAGMemory, _file_hash

    src = tmp_path / "kb"
    src.mkdir()
    doc = src / "readme.md"
    doc.write_text("hello", encoding="utf-8")

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    rag._hash_index[str(doc.resolve())] = _file_hash(doc)

    with patch.object(rag, "_get_store", side_effect=AssertionError("store should not init")):
        stats = rag.sync_directory(collection="knowledgebase", directory=src)

    assert stats == {"added": 0, "updated": 0, "removed": 0, "skipped": 1}


def test_add_legacy_api(rag, mock_store):
    with patch.object(rag, "_get_store", return_value=mock_store):
        rag.add("Some fact", metadata={"source": "self_learning"})

    mock_store.add_documents.assert_called_once()


def test_ingest_url_via_http_fallback(tmp_path):
    from olano.memory.rag_memory import RAGMemory

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.get.return_value = {"ids": [], "metadatas": []}

    mock_response = MagicMock()
    mock_response.text = "# Page content\nThis is a test page."
    mock_response.raise_for_status = MagicMock()

    with (
        patch.object(rag, "_fetch_url", return_value="# Page content\nThis is a test page."),
        patch.object(rag, "_get_store", return_value=mock_store),
    ):
        n = rag.ingest_url("https://example.com")

    assert n >= 1


# ── Knowledge base tools ──────────────────────────────────────────────────────


def test_kb_add_file_tool(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_add_file, set_rag

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.get.return_value = {"ids": [], "metadatas": []}
    set_rag(rag)

    test_file = tmp_path / "doc.txt"
    test_file.write_text("Important document content.", encoding="utf-8")

    with patch.object(rag, "_get_store", return_value=mock_store):
        result = kb_add_file.invoke({"path": str(test_file)})

    assert "doc.txt" in result
    assert "chunk" in result


def test_kb_add_file_not_found(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_add_file, set_rag

    set_rag(RAGMemory(persist_dir=tmp_path / "rag"))
    result = kb_add_file.invoke({"path": "/nonexistent/file.txt"})
    assert "not found" in result.lower()


def test_kb_search_tool(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_search, set_rag

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.similarity_search_with_relevance_scores.return_value = [
        (
            MagicMock(page_content="Python basics", metadata={"source": "guide.md", "chunk": 0}),
            0.85,
        ),
    ]
    set_rag(rag)

    with patch.object(rag, "_get_store", return_value=mock_store):
        result = kb_search.invoke({"query": "Python", "collection": "workspace"})

    assert "Python basics" in result


def test_kb_search_mmr(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_search, set_rag

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.max_marginal_relevance_search.return_value = [
        MagicMock(page_content="diverse result", metadata={"source": "a.md", "chunk": 0}),
    ]
    set_rag(rag)

    with patch.object(rag, "_get_store", return_value=mock_store):
        result = kb_search.invoke({"query": "Python", "collection": "workspace", "mode": "mmr"})

    assert "diverse result" in result


def test_kb_list_empty(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_list, set_rag

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.get.return_value = {"ids": [], "metadatas": []}
    set_rag(rag)

    with patch.object(rag, "_get_store", return_value=mock_store):
        result = kb_list.invoke({})

    assert "empty" in result.lower()


def test_kb_create_store(tmp_path):
    from olano.memory.rag_memory import RAGMemory
    from olano.tools.knowledge_tools import kb_create_store, set_rag

    rag = RAGMemory(persist_dir=tmp_path / "rag")
    mock_store = MagicMock()
    mock_store.get.return_value = {"ids": [], "metadatas": []}
    set_rag(rag)

    src = tmp_path / "docs"
    src.mkdir()
    (src / "readme.md").write_text("# README\nHello world.", encoding="utf-8")
    (src / "notes.txt").write_text("Some notes here.", encoding="utf-8")

    with patch.object(rag, "_get_store", return_value=mock_store):
        result = kb_create_store.invoke({"name": "project_docs", "directory": str(src)})

    assert "project_docs" in result
    assert "chunk" in result
