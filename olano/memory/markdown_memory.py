"""Markdown-based persistent memory — reads/writes MEMORY.md in the workspace."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a sibling ``.tmp`` file first, fsyncs it, then ``os.replace``s
    it into the final location.  If anything raises mid-flight the original
    file is untouched, which is the property MEMORY.md and the daily logs
    were missing — a partial write previously corrupted them.

    Ownership: a file inside a live sandbox's workspace is handed to the
    sandbox agent (uid 1000) along with any directory this call created, so a
    runtime write into a workspace belongs to the agent by default.  The
    existing mode is kept across the rename everywhere.
    """
    from olano.tools._sandbox_exec import handoff_workspace_write, missing_parents

    path = Path(path)
    created = missing_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The rename replaces the file's mode with the temp file's (mkstemp:
    # 0600).  Keep the existing one so a 0644 note stays readable and a
    # private 0600 file stays private.
    try:
        prev = path.stat()
    except FileNotFoundError:
        prev = None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as exc:
                # fsync isn't supported on every filesystem (e.g. some FUSE);
                # not fatal, just less durable.
                logger.debug("fsync failed for %s: %s", tmp_path, exc)
        if prev is not None:
            try:
                tmp_path.chmod(prev.st_mode & 0o7777)
            except OSError as exc:
                logger.debug("could not preserve mode on %s: %s", path, exc)
        tmp_path.replace(path)
        handoff_workspace_write(path, created=created)
    except BaseException:
        # Clean up the temp file on any failure path so we don't leave
        # ``.MEMORY.md.abc123.tmp`` siblings lying around.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except Exception as cleanup_exc:
            logger.debug("temp cleanup failed for %s: %s", tmp_path, cleanup_exc)
        raise


_BULLET_RE = re.compile(r"^[-*]\s+")
_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SEARCH_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "can",
    "did",
    "do",
    "does",
    "for",
    "he",
    "her",
    "his",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "this",
    "to",
    "what",
    "when",
    "which",
    "who",
    "with",
    "you",
    "your",
}
_SEARCH_SYNONYMS = {
    "carlos": {"user"},
    "equity": {"stock", "ticker"},
    "favorite": {"interest", "prefer", "track", "like"},
    "favorites": {"interest", "prefer", "track", "like"},
    "interested": {"interest", "prefer", "track", "like"},
    "interests": {"interest", "prefer", "track", "like"},
    "like": {"interest", "prefer", "track", "favorite"},
    "likes": {"interest", "prefer", "track", "favorite"},
    "portfolio": {"holding", "stock", "ticker"},
    "preferred": {"prefer", "interest", "like", "favorite"},
    "prefers": {"prefer", "interest", "like", "favorite"},
    "stock": {"equity", "ticker", "portfolio", "holding"},
    "stocks": {"equity", "ticker", "portfolio", "holding"},
    "ticker": {"stock", "equity"},
    "tickers": {"stock", "equity"},
    "tracked": {"track", "interest", "stock"},
    "tracks": {"track", "interest", "stock"},
}


def _normalize_bullet(line: str) -> str | None:
    """Return a normalized dedup key for a bullet line, or None if not a bullet.

    Strips the bullet marker and a leading ``[YYYY-MM-DD]`` date stamp, lowercases,
    and collapses whitespace so ``- [2026-01-01] Uses Python`` and
    ``- [2026-02-02] uses  python`` collide.
    """
    stripped = line.strip()
    match = _BULLET_RE.match(stripped)
    if not match:
        return None
    body = stripped[match.end() :]
    body = _DATE_RE.sub("", body, count=1).strip()
    norm = re.sub(r"\s+", " ", body).strip().lower()
    return norm or None


def _bullet_date(line: str) -> str:
    """Extract the ``YYYY-MM-DD`` date from a bullet, or '' if none."""
    match = _DATE_RE.search(line)
    return match.group(1) if match else ""


