"""The user guide has one source: the pages under docs/user-guide/.

MkDocs renders them into docs.olano.ai, and scripts/gen_user_guide.py
concatenates them into olano/archetypes/knowledge_user_guide.md, the single
file the runtime seeds into every agent workspace. These tests keep the two
from drifting: the generated file must be current, every page must be a page
(one H1, first), and every page must be reachable from the site's nav.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "user-guide"
SCRIPT = ROOT / "scripts" / "gen_user_guide.py"
MKDOCS = ROOT / "mkdocs.yml"
PAGES = sorted(p for p in DOCS.rglob("*.md") if not p.name.startswith("."))


def test_the_generated_guide_is_current() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_generated_guide_says_it_is_generated() -> None:
    head = (ROOT / "olano" / "archetypes" / "knowledge_user_guide.md").read_text(encoding="utf-8")[
        :600
    ]
    assert "Generated from docs/user-guide/" in head
    assert "https://docs.olano.ai" in head


def _nav_pages(nav: list) -> set[str]:
    out: set[str] = set()
    for item in nav:
        if isinstance(item, str):
            out.add(item)
            continue
        ((_label, value),) = item.items()
        if isinstance(value, str):
            out.add(value)
        else:
            out |= _nav_pages(value)
    return out


def test_every_page_is_in_the_nav_and_every_nav_entry_is_a_page() -> None:
    nav = _nav_pages(yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))["nav"])
    on_disk = {str(p.relative_to(DOCS)).replace("\\", "/") for p in PAGES}
    assert nav == on_disk, {"only in nav": nav - on_disk, "only on disk": on_disk - nav}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(DOCS)))
def test_a_page_opens_with_its_one_h1(page: Path) -> None:
    lines = page.read_text(encoding="utf-8").splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    assert first.startswith("# "), f"{page.name} does not start with an H1: {first!r}"
    in_fence = False
    h1s = 0
    for ln in lines:
        if ln.startswith("```") or ln.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^# ", ln):
            h1s += 1
    assert h1s == 1, f"{page.name} has {h1s} H1 headings"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(DOCS)))
def test_a_page_links_to_pages_not_to_the_old_numbered_anchors(page: Path) -> None:
    # The single-file guide used `#7-feature-guides`-style anchors. On the
    # site those headings live on other pages, so a link to one is dead.
    text = page.read_text(encoding="utf-8")
    stale = re.findall(r"\]\(#\d+[a-c]?-[^)]*\)", text)
    assert stale == [], stale
