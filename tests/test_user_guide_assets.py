"""The user guide's pictures: every image a page shows exists, every screenshot
is shown somewhere, and the agent's single-file copy carries none of them.

Screenshots are taken by scripts/capture_docs_screenshots.py into
docs/user-guide/assets/screenshots/. A page that names an image the script
never took renders a broken picture on docs.olano.ai, and a screenshot no page
uses is weight in the repository that nobody will ever re-take; both fail here.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "user-guide"
SHOTS_DIR = DOCS / "assets" / "screenshots"
CAPTURE = ROOT / "scripts" / "capture_docs_screenshots.py"
AGENT_COPY = ROOT / "olano" / "archetypes" / "knowledge_user_guide.md"
PAGES = sorted(p for p in DOCS.rglob("*.md") if not p.name.startswith("."))

IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _images() -> list[tuple[Path, str, Path]]:
    """(page, alt text, resolved target) for every Markdown image in the guide."""
    found: list[tuple[Path, str, Path]] = []
    for page in PAGES:
        for alt, target in IMAGE_RE.findall(page.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            found.append((page, alt, (page.parent / target).resolve()))
    return found


def test_every_image_a_page_shows_exists() -> None:
    missing = [
        f"{page.relative_to(DOCS)} -> {target.relative_to(ROOT)}"
        for page, _alt, target in _images()
        if not target.is_file()
    ]
    assert missing == []


def test_every_image_has_alt_text() -> None:
    bare = [
        f"{page.relative_to(DOCS)}: {target.name}"
        for page, alt, target in _images()
        if not alt.strip()
    ]
    assert bare == []


def test_every_screenshot_is_shown_on_some_page() -> None:
    shown = {target for _page, _alt, target in _images()}
    unused = sorted(p.name for p in SHOTS_DIR.glob("*") if p.resolve() not in shown)
    assert unused == []


def test_the_screenshots_are_the_ones_the_capture_script_takes() -> None:
    spec = importlib.util.spec_from_file_location("capture_docs_screenshots", CAPTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: @dataclass looks its own module up in sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    taken = {f"{shot.name}.webp" for shot in module.SHOTS}
    on_disk = {p.name for p in SHOTS_DIR.glob("*")}
    assert on_disk == taken, {"never taken": on_disk - taken, "missing": taken - on_disk}


def test_the_agent_copy_carries_no_pictures() -> None:
    text = AGENT_COPY.read_text(encoding="utf-8")
    assert "assets/screenshots" not in text
    assert "<figure" not in text
