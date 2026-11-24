"""Language-parity drift guard for the per-view string dictionaries.

The dashboard ships three languages (en/zh/es — ``core/i18n.tsx``). A key
added to one language but not the others silently falls back to English at
runtime, which reads as a bug to a zh/es operator. This test parses the view
dictionaries listed below and asserts every language block defines the same
key set.
Pure-text parsing on purpose: no node toolchain in the unit suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_VIEWS = Path(__file__).parent.parent / "olano" / "dashboard_2.0" / "src" / "views"
_STRINGS_FILES = [
    _VIEWS / "issues" / "strings.ts",
    _VIEWS / "revisions" / "strings.ts",
    _VIEWS / "config" / "strings.ts",
    _VIEWS / "nav" / "strings.ts",
    _VIEWS / "simple" / "strings.ts",
    _VIEWS / "agents" / "presenceStrings.ts",
]
_LANGS = ("en", "zh", "es")


def _lang_blocks(source: str) -> dict[str, str]:
    """Split the STR literal into per-language block texts by brace depth."""
    blocks: dict[str, str] = {}
    for lang in _LANGS:
        match = re.search(rf"^  {lang}: \{{", source, re.MULTILINE)
        assert match, f"missing language block {lang!r}"
        depth, i = 1, match.end()
        while depth and i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        blocks[lang] = source[match.end() : i]
    return blocks


def _keys(block: str) -> set[str]:
    return set(re.findall(r'^\s+"([^"]+)":', block, re.MULTILINE))


@pytest.mark.parametrize("path", _STRINGS_FILES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_all_languages_define_the_same_keys(path: Path) -> None:
    blocks = _lang_blocks(path.read_text(encoding="utf-8"))
    en_keys = _keys(blocks["en"])
    assert en_keys, "no keys parsed — parser drift?"
    for lang in ("zh", "es"):
        missing = en_keys - _keys(blocks[lang])
        extra = _keys(blocks[lang]) - en_keys
        assert not missing, f"{path.parent.name}/{lang} missing keys: {sorted(missing)}"
        assert not extra, f"{path.parent.name}/{lang} extra keys: {sorted(extra)}"
