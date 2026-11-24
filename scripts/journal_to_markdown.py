#!/usr/bin/env python3
"""Render Olano work-journal JSON into human-readable markdown.

The analytics layer writes one ``<home>/journal/YYYY-MM-DD.json`` per day
(deterministic metrics + optional LLM ``narrative``). This script turns those
into markdown — for a daily standup digest, a weekly report, or just reading in
a terminal. It needs no running backend: it reads the JSON files directly.

Usage::

    # Render today's journal to stdout
    python scripts/journal_to_markdown.py

    # A specific day
    python scripts/journal_to_markdown.py 2026-06-07

    # Every journal on disk, concatenated (newest first)
    python scripts/journal_to_markdown.py --all

    # A specific JSON file (anywhere), or a custom journal dir
    python scripts/journal_to_markdown.py --file /path/to/2026-06-07.json
    python scripts/journal_to_markdown.py --all --dir ~/.olano/journal

    # Write to a file / directory instead of stdout
    python scripts/journal_to_markdown.py 2026-06-07 --out report.md
    python scripts/journal_to_markdown.py --all --out-dir ./reports
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_usd(n: Any) -> str:
    try:
        return f"${float(n):,.4f}"
    except (TypeError, ValueError):
        return f"${n}"


def _as_float(n: Any) -> float:
    try:
        return float(n or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_credits(n: Any) -> str:
    """Olano credits, rendered verbatim — never converted from the dollar column.

    A journal day carries both books: ``credits`` is the gateway's authoritative
    charge for Olano-managed calls, ``usd`` a list-price estimate of the
    customer's own provider bill (0 for managed calls by design). This script
    reads JSON off disk with no backend to ask for the deployment's display
    unit, so it prints whichever books the day actually recorded.
    """
    v = _as_float(n)
    if v and abs(v) < 1:
        return f"{v:,.3f} credits"
    return f"{v:,.0f} credits"


def _has_credits(entry: dict[str, Any]) -> bool:
    """True if this day recorded any Olano-managed charge worth a column."""
    rows = [
        entry.get("metrics", {}),
        *(entry.get("by_agent") or []),
        *(entry.get("by_model") or []),
    ]
    return any(_as_float(r.get("credits")) for r in rows)


def _fmt_tokens(n: Any) -> str:
    """Compact token count: 12 → '12', 3400 → '3.4K', 1_200_000 → '1.2M'."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def render_journal(entry: dict[str, Any]) -> str:
    """Render one journal entry dict to a markdown string."""
    day = entry.get("date", "unknown")
    m = entry.get("metrics", {})
    lines: list[str] = [f"# Work Journal — {day}", ""]

    narrative = entry.get("narrative")
    if narrative:
        lines += ["> " + narrative.replace("\n", "\n> "), ""]

    # ── Summary line ──────────────────────────────────────────────────────
    # The two books are listed, never added: a credits charge and a dollar
    # estimate price different transactions, so a single "spend" line would be
    # a number with no meaning.
    show_credits = _has_credits(entry)
    agents = m.get("active_agents", []) or []
    lines += ["## Summary", ""]
    if show_credits:
        lines.append(f"- **Olano credits:** {_fmt_credits(m.get('credits'))}")
    lines += [
        f"- **API cost (your keys):** {_fmt_usd(m.get('usd', 0))}",
        f"- **Model calls:** {_fmt_int(m.get('calls', 0))}",
        f"- **Tokens:** {_fmt_int(m.get('total_tokens', 0))} "
        f"({_fmt_int(m.get('input_tokens', 0))} in / {_fmt_int(m.get('output_tokens', 0))} out)",
        f"- **Tool calls:** {_fmt_int(m.get('tool_calls', 0))}",
        f"- **Agent-to-agent messages:** {_fmt_int(m.get('a2a_messages', 0))}",
        f"- **Active agents:** {len(agents)}" + (f" ({', '.join(agents)})" if agents else ""),
        "",
    ]

    credits_head = "Credits | " if show_credits else ""
    credits_rule = "--:|" if show_credits else ""

    # ── Per-agent table ───────────────────────────────────────────────────
    by_agent = entry.get("by_agent", []) or []
    if by_agent:
        lines += [
            "## By agent",
            "",
            f"| Agent | {credits_head}API cost | Tokens | Calls | Tools | A2A (↑/↓) | Top tools |",
            f"|---|{credits_rule}--:|--:|--:|--:|--:|---|",
        ]
        for a in by_agent:
            top = ", ".join(f"`{t['tool']}`×{t['count']}" for t in (a.get("top_tools") or [])[:5])
            lines.append(
                f"| {a.get('agent', '?')} "
                + (f"| {_fmt_credits(a.get('credits'))} " if show_credits else "")
                + f"| {_fmt_usd(a.get('usd', 0))} "
                f"| {_fmt_tokens(a.get('total_tokens', 0))} "
                f"| {_fmt_int(a.get('calls', 0))} "
                f"| {_fmt_int(a.get('tool_calls', 0))} "
                f"| {_fmt_int(a.get('a2a_sent', 0))}/{_fmt_int(a.get('a2a_received', 0))} "
                f"| {top or '—'} |"
            )
        lines.append("")

    # ── Per-model table ───────────────────────────────────────────────────
    by_model = entry.get("by_model", []) or []
    if by_model:
        lines += [
            "## By model",
            "",
            f"| Model | {credits_head}API cost | Tokens | Calls |",
            f"|---|{credits_rule}--:|--:|--:|",
        ]
        for mm in by_model:
            lines.append(
                f"| `{mm.get('model', '?')}` "
                + (f"| {_fmt_credits(mm.get('credits'))} " if show_credits else "")
                + f"| {_fmt_usd(mm.get('usd', 0))} "
                f"| {_fmt_tokens(mm.get('total_tokens', 0))} "
                f"| {_fmt_int(mm.get('calls', 0))} |"
            )
        lines.append("")

    # ── Events ────────────────────────────────────────────────────────────
    events = entry.get("events", {}) or {}
    actions = events.get("cognition_actions", []) or []
    cortex = events.get("cortex", []) or []
    if actions or cortex:
        lines += ["## Notable activity", ""]
        for a in actions:
            summary = a.get("summary") or a.get("action", "")
            lines.append(f"- **{a.get('action', 'action')}** — {summary}")
        for c in cortex:
            verb = "Created" if c.get("created") else "Updated"
            lines.append(
                f"- {verb} {c.get('kind', 'item')} _{c.get('title', '')}_ → `{c.get('status', '')}`"
            )
        lines.append("")

    gen = entry.get("generated_at")
    if gen:
        lines.append(f"_Generated {gen}_")
    return "\n".join(lines).rstrip() + "\n"