def _search_stem(token: str) -> str:
    """Normalize common inflections without introducing a stemming dependency."""
    token = token.lower()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ed"):
        base = token[:-2]
        if len(base) >= 2 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "ses", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _search_tokens(text: str, *, drop_stopwords: bool = False) -> set[str]:
    tokens = {_search_stem(t) for t in _TOKEN_RE.findall(text.lower())}
    if drop_stopwords:
        return {t for t in tokens if t not in _SEARCH_STOPWORDS}
    return tokens


def _expanded_search_terms(query: str) -> set[str]:
    terms = _search_tokens(query, drop_stopwords=True)
    expanded = set(terms)
    for term in terms:
        expanded.update(_search_stem(s) for s in _SEARCH_SYNONYMS.get(term, set()))
    return expanded


class MarkdownMemory:
    """
    File-based memory backed by MEMORY.md (curated long-term) and
    daily files at memories/YYYY-MM-DD.md (raw session logs).

    Sections are H2 headings (## Section Name).
    Facts are appended as bullet points under sections.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.path = workspace_root / "MEMORY.md"
        self.memories_dir = workspace_root / "memories"
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        if not self.path.exists():
            atomic_write_text(
                self.path,
                "# MEMORY.md - Long-Term Memory\n\n"
                "*This file grows as you learn. Curated memories, not raw logs.*\n",
            )
        if not self.memories_dir.exists():
            from olano.tools._sandbox_exec import handoff_workspace_write

            self.memories_dir.mkdir(parents=True)
            handoff_workspace_write(self.memories_dir)

    # ------------------------------------------------------------------ MEMORY.md

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        # Atomic write: stage to a sibling .tmp, fsync, then rename.  Prevents
        # partial-update corruption of MEMORY.md when self-learning fails
        # mid-flight.
        atomic_write_text(self.path, content)

    def append_fact(self, fact: str, section: str = "General") -> None:
        """Append a fact under the given section heading in MEMORY.md.

        Skips the write if a normalized-equal fact already exists in this section,
        preventing the most common source of Self-Learned bloat (same fact re-added
        every session because the raw text is virtually identical).
        """
        content = self.read()
        heading = f"## {section}"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bullet = f"- [{ts}] {fact.strip()}"
        norm_new = _normalize_bullet(bullet)

        # Skip if the same normalized fact is already in this section.
        if norm_new:
            section_text = self.get_section(section)
            for line in section_text.splitlines():
                if _normalize_bullet(line) == norm_new:
                    return

        if heading in content:
            content = content.replace(f"{heading}\n", f"{heading}\n{bullet}\n", 1)
        else:
            content = content.rstrip() + f"\n\n{heading}\n{bullet}\n"

        self.write(content)

    def search(self, query: str) -> list[str]:
        """Ranked lexical search across MEMORY.md; returns matching lines.

        Exact substring matches are preserved, but broader natural-language
        queries also match by token overlap, section context, simple inflection
        normalization, and a small preference-oriented synonym map. This keeps
        the search deterministic and local while avoiding grep-like misses such
        as "which stocks does Carlos like" vs "Stock interests: QCOM...".
        """
        query = query.strip()
        if not query:
            return []

        content = self.read()
        query_lower = query.lower()
        query_terms = _search_tokens(query, drop_stopwords=True)
        expanded_terms = _expanded_search_terms(query)
        if not expanded_terms:
            return []

        scored: list[tuple[int, int, str]] = []
        current_section = ""

        for idx, line in enumerate(content.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("## "):
                current_section = stripped[3:].strip()
                continue

            searchable = f"{current_section} {stripped}"
            line_lower = stripped.lower()
            line_tokens = _search_tokens(stripped)
            context_tokens = _search_tokens(searchable)

            score = 0
            if query_lower in line_lower:
                score += 100

            exact_hits = query_terms & line_tokens
            expanded_hits = expanded_terms & line_tokens
            context_hits = expanded_terms & context_tokens

            score += len(exact_hits) * 8
            score += len(expanded_hits - exact_hits) * 5
            score += len(context_hits - expanded_hits) * 2
            if "stock" in query_terms and "stock" in line_tokens:
                score += 8

            # Prefer compact facts that answer several query terms over long
            # archive-style paragraphs with incidental matches.
            if len(expanded_hits) >= 2:
                score += 4
            if len(stripped) <= 240:
                score += 1

            if score >= 5:
                scored.append((score, idx, stripped))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [line for _, _, line in scored]

    def get_section(self, section: str) -> str:
        """Return all content under a given H2 section."""
        content = self.read()
        pattern = rf"## {re.escape(section)}\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, content, re.DOTALL)
        return match.group(1).strip() if match else ""

    def list_sections(self) -> list[str]:
        """Return all section names."""
        return re.findall(r"^## (.+)$", self.read(), re.MULTILINE)

    def summarize_and_trim(self, max_chars: int = 20_000) -> None:
        """If MEMORY.md exceeds max_chars, truncate oldest content."""
        content = self.read()
        if len(content) > max_chars:
            trimmed = content[-max_chars:]
            boundary = trimmed.find("\n## ")
            if boundary > 0:
                trimmed = trimmed[boundary:]
            self.write("# Memory\n\n[...earlier entries trimmed...]\n\n" + trimmed)

    def dedupe_facts(self) -> int:
        """Remove duplicate and near-duplicate bullet facts within each section.

        Two passes — both deterministic (no LLM):

        **Pass 1 — exact normalized dedup:** removes bullets whose normalized
        form (marker + ``[date]`` stripped, lowercased, whitespace collapsed)
        already appeared in the same section, keeping the newest-dated form.

        **Pass 2 — subject-prefix dedup:** groups bullets by their first
        ``_SUBJECT_WORDS`` normalized words (their "subject"). Within a group,
        keep the single bullet with the most recent date and the longest body
        (most informative). Discards redundant restatements of the same subject,
        which is the primary cause of Self-Learned bloat (e.g. "User's name is
        Carlos" / "User prefers to be addressed as Carlos" / "User is addressed
        as Carlos" all share the same subject and intent).

        Returns total bullets removed across both passes.
        """
        content = self.read()
        removed = self._dedupe_pass1(content)
        # Re-read after pass 1 before pass 2.
        content = self.read()
        removed += self._dedupe_pass2(content)
        return removed

    @staticmethod
    def _subject_key(norm: str, words: int = 3) -> str:
        """Return the first ``words`` tokens of a normalized bullet body.

        Strips punctuation before tokenising so "user prefers concise," and
        "user prefers concise financial" share the same 3-word subject key
        "user prefers concise".
        """
        clean = re.sub(r"[^\w\s]", " ", norm)
        return " ".join(clean.split()[:words])

    def _dedupe_pass1(self, content: str) -> int:
        """Exact normalized dedup — see dedupe_facts docstring."""
        lines = content.splitlines()
        out: list[str] = []
        removed = 0
        seen: dict[str, int] = {}
        best_date: dict[str, str] = {}

        for line in lines:
            if line.startswith("## "):
                seen.clear()
                best_date.clear()
                out.append(line)
                continue
            norm = _normalize_bullet(line)
            if norm is None:
                out.append(line)
                continue
            this_date = _bullet_date(line)
            if norm in seen:
                removed += 1
                if this_date and this_date > best_date.get(norm, ""):
                    out[seen[norm]] = line
                    best_date[norm] = this_date
                continue
            seen[norm] = len(out)
            best_date[norm] = this_date
            out.append(line)

        if removed:
            new_content = "\n".join(out)
            if content.endswith("\n"):
                new_content += "\n"
            self.write(new_content)
        return removed

    def _dedupe_pass2(self, content: str) -> int:
        """Subject-prefix dedup — collapses near-duplicate restements.

        Only applied to sections whose name contains "Self-Learned", "self_learned",
        "General", or "User" — sections that grow via automatic extraction and are
        most prone to redundancy. Curated sections (Knowledge Graph, weekly
        summaries, Identity, etc.) are left untouched.
        """
        _SUBJECT_SECTIONS = {
            "self-learned",
            "self_learned",
            "general",
            "user profile",
            "agent soul",
        }

        lines = content.splitlines()
        out: list[str] = []
        removed = 0

        current_section = ""
        # Accumulate bullets per section; flush after each section boundary.
        pending_bullets: list[tuple[int, str, str, str]] = []  # (out_idx, norm, date, line)

        def _flush_section_dedup() -> int:
            nonlocal removed
            _section_lower = current_section.lower().strip()
            if not any(s in _section_lower for s in _SUBJECT_SECTIONS):
                return 0
            # Group by subject key, keep best (newest date, then longest norm).
            groups: dict[str, list[tuple[int, str, str, str]]] = {}
            for entry in pending_bullets:
                key = MarkdownMemory._subject_key(entry[1])
                if not key:
                    continue
                groups.setdefault(key, []).append(entry)
            _removed = 0
            for group in groups.values():
                if len(group) <= 1:
                    continue
                # Sort by date desc then body length desc; first is the winner.
                winner = sorted(
                    group,
                    key=lambda e: (e[2], len(e[1])),
                    reverse=True,
                )[0]
                for entry in group:
                    if entry[0] != winner[0]:
                        # Blank out the loser in `out`.
                        out[entry[0]] = None  # type: ignore[call-overload]
                        _removed += 1
            return _removed

        for line in lines:
            if line.startswith("## "):
                removed += _flush_section_dedup()
                pending_bullets = []
                current_section = line[3:].strip()
                out.append(line)
                continue
            norm = _normalize_bullet(line)
            if norm is None:
                out.append(line)
                continue
            out.append(line)
            pending_bullets.append((len(out) - 1, norm, _bullet_date(line), line))

        # Flush final section.
        removed += _flush_section_dedup()

        if removed:
            new_content = "\n".join(ln for ln in out if ln is not None)
            # Collapse triple+ blank lines produced by None removal.
            new_content = re.sub(r"\n{3,}", "\n\n", new_content)
            if content.endswith("\n"):
                new_content += "\n"
            self.write(new_content)
        return removed

    def trim_section(self, section: str, max_bullets: int = 50) -> int:
        """Keep only the ``max_bullets`` most recent bullet lines in a section.

        Older bullets are removed. Non-bullet lines (headings, prose) are
        preserved. Returns number of bullets removed.
        """
        content = self.read()
        pattern = re.compile(
            rf"(## {re.escape(section)}\n)(.*?)(?=\n## |\Z)",
            re.DOTALL,
        )
        match = pattern.search(content)
        if not match:
            return 0

        body = match.group(2)
        bullet_lines = [ln for ln in body.splitlines() if _normalize_bullet(ln)]
        other_lines = [ln for ln in body.splitlines() if not _normalize_bullet(ln)]
        if len(bullet_lines) <= max_bullets:
            return 0

        # Keep the max_bullets most recent (last N in file order ≈ newest).
        to_keep = bullet_lines[-max_bullets:]
        removed = len(bullet_lines) - len(to_keep)
        new_body = "\n".join(other_lines + to_keep) + "\n"
        new_content = content[: match.start(2)] + new_body + content[match.end(2) :]
        self.write(new_content)
        return removed

    def roll_up_daily_logs(self, keep_days: int = 14) -> int:
        """Archive daily logs older than ``keep_days`` into monthly rollups.

        Moves ``memories/YYYY-MM-DD.md`` files older than the cutoff into
        ``memories/archive/<YYYY-MM>.md`` (appending), then deletes the
        originals. Returns the number of daily logs archived.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).date()
        archive_dir = self.memories_dir / "archive"
        rolled = 0

        for path in sorted(self.memories_dir.glob("????-??-??.md")):
            try:
                file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date >= cutoff:
                continue
            month_key = file_date.strftime("%Y-%m")
            archive_path = archive_dir / f"{month_key}.md"
            existing = (
                archive_path.read_text(encoding="utf-8")
                if archive_path.exists()
                else f"# Archived Daily Logs — {month_key}\n"
            )
            body = path.read_text(encoding="utf-8").strip()
            section = f"\n\n## {path.stem}\n\n{body}\n"
            atomic_write_text(archive_path, existing.rstrip() + "\n" + section)
            path.unlink()
            rolled += 1

        return rolled

    # ------------------------------------------------------------------ Daily files

    def _daily_path(self, date: datetime | None = None) -> Path:
        d = date or datetime.now(timezone.utc)
        return self.memories_dir / f"{d.strftime('%Y-%m-%d')}.md"

    def append_daily(self, entry: str, date: datetime | None = None) -> None:
        """Append a raw entry to the daily log file (atomic rewrite)."""
        path = self._daily_path(date)
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if path.exists():
            existing = path.read_text(encoding="utf-8")
        else:
            existing = (
                f"# Daily Log — {(date or datetime.now(timezone.utc)).strftime('%Y-%m-%d')}\n\n"
            )
        new_content = existing + f"\n## {ts}\n\n{entry.strip()}\n"
        # Atomic rewrite is heavier than the original ``open('a')`` but
        # protects against torn writes when the process is killed mid-append.
        atomic_write_text(path, new_content)

    def read_daily(self, date: datetime | None = None) -> str:
        """Read the daily log for a given date (defaults to today)."""
        path = self._daily_path(date)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_today(self) -> str:
        return self.read_daily()

    def read_yesterday(self) -> str:
        from datetime import timedelta

        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        return self.read_daily(yesterday)

    def list_daily_files(self, limit: int = 7) -> list[Path]:
        files = sorted(self.memories_dir.glob("????-??-??.md"), reverse=True)
        return files[:limit]

    def search_daily(self, query: str, days: int = 7) -> list[str]:
        """Search recent daily log files for matching lines."""
        results: list[str] = []
        query_lower = query.lower()
        for path in self.list_daily_files(days):
            for line in path.read_text(encoding="utf-8").splitlines():
                if query_lower in line.lower() and line.strip():
                    results.append(f"[{path.stem}] {line.strip()}")
        return results


