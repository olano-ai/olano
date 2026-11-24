"""One source of truth for the offline MCP directory baseline.

The rows used to be hand-maintained in two places — Python's
``_BUNDLED_PRESETS``/``_BUNDLED_EXAMPLES`` and the dashboard's
``mcpPresets.ts``/``mcpDirectory.ts`` — and they drifted: the same server was
``parallel`` on one side and ``parallel-search`` on the other, with a different
title, description, ``oauthClient`` and a category that existed nowhere else,
while ``github-docker-stdio`` existed in both fallbacks and in no cloud row at
all. So the modal and the onboarding wizard could offer different servers.

Both now derive from ``olano/core/bundled_mcp.json``. These tests pin that the
snapshot is the only source, and that the generated TS module beside the
dashboard has not drifted from it — the dashboard cannot import the JSON
directly (its Vite root and Docker build context are both
``olano/dashboard_2.0/``), so the generated file is what stands in for it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from olano.core import mcp_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_bundled_mcp.py"
TS_MODULE = (
    REPO_ROOT / "olano" / "dashboard_2.0" / "src" / "views" / "agents" / "mcpBundled.generated.ts"
)


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(mcp_registry.BUNDLED_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_the_snapshot_is_what_the_bundled_directory_serves(snapshot) -> None:
    """No second copy: every bundled row comes from the JSON, in its order."""
    directory = mcp_registry._bundled_directory()
    served = [p["slug"] for p in directory["presets"]] + [e["slug"] for e in directory["examples"]]
    pinned = [r["slug"] for r in snapshot["servers"]]
    assert sorted(served) == sorted(pinned)
    assert directory["source"] == "bundled"
    # Split by `kind`, exactly as a live cloud payload is.
    assert {e["slug"] for e in directory["examples"]} == {
        r["slug"] for r in snapshot["servers"] if r.get("kind") == "example"
    }


def test_rows_normalize_to_the_full_wire_shape(snapshot) -> None:
    """Sparse on disk, complete on load — same as a cloud response."""
    directory = mcp_registry._bundled_directory()
    for entry in directory["presets"] + directory["examples"]:
        assert set(entry) == set(mcp_registry._ENTRY_DEFAULTS)


def test_the_parallel_row_uses_the_catalog_slug(snapshot) -> None:
    """The drift that started this: the dashboard called it `parallel-search`.

    Everything that resolves an entry keys on the catalog slug, so the other
    spelling would duplicate the row the moment a real sync landed.
    """
    slugs = {r["slug"] for r in snapshot["servers"]}
    assert "parallel" in slugs
    assert "parallel-search" not in slugs


def test_no_row_is_invented_by_the_fallback(snapshot) -> None:
    """`github-docker-stdio` was in both fallbacks and in no cloud row, so a
    real sync silently deleted it. It has a migration now; keep it pinned so
    the fallback and the catalog cannot disagree about it again."""
    slugs = {r["slug"] for r in snapshot["servers"]}
    assert "github-docker-stdio" in slugs


def test_every_row_has_an_identity_and_a_kind(snapshot) -> None:
    seen: set[str] = set()
    for row in snapshot["servers"]:
        assert row.get("slug"), f"row without a slug: {row}"
        assert row.get("name"), f"{row['slug']} has no name"
        assert row.get("kind") in {"preset", "example"}, f"{row['slug']} has a bad kind"
        assert row["slug"] not in seen, f"duplicate slug {row['slug']}"
        seen.add(row["slug"])


def test_an_unreadable_snapshot_degrades_instead_of_raising(monkeypatch, tmp_path) -> None:
    """A packaging mistake must not take the process down on import: every
    caller above this tier already handles an empty directory."""
    monkeypatch.setattr(mcp_registry, "BUNDLED_SNAPSHOT_PATH", tmp_path / "missing.json")
    assert mcp_registry._load_bundled_servers() == []
    assert mcp_registry._bundled_directory()["presets"] == []


def test_the_generated_ts_module_is_not_stale() -> None:
    """Drift guard for the copy the dashboard actually imports.

    Regenerate with ``uv run python scripts/gen_bundled_mcp.py --sync-ts``.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_generated_ts_carries_every_pinned_row(snapshot) -> None:
    """Cheap structural check that does not shell out: the TS module names
    every slug the snapshot pins, so a hand-edit that drops one is caught even
    if the generator is unavailable."""
    text = TS_MODULE.read_text(encoding="utf-8")
    for row in snapshot["servers"]:
        assert f'"slug": "{row["slug"]}"' in text, f"{row['slug']} missing from the TS module"
