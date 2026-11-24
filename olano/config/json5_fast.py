"""Fast, correct JSON5 parsing for every config/catalog read path.

Every JSON5 file olano reads — ``agent.json5``, archetype templates,
ToolkitAgent definitions, orchestration subagent files — is ordinary JSON plus
two conveniences: comments and trailing commas. The ``json5`` package parses
the full grammar, but it is pure Python and that cost is not academic: parsing
the 202-file canonical ``olano-agents`` archetype pack through it takes ~24s
(~84ms/file), which used to land inline on the event loop in
``GET /api/archetypes``.

So this module strips comments and trailing commas in one character walk and
hands the result to stdlib ``json`` (a C parser) — 0.017s for those same 202
files, ~1000x faster. Anything the strict parser rejects (unquoted keys,
single-quoted strings, hex/``Infinity`` literals, …) falls through to
``json5``, so the accepted grammar is exactly what it was before; only the
common case got fast.

**The pre-pass is a character walk, never a regex.** Both regex shortcuts are
wrong on real files and both were live in this repo before this module:

* ``re.sub(r"//.*$", "", text, flags=re.MULTILINE)`` corrupts
  ``"url": "https://example.com"`` into ``"url": "https:``.
* ``re.sub(r",\\s*([}\\]])", r"\\1", text)`` corrupts
  ``"note": "ends with a comma, }"`` into ``"note": "ends with a comma}"``.

The walk below tracks whether it is inside a string literal, so neither can
happen. This is the only JSON5 entry point in the codebase — do not add
another parser; import :func:`loads` instead.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["loads", "strip_json5_sugar"]

_WHITESPACE = " \t\r\n"


def strip_json5_sugar(text: str) -> str:
    """Return ``text`` with comments and trailing commas removed.

    String-literal aware: quoted content is copied through verbatim, escapes
    included, so ``//`` and ``,`` inside a value are never touched. Removed
    trailing commas are replaced by a space rather than deleted, which keeps
    byte offsets stable so a ``json`` error message still points at the right
    column of the original file.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    # Index in `out` of the most recent comma that could still turn out to be
    # a trailing one, or -1. Whitespace and comments preserve it; any other
    # token clears it, because then the comma had a value after it.
    pending_comma = -1

    while i < n:
        ch = text[i]

        # ── String literal: copy verbatim, honouring backslash escapes ──────
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                ch = text[i]
                out.append(ch)
                if ch == "\\":
                    i += 1
                    if i < n:
                        out.append(text[i])
                elif ch == quote:
                    break
                i += 1
            i += 1  # step past the closing quote (or past the end if unterminated)
            pending_comma = -1
            continue

        # ── Comments: dropped entirely, without clearing `pending_comma` ────
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        # ── Structural characters ──────────────────────────────────────────
        if ch == ",":
            out.append(ch)
            pending_comma = len(out) - 1
            i += 1
            continue
        if ch in _WHITESPACE:
            out.append(ch)
            i += 1
            continue
        if ch in "}]" and pending_comma >= 0:
            out[pending_comma] = " "  # that comma was trailing after all

        pending_comma = -1
        out.append(ch)
        i += 1

    return "".join(out)


def loads(text: str) -> Any:
    """Parse a JSON5 document.

    Fast path is stdlib ``json`` over :func:`strip_json5_sugar`; exotic JSON5
    syntax falls back to the ``json5`` package. Raises the underlying parser's
    error (``ValueError``) when neither can read the document — callers that
    want a lenient read must catch it themselves, so a malformed file is never
    silently mistaken for an empty one.
    """
    try:
        return json.loads(strip_json5_sugar(text))
    except ValueError as fast_error:
        try:
            import json5
        except ImportError:
            raise fast_error from None
        return json5.loads(text)
