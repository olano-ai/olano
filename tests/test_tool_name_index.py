"""Tests for the static tool-name index and lazy registry resolution.

The generated index (``olano/tools/_name_index.py``) lets ``_resolve_name``
import only the toolkit that owns a requested tool. These tests pin the two
properties that matter:

* the generated file matches a live scan of every toolkit (drift guard —
  regenerate with ``uv run python scripts/gen_tool_name_index.py``), and
* resolution through the fast path never triggers the full-registry scan,
  while anything unexpected still falls back to it.
"""

from __future__ import annotations

import importlib

import pytest

from olano.tools import registry
from olano.tools._name_index import NAME_INDEX


def _live_scan() -> dict[str, str]:
    """The same walk the generator performs: import every toolkit, map names."""
    index: dict[str, str] = {}
    for toolkit, mod_path in registry._TOOLKITS.items():
        mod = importlib.import_module(mod_path)
        for t in mod.get_tools():
            name = getattr(t, "name", None)
            if name:
                index[name] = mod_path
    return index


def test_generated_index_matches_live_scan():
    """Drift guard: a toolkit change without regenerating the index fails here."""
    live = _live_scan()
    assert NAME_INDEX == live, (
        "olano/tools/_name_index.py is stale — regenerate with:\n"
        "    uv run python scripts/gen_tool_name_index.py"
    )


@pytest.fixture
def cold_registry(monkeypatch: pytest.MonkeyPatch):
    """Registry state as a fresh process sees it: live index not yet built."""
    monkeypatch.setattr(registry, "_INDEX_BUILT", False)
    monkeypatch.setattr(registry, "_NAME_INDEX", {})
    return registry


def test_fast_path_resolves_without_full_scan(cold_registry, monkeypatch):
    def _boom() -> None:
        pytest.fail("_ensure_index (full toolkit scan) ran for an indexed name")

    monkeypatch.setattr(registry, "_ensure_index", _boom)
    tools = registry.resolve_tools(["graph_summary"])
    assert [t.name for t in tools] == ["graph_summary"]


def test_alias_resolves_via_fast_path(cold_registry, monkeypatch):
    def _boom() -> None:
        pytest.fail("_ensure_index (full toolkit scan) ran for an aliased name")

    monkeypatch.setattr(registry, "_ensure_index", _boom)
    tools = registry.resolve_tools(["get_current_date"])
    assert [t.name for t in tools] == ["get_current_time"]


def test_unknown_name_falls_back_to_full_scan(cold_registry, monkeypatch):
    calls: list[bool] = []

    def _fake_scan() -> None:
        calls.append(True)
        monkeypatch.setattr(registry, "_INDEX_BUILT", True)

    monkeypatch.setattr(registry, "_ensure_index", _fake_scan)
    assert registry.resolve_tools(["definitely_not_a_tool"]) == []
    assert calls, "unindexed names must still consult the authoritative scan"


def test_stale_index_entry_falls_back_to_full_scan(cold_registry, monkeypatch):
    """A drifted entry (module moved/renamed) must degrade to the scan, not break."""
    monkeypatch.setattr(
        registry, "_static_index", lambda: {"graph_summary": "olano.no_such_module"}
    )

    def _fake_scan() -> None:
        monkeypatch.setattr(registry, "_INDEX_BUILT", True)
        monkeypatch.setattr(registry, "_NAME_INDEX", {"graph_summary": "olano.memory.graph_tools"})

    monkeypatch.setattr(registry, "_ensure_index", _fake_scan)
    tools = registry.resolve_tools(["graph_summary"])
    assert [t.name for t in tools] == ["graph_summary"]


def test_live_index_wins_once_built(cold_registry, monkeypatch):
    """After the full scan has run, the live index is authoritative — the static
    index must not be consulted at all."""
    monkeypatch.setattr(registry, "_INDEX_BUILT", True)
    monkeypatch.setattr(registry, "_NAME_INDEX", {"graph_summary": "olano.memory.graph_tools"})

    def _boom() -> dict[str, str]:
        pytest.fail("_static_index consulted after the live scan was built")

    monkeypatch.setattr(registry, "_static_index", _boom)
    tools = registry.resolve_tools(["graph_summary"])
    assert [t.name for t in tools] == ["graph_summary"]
