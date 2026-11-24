"""Two colour rules the dashboard cannot break silently.

Both exist because of shipped defects that no test, type-checker or browser
warning could catch — the page just rendered wrong, in one theme, quietly.

**A colour computed in JS is theme-blind.** The issue-label chips picked their
colour with ``hsl(<hash of the label> 75% 62%)``. A fixed lightness works on
whichever ground the author happened to be looking at and fails on the other:
measured on a pale theme, the labels in use landed between **1.16:1** and
**2.39:1** against their own chip fill, i.e. unreadable, against a 4.5:1 floor.
It is hue-blind too — at identical inputs, yellow-green is far brighter than
blue. The fix is the ``--hue-*`` ramp in ``styles/tokens.css``: thirteen slots,
each defined by every theme for its own ground, picked by name through
``core/hues.ts``. So: no ``hsl()``/``hsla()`` literal in dashboard TS/TSX.

**An undefined token is not a fallback, it is a dropped declaration.** Four
rules referenced ``var(--text-1)`` and two more ``var(--bg-1)`` / ``var(--bg-2)``
— names from an older palette that this token set never defined. CSS resolves
``var()`` with no fallback and no definition to the guaranteed-invalid value, so
the whole declaration is thrown away at computed-value time and the property
inherits instead. The channel badges had been rendering with inherited text
colour, not the hue they were written to use, and ``.queue-item`` had no fill at
all. Nothing errors, nothing logs. So: every ``var(--x)`` used without a
fallback must be defined somewhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "olano" / "dashboard_2.0" / "src"
STYLES = DASHBOARD / "styles"

pytestmark = pytest.mark.skipif(not DASHBOARD.is_dir(), reason="dashboard sources not present")

# `hsl(...)` written as CSS text inside a .css file is fine — it sits next to the
# theme that chose it. This rule is about *computing* one in JS, where nothing
# knows which theme is mounted.
_HSL = re.compile(r"\bhsla?\(")


def _ts_sources() -> list[Path]:
    return [
        p
        for p in [*DASHBOARD.rglob("*.ts"), *DASHBOARD.rglob("*.tsx")]
        if "node_modules" not in p.parts and not p.name.endswith(".test.ts")
    ]


def _strip_comments(text: str) -> str:
    """Blank out comments, keeping line numbers — a comment may *name* the ban.

    Crude on purpose: it also blanks a `//` inside a string literal, which can
    only ever cost this scan a match it did not need.
    """
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
    return "\n".join(re.sub(r"//.*", "", line) for line in text.splitlines())


def test_no_javascript_computed_colours() -> None:
    """Decorative colour comes from a --hue-* slot, never from hsl() in JS."""
    offenders = [
        f"{p.relative_to(DASHBOARD)}:{i}"
        for p in _ts_sources()
        for i, line in enumerate(_strip_comments(p.read_text(encoding="utf-8")).splitlines(), 1)
        if _HSL.search(line)
    ]
    assert not offenders, (
        "hsl()/hsla() computed in JS is theme-blind — a fixed lightness that reads "
        "on one ground is unreadable on the other. Take a slot from the channel-hue "
        f"ramp instead (core/hues.ts → hueVarFor). Offenders: {offenders}"
    )


def _declared_tokens() -> set[str]:
    """Every `--x:` declaration, from the stylesheets and from inline style props."""
    declared: set[str] = set()
    for css in STYLES.glob("*.css"):
        declared |= set(re.findall(r"(--[\w-]+)\s*:", css.read_text(encoding="utf-8")))
    # A component may set a custom property on an element (style={{ "--x": … }}),
    # which is a definition the stylesheets can legitimately read.
    for src in _ts_sources():
        text = src.read_text(encoding="utf-8")
        declared |= set(re.findall(r'"(--[\w-]+)"', text))
        declared |= set(re.findall(r"\[`?(--[\w-]+)", text))
    return declared


def test_every_css_token_used_without_a_fallback_is_defined() -> None:
    """var(--x) with no fallback and no definition drops the whole declaration."""
    declared = _declared_tokens()
    missing: list[str] = []
    for css in STYLES.glob("*.css"):
        for line_no, line in enumerate(css.read_text(encoding="utf-8").splitlines(), 1):
            for name, following in re.findall(r"var\(\s*(--[\w-]+)\s*([,)])", line):
                if following == ",":  # has a fallback — degrades on purpose
                    continue
                if name not in declared:
                    missing.append(f"{css.name}:{line_no} {name}")
    assert not missing, (
        "var(--x) with no fallback resolves to the guaranteed-invalid value when "
        "--x is never declared, so the browser throws the declaration away and the "
        "property inherits — silently, in every theme. Define the token in "
        f"tokens.css or give the var() a fallback. Offenders: {missing}"
    )
