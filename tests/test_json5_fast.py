"""Tests for the shared JSON5 loader (`olano.config.json5_fast`).

The point of this module is that it is *both* faster than the `json5` package
and identical to it on the files olano actually ships, so the tests assert both
the grammar it must accept and the two string-corruption bugs that the regex
shortcuts it replaced were guilty of.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.config.json5_fast import loads, strip_json5_sugar

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── The bugs this module exists to make impossible ──────────────────────────


def test_double_slash_inside_a_string_survives():
    """`re.sub(r"//.*$")` truncated any value containing `//`.

    This was live in `config/loader.py` and `orchestration/subagent_store.py`,
    where it silently rewrote every URL in an agent config.
    """
    doc = '{"url": "https://example.com/a//b", "n": 1}'
    assert loads(doc) == {"url": "https://example.com/a//b", "n": 1}


def test_comma_before_brace_inside_a_string_survives():
    """`re.sub(r",\\s*([}\\]])", r"\\1")` ate commas inside string values."""
    doc = '{"note": "ends with a comma, }", "list": ["a, ]", "b"]}'
    assert loads(doc) == {"note": "ends with a comma, }", "list": ["a, ]", "b"]}


def test_comment_markers_inside_strings_are_not_comments():
    doc = '{"a": "/* not a comment */", "b": "// neither"}'
    assert loads(doc) == {"a": "/* not a comment */", "b": "// neither"}


def test_escaped_quote_does_not_end_the_string():
    doc = r'{"a": "she said \"hi\" // still in the string", "b": 2}'
    assert loads(doc)["b"] == 2
    assert loads(doc)["a"] == 'she said "hi" // still in the string'


# ── Grammar it must accept ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ('{"a": 1} // trailing line comment', {"a": 1}),
        ('// leading\n{"a": 1}', {"a": 1}),
        ('{/* block */ "a": 1}', {"a": 1}),
        ('{"a": 1,}', {"a": 1}),
        ('{"a": [1, 2,],}', {"a": [1, 2]}),
        ('{"a": [{"b": 1},]}', {"a": [{"b": 1}]}),
        ('{"a": 1, // note\n}', {"a": 1}),
        ('{"a": 1, /* note */ }', {"a": 1}),
        ("{}", {}),
        # A comma that is NOT trailing must survive.
        ('{"a": 1, "b": 2}', {"a": 1, "b": 2}),
        ('{"a": [1, 2], "b": 3}', {"a": [1, 2], "b": 3}),
        # Unquoted keys / single quotes are real JSON5 — handled by the fallback.
        ("{name: 'x', n: 1}", {"name": "x", "n": 1}),
    ],
)
def test_accepts_json5_grammar(doc, expected):
    assert loads(doc) == expected


def test_malformed_input_raises_rather_than_returning_empty():
    """Fail closed: a broken file must not be mistaken for an empty one."""
    with pytest.raises(ValueError):
        loads('{"a": ')


def test_strip_preserves_offsets():
    """Removed commas become spaces, so error columns still point at the source."""
    doc = '{"a": 1,}'
    assert len(strip_json5_sugar(doc)) == len(doc)


# ── Equivalence with the json5 package over everything we ship ──────────────


def _shipped_json5_files() -> list[Path]:
    roots = [REPO_ROOT / "olano", REPO_ROOT / "tests", REPO_ROOT / "agents"]
    out: list[Path] = []
    for root in roots:
        if root.exists():
            out.extend(sorted(root.rglob("*.json5")))
    return out


def test_matches_json5_package_on_every_shipped_file():
    """Every archetype, ToolkitAgent def and agent config parses identically.

    This is the safety net for the fast path: if a future file uses syntax the
    pre-pass mishandles, this fails rather than the catalog quietly losing an
    entry.
    """
    json5 = pytest.importorskip("json5")
    files = _shipped_json5_files()
    assert files, "expected to find shipped .json5 files"
    mismatched = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            expected = json5.loads(text)
        except Exception:  # a file json5 itself rejects is not our problem
            continue
        if loads(text) != expected:
            mismatched.append(str(path.relative_to(REPO_ROOT)))
    assert not mismatched, f"fast loader disagrees with json5 on: {mismatched}"


def test_fast_path_covers_the_bundled_archetypes():
    """The bundled templates must take the stdlib path, not the json5 fallback.

    The whole performance argument rests on the common case never reaching
    `json5`, so assert it directly instead of trusting a benchmark.
    """
    templates = sorted((REPO_ROOT / "olano" / "archetypes" / "templates").glob("*.json5"))
    assert templates, "expected bundled archetype templates"
    for path in templates:
        # Raises if the pre-pass left anything stdlib json can't read.
        json.loads(strip_json5_sugar(path.read_text(encoding="utf-8")))