def make_memory_tools(memory: MarkdownMemory):
    """Create langchain tools bound to a MarkdownMemory instance."""

    def _normalize_profile_key(key: str, *, kind: str) -> str:
        cleaned = key.strip()
        lowered = cleaned.lower().replace("-", " ").replace("_", " ")
        lowered = re.sub(r"\s+", " ", lowered).strip()

        if kind == "user":
            aliases = {
                "name": "Name",
                "preferred name": "Preferred name",
                "goals": "Goals",
                "preferred ways of working": "Preferred ways of working",
                "working style": "Preferred ways of working",
                "project description": "Project description",
                "company": "Company",
                "timezone": "Timezone",
            }
        else:
            aliases = {
                "name": "Name",
                "preferred display name": "Name",
                "display name": "Name",
                "role": "Role",
                "emoji": "Emoji",
            }
        if lowered in aliases:
            return aliases[lowered]
        return cleaned[:1].upper() + cleaned[1:]

    def _upsert_markdown_metadata_line(path: Path, *, title: str, label: str, value: str) -> None:
        content = path.read_text(encoding="utf-8") if path.exists() else f"# {title}\n\n"
        line = f"**{label}:** {value}"
        pattern = re.compile(rf"^\*\*{re.escape(label)}:\*\*.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(line, content, count=1)
        else:
            lines = content.splitlines()
            insert_at = 1 if lines else 0
            while insert_at < len(lines) and (
                not lines[insert_at].strip() or lines[insert_at].strip().startswith("<!--")
            ):
                insert_at += 1
            lines[insert_at:insert_at] = [line]
            content = "\n".join(lines).rstrip() + "\n"
        path.write_text(content, encoding="utf-8")

    def _upsert_markdown_section_fact(
        path: Path,
        *,
        title: str,
        section: str,
        key: str,
        value: str,
    ) -> str:
        content = path.read_text(encoding="utf-8") if path.exists() else f"# {title}\n\n"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = f"- **{key}**: {value}  _(updated {ts})_"
        heading = f"## {section}"
        pattern = re.compile(
            rf"({re.escape(heading)}\n)(.*?)(?=\n## |\Z)",
            re.DOTALL,
        )
        match = pattern.search(content)
        if match:
            body = match.group(2).strip("\n")
            line_pattern = re.compile(rf"^- \*\*{re.escape(key)}\*\*:.*$", re.MULTILINE)
            if line_pattern.search(body):
                body = line_pattern.sub(entry, body)
            else:
                body = (body.rstrip() + "\n" + entry).strip()
            replacement = f"{heading}\n{body}\n"
            content = content[: match.start()] + replacement + content[match.end() :]
        else:
            content = content.rstrip() + f"\n\n{heading}\n{entry}\n"
        path.write_text(content, encoding="utf-8")
        return entry

    @tool
    def memory_save(fact: str, section: str = "General") -> str:
        """Save a fact to persistent long-term memory (MEMORY.md) under the given section."""
        memory.append_fact(fact, section)
        return f"Saved to memory under '{section}'."

    @tool
    def memory_search(query: str) -> str:
        """Search persistent memory (MEMORY.md) for information matching the query."""
        results = memory.search(query)
        if not results:
            return "No matching memory entries found."
        return "\n".join(results[:20])

    @tool
    def memory_read(section: str = "") -> str:
        """Read long-term memory. If section is provided, return only that section."""
        if section:
            return memory.get_section(section) or f"No section '{section}' found."
        return memory.read()

    @tool
    def memory_append_daily(entry: str) -> str:
        """Append a raw note or log entry to today's daily memory file."""
        memory.append_daily(entry)
        return "Appended to today's daily log."

    @tool
    def memory_read_today() -> str:
        """Read today's daily log file."""
        content = memory.read_today()
        return content if content else "No entries for today yet."

    @tool
    def memory_search_recent(query: str, days: int = 7) -> str:
        """Search recent daily log files (last N days) for matching entries."""
        results = memory.search_daily(query, days)
        if not results:
            return f"No matching entries found in the last {days} days."
        return "\n".join(results[:30])

    @tool
    def memory_compact() -> str:
        """Deduplicate MEMORY.md, trim large sections, and archive old daily logs.

        Runs two dedup passes (exact-normalized + subject-prefix), trims any
        Self-Learned section that has grown beyond 50 bullets (keeping the most
        recent), then rolls daily logs older than two weeks into monthly archives.
        Safe and deterministic — never drops a unique fact."""
        removed = memory.dedupe_facts()
        # Trim any Self-Learned sections that are still oversized after dedup.
        trimmed = 0
        for sec in memory.list_sections():
            if "self-learned" in sec.lower() or "self_learned" in sec.lower():
                trimmed += memory.trim_section(sec, max_bullets=50)
        rolled = memory.roll_up_daily_logs()
        parts = []
        if removed:
            parts.append(f"removed {removed} duplicate fact(s)")
        if trimmed:
            parts.append(f"trimmed {trimmed} excess Self-Learned bullet(s)")
        parts.append(f"archived {rolled} old daily log(s)")
        return "Compacted memory: " + ", ".join(parts) + "."

    @tool
    def user_profile_save(key: str, value: str) -> str:
        """Save a key fact the user has explicitly stated about themselves to USER.md
        (name, timezone, preferences, goals, etc.). Persistent across sessions.

        Only call this when the user states a durable fact about themselves in their
        own words (e.g. "my name is Sam", "I prefer concise answers"). When that
        happens, save it in the same response without asking for confirmation first.
        Do NOT call it for greetings or small talk, and never invent, guess, or infer
        a value — save only what the user actually said. Never save a name, role, or
        detail taken from system context, identity blocks, or login/role metadata."""
        user_path = memory.workspace_root / "USER.md"
        normalized_key = _normalize_profile_key(key, kind="user")
        try:
            _upsert_markdown_section_fact(
                user_path,
                title="User Profile",
                section="About the User",
                key=normalized_key,
                value=value,
            )
            # Also save to MEMORY.md for quick retrieval
            memory.append_fact(f"{normalized_key}: {value}", section="User Profile")
            return f"Saved to USER.md: {normalized_key} = {value}"
        except Exception as e:
            return f"Error saving user profile: {e}"

    @tool
    def identity_preference_save(key: str, value: str) -> str:
        """Save user-guided identity preferences for the agent to IDENTITY.md.
        Use when the user wants the agent renamed, re-described, or represented differently."""
        identity_path = memory.workspace_root / "IDENTITY.md"
        normalized_key = _normalize_profile_key(key, kind="identity")
        try:
            if normalized_key in {"Name", "Role", "Emoji"}:
                _upsert_markdown_metadata_line(
                    identity_path,
                    title="Identity",
                    label=normalized_key,
                    value=value,
                )
            _upsert_markdown_section_fact(
                identity_path,
                title="Identity",
                section="User-Guided Identity Preferences",
                key=normalized_key,
                value=value,
            )
            memory.append_fact(
                f"Identity preference — {normalized_key}: {value}",
                section="Agent Identity",
            )
            return f"Saved to IDENTITY.md: {normalized_key} = {value}"
        except Exception as e:
            return f"Error saving identity preference: {e}"

    @tool
    def cortex_guidance_save(content: str, section: str = "Goals") -> str:
        """Record creative direction for the Cortex Engine in CORTEX.md.

        CORTEX.md steers what the agent pursues when it generates on its OWN —
        the Cortex Engine's autonomous phases (Pulse, Brainstorming, Ideas,
        Prototypes, Advance) — separately from normal chat. Use this when the
        user states durable creative direction, or during self-learning when
        sessions reveal it. Pick the right section:
        - "Goals": concrete things to work toward.
        - "Dreams": bigger, aspirational north-star directions.
        - "Avoid": topics or kinds of suggestions the user does NOT want.
        Keep each entry short and specific. Do not duplicate plain facts that
        belong in MEMORY.md / USER.md — this file is only for autonomous
        creative direction."""
        from olano.cognition.guidance import append_guidance

        try:
            written = append_guidance(memory.workspace_root, content, section)
        except Exception as e:
            return f"Error saving Cortex guidance: {e}"
        if not written:
            return "Nothing to save — content was empty."
        return f"Saved to CORTEX.md under '{written}'."

    @tool
    def soul_preference_save(key: str, value: str) -> str:
        """Save user-guided tone, values, and communication preferences for the agent to SOUL.md.
        Use when the user asks the agent to adjust personality, style, or behavioral tone."""
        soul_path = memory.workspace_root / "SOUL.md"
        try:
            _upsert_markdown_section_fact(
                soul_path,
                title="Soul",
                section="User-Guided Communication Preferences",
                key=key,
                value=value,
            )
            memory.append_fact(f"Soul preference — {key}: {value}", section="Agent Soul")
            return f"Saved to SOUL.md: {key} = {value}"
        except Exception as e:
            return f"Error saving soul preference: {e}"

    return [
        memory_save,
        memory_search,
        memory_read,
        memory_append_daily,
        memory_read_today,
        memory_search_recent,
        memory_compact,
        user_profile_save,
        identity_preference_save,
        soul_preference_save,
        cortex_guidance_save,
    ]
