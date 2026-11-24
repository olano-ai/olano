#!/usr/bin/env python3
"""Generate olano/archetypes/knowledge_user_guide.md from docs/user-guide/.

The user guide has one source: the pages under docs/user-guide/, which MkDocs
renders into docs.olano.ai (see mkdocs.yml). The runtime seeds a single-file
copy of the same guide into every agent workspace (system/knowledge/USER_GUIDE.md,
RAG-indexed and read on demand with workspace_context), and this script
produces that copy: the pages concatenated in the site's nav order, every
heading pushed down under one section heading per page, page links turned
back into in-document anchors, and a generated table of contents in front.

    uv run python scripts/gen_user_guide.py          # rewrite the file
    uv run python scripts/gen_user_guide.py --check  # exit 1 when it is stale

tests/test_user_guide_generated.py runs --check, so a page edited without a
regeneration fails CI, and a hand edit to the generated file is undone by the
next run. The generated file carries a comment saying so.

Pictures stay on the website. A screenshot (a `<figure>` block, or an image on
a line of its own) is dropped from the agent's copy: the agent reads text, and
a path to an image it cannot open is noise in every search hit. Whatever a
reader needs from a picture is said in the prose around it. Mermaid diagrams
are text and are kept as they are.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs" / "user-guide"
TARGET = ROOT / "olano" / "archetypes" / "knowledge_user_guide.md"

# The landing page is for the website; the agent's copy opens with PREAMBLE.
SKIP_PAGES = {"index.md"}

PREAMBLE = """\
# Olano User Guide

<!-- Generated from docs/user-guide/ by scripts/gen_user_guide.py. Do not edit
this file: edit the page under docs/user-guide/ and regenerate. The same pages
are published at https://docs.olano.ai. -->

This is the reference for people who run an Olano deployment, and for agents
that build other agents. It covers the dashboard, every field in the two
configuration files, every major feature, the Olano model tiers, and the
Olano Cloud service.

**How to use this guide.** It is long on purpose: it is meant to be searched,
not read front to back. Jump to the section you need from the table of contents.
Every configuration field lists its accepted values and its default, so you can
write a valid `agent.json5` from this document alone.

**If you are an agent reading this:** this file is your source of truth for
platform questions. Never guess a field name, a tool name, or a model string;
look it up here. When a value you write has to match something the runtime
already knows about (an agent name, a tool, an archetype, a model), confirm it
with the matching `olano_list_*` tool before writing it into a config.
"""


def md_slug(title: str) -> str:
    """Python-Markdown's toc slug of a heading, over its rendered text.

    Links between pages are written as `page.md#anchor`, where the anchor is
    what MkDocs emits for the target heading. The concatenated copy keeps the
    same anchors, so the same function has to produce them here.
    """
    text = title.replace("`", "")
    text = re.sub(r"(?<!\w)\*+|\*+(?!\w)", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


# ── the nav ──────────────────────────────────────────────────────────────────


def nav_pages(nav: list) -> list[tuple[str, int]]:
    """(page path, depth) in reading order. Depth 1 = a section, 2 = a page
    inside a section folder. A section's index page is its first entry."""
    out: list[tuple[str, int]] = []
    for item in nav:
        if isinstance(item, str):
            out.append((item, 1))
            continue
        ((_label, value),) = item.items()
        if isinstance(value, str):
            out.append((value, 1))
            continue
        children = list(value)
        first = children[0]
        index = first if isinstance(first, str) else next(iter(first.values()))
        out.append((index, 1))
        for child in children[1:]:
            page = child if isinstance(child, str) else next(iter(child.values()))
            out.append((page, 2))
    return out


def load_nav() -> list[tuple[str, int]]:
    doc = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))
    return [(p, d) for p, d in nav_pages(doc["nav"]) if p not in SKIP_PAGES]


# ── one page ─────────────────────────────────────────────────────────────────

HEADING_RE = re.compile(r"^(#{1,6}) (.+?)\s*$")
PAGE_LINK_RE = re.compile(r"\]\(([^)\s#]+\.md)(#[^)\s]+)?\)")


def strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def page_title(text: str) -> str:
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    raise ValueError("page has no H1")


IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)(\{[^}]*\})?\s*$")


def strip_pictures(text: str) -> str:
    """Drop `<figure>` blocks and stand-alone images (see the module docstring)."""
    out: list[str] = []
    in_fence = False
    in_figure = False
    for line in text.splitlines():
        if not in_figure and (line.startswith("```") or line.startswith("~~~")):
            in_fence = not in_fence
        if not in_fence:
            if line.lstrip().startswith("<figure"):
                in_figure = True
            if in_figure:
                if "</figure>" in line:
                    in_figure = False
                continue
            if IMAGE_LINE_RE.match(line):
                continue
        out.append(line)
    # A dropped picture leaves its blank lines behind; never more than one.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def shift_headings(text: str, by: int) -> str:
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            line = "#" * (len(m.group(1)) + by) + " " + m.group(2)
        out.append(line)
    return "\n".join(out)


def relink(text: str, page: str, titles: dict[str, str]) -> str:
    """`other.md` and `other.md#anchor` become in-document anchors."""
    base = PurePosixPath(page).parent

    def repl(m: re.Match[str]) -> str:
        # Page links are relative ("../config-yaml.md"); resolve them against
        # the linking page, in POSIX form whatever the host, to the nav's key.
        target = posixpath.normpath(str(base / m.group(1)))
        anchor = m.group(2)
        if anchor:
            return f"]({anchor})"
        if target not in titles:
            raise ValueError(f"{page}: link to unknown page {target}")
        return f"](#{md_slug(titles[target])})"

    return PAGE_LINK_RE.sub(repl, text)


# ── the whole document ───────────────────────────────────────────────────────


def generate() -> str:
    pages = load_nav()
    texts: dict[str, str] = {}
    titles: dict[str, str] = {}
    for page, _depth in pages:
        path = DOCS / page
        if not path.is_file():
            raise FileNotFoundError(f"mkdocs.yml lists {page}, which does not exist")
        text = strip_front_matter(path.read_text(encoding="utf-8"))
        texts[page] = text
        titles[page] = page_title(text)

    on_disk = {
        str(p.relative_to(DOCS)).replace(os.sep, "/")
        for p in DOCS.rglob("*.md")
        if not p.name.startswith(".")
    } - SKIP_PAGES
    missing = sorted(on_disk - set(texts))
    if missing:
        raise ValueError(f"pages not listed in mkdocs.yml nav: {', '.join(missing)}")

    toc: list[str] = []
    number = 0
    for page, depth in pages:
        link = f"[{titles[page]}](#{md_slug(titles[page])})"
        if depth == 1:
            number += 1
            toc.append(f"{number}. {link}")
        else:
            toc.append(f"    - {link}")

    body: list[str] = []
    for page, depth in pages:
        text = relink(strip_pictures(texts[page]), page, titles)
        text = shift_headings(text, depth).strip() + "\n"
        if depth == 1:
            body.append("---\n")
        body.append(text)

    return (
        PREAMBLE + "\n---\n\n## Table of contents\n\n" + "\n".join(toc) + "\n\n" + "\n".join(body)
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the generated file is stale"
    )
    args = parser.parse_args(argv)
    text = generate()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if args.check:
        if current == text:
            print(f"{TARGET.relative_to(ROOT)} is current")
            return 0
        print(
            f"{TARGET.relative_to(ROOT)} is stale: run `uv run python scripts/gen_user_guide.py`",
            file=sys.stderr,
        )
        return 1
    if current == text:
        print(f"{TARGET.relative_to(ROOT)} unchanged")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