def _default_journal_dir() -> Path:
    """Resolve the journal dir, importing olano if available else guessing."""
    try:
        from olano.config.home import get_olano_paths

        return get_olano_paths().journal_dir
    except Exception:
        import os

        home = (
            os.environ.get("OLANO_HOME")
            or os.environ.get("LIONCLAW_HOME")
            or str(Path.home() / ".olano")
        )
        return Path(home).expanduser() / "journal"


def _load_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Olano journal JSON to markdown.")
    parser.add_argument(
        "day",
        nargs="?",
        help="Day to render (YYYY-MM-DD). Defaults to today. Ignored with --all/--file.",
    )
    parser.add_argument("--all", action="store_true", help="Render every journal on disk.")
    parser.add_argument("--file", type=Path, help="Render a specific JSON file.")
    parser.add_argument("--dir", type=Path, help="Journal directory (default: <home>/journal).")
    parser.add_argument("--out", type=Path, help="Write output to this file instead of stdout.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="With --all: write one <day>.md per entry into this directory.",
    )
    args = parser.parse_args(argv)

    jdir = args.dir or _default_journal_dir()

    # Resolve which (path, entry) pairs to render.
    pairs: list[tuple[str, dict[str, Any]]] = []
    if args.file:
        if not args.file.exists():
            parser.error(f"file not found: {args.file}")
        entry = _load_file(args.file)
        pairs.append((entry.get("date", args.file.stem), entry))
    elif args.all:
        if not jdir.exists():
            parser.error(f"journal dir not found: {jdir}")
        for p in sorted(jdir.glob("*.json"), reverse=True):
            try:
                pairs.append((p.stem, _load_file(p)))
            except (OSError, json.JSONDecodeError):
                print(f"skip (unreadable): {p}", file=sys.stderr)
    else:
        day = args.day or datetime.now(timezone.utc).date().isoformat()
        try:
            date.fromisoformat(day)
        except ValueError:
            parser.error(f"invalid date: {day!r} (expected YYYY-MM-DD)")
        path = jdir / f"{day}.json"
        if not path.exists():
            parser.error(f"no journal for {day} at {path}")
        pairs.append((day, _load_file(path)))

    if not pairs:
        print("No journal entries found.", file=sys.stderr)
        return 1

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for day, entry in pairs:
            (args.out_dir / f"{day}.md").write_text(render_journal(entry), encoding="utf-8")
        print(f"Wrote {len(pairs)} file(s) to {args.out_dir}", file=sys.stderr)
        return 0

    rendered = "\n\n---\n\n".join(render_journal(entry) for _, entry in pairs)
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
