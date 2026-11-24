"""Cortex items — the structured store behind ideas, brainstorms, prototypes, and
projects.

Instead of three ever-growing flat files (IDEAS.md / BRAINSTORMING.md /
PROTOTYPING.md), each idea/brainstorm/prototype/project is a **folder** holding
its full detail (and any supporting files the agent wants to create), tracked in
a single JSON index. The flat ``*.md`` files become short, auto-generated index
tables that link to the detail folders — readable in Files/git, never bloated.

Layout (workspace-relative):
    ideas/<slug>/idea.md
    brainstorming/<slug>/brainstorm.md
    prototypes/<slug>/README.md           (+ scripts/data the agent creates)
    projects/<slug>/{README,PLAN,STATUS,JOURNAL}.md
Index: <workspace>/.olano/state/cortex/items.json

Status lifecycle: ``active`` → ``exploring`` → ``promoted`` / ``discarded``.
The index is the source of truth; ``cognition`` runners + the dashboard read it.

The funnel runs **brainstorm → idea → prototype → project** (:data:`FUNNEL`).
Each item may carry a ``rating`` (``{"score": 1..10, "rationale", "dimensions",
"recommendation", "next_step", "rated_at"}``) written by the Cortex triage
phase or a human; :func:`set_rating` is the one writer so the shape stays
uniform. Promotion links the stages: the source item becomes ``promoted`` with
``links.promoted_to`` naming the new item's folder, and the new item carries
``links.from_item`` / ``links.from_path``.

**Integrity.** Every read-modify-write holds a per-index lock
(:func:`_index_lock`) and every save is atomic (temp file + rename), because
LangGraph runs parallel tool calls concurrently and five ``cortex_edit`` calls
in one model turn used to interleave plain ``write_text`` calls: the shorter
document landed over the longer one and the longer one's tail survived, so the
file read as a valid object followed by a duplicated ``]}`` fragment. The
loader then swallowed the parse error and returned an empty index, so every
tool and the dashboard reported zero items while the file visibly held them,
and the next ``add_item`` would have saved a fresh index over the recoverable
one. :func:`_load` now recovers that exact shape (the first complete document)
behind a backup copy, and refuses (:class:`CortexIndexError`) anything it
cannot recover rather than reading it as empty. Nothing writes over an index
that failed to load.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

from olano.config.workspace import WORKSPACE_STATE_DIRNAME

logger = logging.getLogger(__name__)


class CortexIndexError(RuntimeError):
    """``items.json`` exists but cannot be read as an index.

    Raised instead of reading the file as empty: an empty answer here is
    indistinguishable from "no items", and the next writer would have saved a
    fresh index over the one that still holds the data. The message names the
    file so the operator can repair it; the caller must not write.
    """


# kind → (folder, detail filename, mirror filename, human label)
KINDS: dict[str, dict[str, str]] = {
    "idea": {"dir": "ideas", "detail": "idea.md", "mirror": "IDEAS.md", "label": "Idea"},
    "brainstorm": {
        "dir": "brainstorming",
        "detail": "brainstorm.md",
        "mirror": "BRAINSTORMING.md",
        "label": "Brainstorm",
    },
    "prototype": {
        "dir": "prototypes",
        "detail": "README.md",
        "mirror": "PROTOTYPING.md",
        "label": "Prototype",
    },
    "project": {
        "dir": "projects",
        "detail": "README.md",
        "mirror": "PROJECTS.md",
        "label": "Project",
    },
}

STATUSES: tuple[str, ...] = ("active", "exploring", "promoted", "discarded")

#: The staged funnel, in order. ``next_kind(kind)`` is the stage after ``kind``;
#: a project is the end of the line.
FUNNEL: tuple[str, ...] = ("brainstorm", "idea", "prototype", "project")

#: ``rating.recommendation`` vocabulary written by the triage phase.
RECOMMENDATIONS: tuple[str, ...] = ("promote", "execute", "keep", "explore", "discard")

#: Score bounds for ``rating.score``.
RATING_MIN, RATING_MAX = 1, 10


def next_kind(kind: str) -> str | None:
    """The stage after ``kind`` in :data:`FUNNEL`, or ``None`` for a project."""
    try:
        idx = FUNNEL.index(kind)
    except ValueError:
        return None
    return FUNNEL[idx + 1] if idx + 1 < len(FUNNEL) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "cortex" / "items.json"


# ───────────────────────────── per-index locking ────────────────────────────
# One re-entrant lock per index file, process-wide. The runtime is one process
# (dashboard routes, chat tools and Cortex workers all run in it), so this is
# the whole story for the race described in the module docstring; the one
# other writer, the operator's CLI, is a human typing one command. Re-entrant
# because ``upsert_item`` → ``update_item`` → ``_load`` nest.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _index_lock(workspace_root: Path) -> threading.RLock:
    key = str(_index_path(workspace_root).absolute())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def slugify(title: str, *, max_len: int = 48) -> str:
    """Kebab-case slug from a title (safe folder name)."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].strip("-")) or "untitled"


# ─────────────────────────────── de-duplication ─────────────────────────────
# Default similarity at which two titles are treated as the same item. Conservative
# on purpose: exact/near-exact dups (punctuation, spacing, casing) collapse via the
# normalized-equality short-circuit; this fuzzy margin only catches very close
# rewordings, so genuinely distinct short titles ("Weekly Review" vs "Weekly
# Preview", ~0.89) stay separate.
SIMILAR_TITLE_THRESHOLD = 0.90


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for title comparison."""
    s = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


#: Content similarity (title + summary, see :func:`content_similarity`) at or
#: above which a candidate is the SAME item as an existing one of any kind or
#: status. Lower than the title threshold on purpose: it compares what the
#: item is about, not how it was named, so "Solar lumber kiln" and "Dry
#: lumber with passive solar heat" meet here even though their titles do not.
SIMILAR_CONTENT_THRESHOLD = 0.5

#: Below this the lexical signal says "different" and the semantic check is
#: not consulted at all; between the two, embeddings decide.
_SEMANTIC_CHECK_FLOOR = 0.25
#: Cosine similarity (fastembed, L2-normalised vectors) that counts as the
#: same item when the lexical score sits in the grey band.
SEMANTIC_DUPLICATE_THRESHOLD = 0.86

_STOPWORDS: frozenset[str] = frozenset(
    """a an the and or of to for in on at by with from into over under as is are be
    was were it its this that these those we you they our your their us them i my me
    how what why when where which who use using make making build building create new
    about via per more most less than then so if not no yes up down out off all any
    each every some such own same other both also just only very can could should
    would will may might do does did done get got has have had""".split()
)

_SUFFIXES: tuple[str, ...] = ("ing", "ies", "es", "ed", "s")


def _stem(word: str) -> str:
    """A crude stem: ``invoices`` and ``invoice``, ``drying`` and ``dry`` meet.
    Not a stemmer; just enough that a plural or a participle is not a new word."""
    if word.endswith("ies") and len(word) > 5:
        return word[:-3] + "y"
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _content_tokens(text: str) -> set[str]:
    return {
        _stem(t)
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def _tokens_match(a: str, b: str) -> bool:
    """Equal stems, or one a prefix of the other (``chas`` / ``chase``)."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


def _overlap(tokens_a: set[str], tokens_b: set[str]) -> int:
    return sum(1 for a in tokens_a if any(_tokens_match(a, b) for b in tokens_b))


def content_similarity(title_a: str, summary_a: str, title_b: str, summary_b: str) -> float:
    """How much two items are about the same thing, 0..1.

    Token overlap (Jaccard, or containment for a short item inside a long
    one) of title + summary, on crude stems; a near-identical title counts on
    its own only at the title threshold, because two short titles that share
    one word ("Idea A" / "Idea B", "Weekly Review" / "Weekly Preview") are
    not the same item. A mild bonus when title and content agree.
    Deterministic and free, so it is the gate every generation path runs.
    """
    title_ratio = _title_similarity(title_a, title_b)
    tokens_a = _content_tokens(f"{title_a} {summary_a}")
    tokens_b = _content_tokens(f"{title_b} {summary_b}")
    overlap = 0.0
    if tokens_a and tokens_b:
        inter = _overlap(tokens_a, tokens_b)
        union = len(tokens_a) + len(tokens_b) - inter
        smaller = min(len(tokens_a), len(tokens_b))
        # Evidence guards: one shared word in two tiny sets proves nothing.
        jaccard = inter / union if union >= 4 else 0.0
        containment = inter / smaller if smaller >= 3 else 0.0
        overlap = max(jaccard, 0.8 * containment)
    score = max(title_ratio if title_ratio >= SIMILAR_TITLE_THRESHOLD else 0.0, overlap)
    if title_ratio >= 0.6 and overlap >= 0.3:
        score = min(1.0, max(score, overlap + 0.1))
    return score


_SEMANTIC_DISABLED = False


def _semantic_similarity(text_a: str, text_b: str) -> float | None:
    """Embedding cosine between two item texts, or ``None`` when unavailable.

    Off with ``LIONCLAW_CORTEX_SEMANTIC_DEDUP=0`` (the test suite pins it so
    no test downloads an encoder); disabled for the process after the first
    failure so a missing model costs one warning, never one per candidate.
    """
    global _SEMANTIC_DISABLED
    import os

    if _SEMANTIC_DISABLED or os.environ.get("LIONCLAW_CORTEX_SEMANTIC_DEDUP", "1") == "0":
        return None
    try:
        from olano.memory.embeddings import cosine_similarity, embed_texts

        vectors = embed_texts([text_a, text_b])
        return float(cosine_similarity(vectors[0], vectors[1]))
    except Exception as exc:
        _SEMANTIC_DISABLED = True
        logger.warning("cortex_items: semantic dedup unavailable (%s); lexical only", exc)
        return None


def find_similar_any(
    workspace_root: Path,
    title: str,
    summary: str = "",
    *,
    threshold: float = SIMILAR_CONTENT_THRESHOLD,
    exclude_id: str | None = None,
) -> tuple[dict[str, Any], float] | None:
    """The existing item, of ANY kind and ANY status, that a candidate repeats.

    Returns ``(item, score)`` for the strongest match at or above
    ``threshold``, else ``None``. Every stage and every status counts: an idea
    that restates a running project, a brainstorm that reopens a discarded
    one, a prototype of an idea already built. The lexical score decides
    outright above the threshold; in the grey band between
    :data:`_SEMANTIC_CHECK_FLOOR` and the threshold an embedding cosine at or
    above :data:`SEMANTIC_DUPLICATE_THRESHOLD` also counts, so a rewording
    with no words in common is still caught when an encoder is available.
    Ties prefer the furthest-along item (project > prototype > idea >
    brainstorm), then the most recently updated.
    """
    if not _normalize_title(title) and not _content_tokens(summary):
        return None
    rank = {k: i for i, k in enumerate(FUNNEL)}
    best: tuple[dict[str, Any], float] | None = None
    grey: list[tuple[dict[str, Any], float]] = []
    for it in _load(workspace_root)["items"]:
        if exclude_id and it.get("id") == exclude_id:
            continue
        score = content_similarity(
            title, summary, str(it.get("title") or ""), str(it.get("summary") or "")
        )
        if score >= threshold:
            if best is None or (
                score,
                rank.get(str(it.get("kind")), -1),
                str(it.get("updated_at") or ""),
            ) > (
                best[1],
                rank.get(str(best[0].get("kind")), -1),
                str(best[0].get("updated_at") or ""),
            ):
                best = (dict(it), score)
        elif score >= _SEMANTIC_CHECK_FLOOR:
            grey.append((dict(it), score))
    if best is not None:
        return best
    if not grey:
        return None
    candidate_text = f"{title}. {summary}".strip()
    # Closest lexical first; one embedding call per grey item, bounded.
    grey.sort(key=lambda pair: -pair[1])
    for it, _lexical in grey[:12]:
        cosine = _semantic_similarity(
            candidate_text, f"{it.get('title') or ''}. {it.get('summary') or ''}".strip()
        )
        if cosine is None:
            return None
        if cosine >= SEMANTIC_DUPLICATE_THRESHOLD:
            return it, cosine
    return None


def portfolio_digest(workspace_root: Path, *, limit: int = 200) -> str:
    """Every item the agent has, one line each, grouped by stage, for a
    generation worker's context: what exists must not be produced again.

    Promoted items are listed (they became something further along) and
    discarded ones too (the owner or the reviewer rejected them; they stay
    rejected). Newest first within a stage; ``limit`` lines in all.
    """
    items = list_items(workspace_root, include_discarded=True)
    if not items:
        return "Nothing captured yet in any stage."
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in FUNNEL}
    for it in items:
        by_kind.setdefault(str(it.get("kind")), []).append(it)
    lines = [
        "EVERYTHING ALREADY CAPTURED, every stage and status. Do not repeat, rephrase, "
        "narrow, widen or recombine any of these into a 'new' item; a discarded one stays "
        "discarded; a promoted one lives on at the next stage. New means a genuinely "
        "different idea, not a variant."
    ]
    budget = max(0, limit)
    for kind in by_kind:
        rows = by_kind.get(kind) or []
        if not rows:
            continue
        label = KINDS.get(kind, {}).get("label", kind.title())
        lines.append(f"{label}s ({len(rows)}):")
        for it in rows:
            if budget <= 0:
                lines.append("  (list truncated)")
                return "\n".join(lines)
            status = str(it.get("status") or "")
            flag = {"discarded": " [DISCARDED: do not resurrect]", "promoted": " [promoted]"}.get(
                status, ""
            )
            summary = " ".join(str(it.get("summary") or "").split())[:140]
            lines.append(f"  - {it.get('title')}{flag}" + (f": {summary}" if summary else ""))
            budget -= 1
    return "\n".join(lines)


def find_similar(
    workspace_root: Path,
    kind: str,
    title: str,
    *,
    threshold: float = SIMILAR_TITLE_THRESHOLD,
    exclude_id: str | None = None,
) -> dict[str, Any] | None:
    """Return an existing NON-discarded item of ``kind`` whose title is identical or
    near-identical to ``title`` (normalized similarity ≥ ``threshold``), else None.

    Used by the generation runners to avoid creating duplicate ideas / brainstorms
    / prototypes — when a match is found they improve it instead of adding a near-copy.
    """
    norm = _normalize_title(title)
    if not norm:
        return None
    best: dict[str, Any] | None = None
    best_ratio = 0.0
    for it in _load(workspace_root)["items"]:
        if it.get("kind") != kind or it.get("status") == "discarded":
            continue
        if exclude_id and it.get("id") == exclude_id:
            continue
        other = _normalize_title(it.get("title", ""))
        if not other:
            continue
        if other == norm:
            return dict(it)  # exact normalized match — unambiguous
        ratio = difflib.SequenceMatcher(None, norm, other).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, dict(it)
    return best if best_ratio >= threshold else None


# ───────────────────────────────── index I/O ────────────────────────────────


def _is_index(data: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(data, dict) and isinstance(data.get("items"), list)


def _recover_first_document(raw: str) -> dict[str, Any] | None:
    """The first complete JSON document in ``raw``, when it is an index.

    This is the shape a torn write leaves behind (a whole document followed by
    the tail of a longer one), so recovering it loses at most the edits of the
    writer that lost the race, never the index.
    """
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw.lstrip())
    except ValueError:
        return None
    return dict(obj) if _is_index(obj) else None


def _backup_corrupt(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    shutil.copy2(path, backup)
    return backup


def _load(workspace_root: Path) -> dict[str, Any]:
    """Read the index. Never answers "empty" for a file that holds data.

    A missing file is an empty index. An unparseable file is recovered when it
    is a torn write (see :func:`_recover_first_document`): the original is kept
    beside it as ``items.json.corrupt-<stamp>`` and the recovered document is
    written back, with one warning in the log. Anything else raises
    :class:`CortexIndexError` so the caller surfaces it instead of reading
    nothing and, worse, writing a fresh index over it.
    """
    path = _index_path(workspace_root)
    with _index_lock(workspace_root):
        if not path.exists():
            return {"items": []}
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            parse_error: Exception | None = exc
            data = None
        else:
            parse_error = None
        if _is_index(data):
            return data
        if not raw.strip():
            # A zero-byte file holds nothing to lose (a crash between truncate
            # and write under the old non-atomic save). Keep it for the record
            # and start fresh.
            backup = _backup_corrupt(path)
            logger.warning("cortex_items: empty index %s, kept as %s, starting fresh", path, backup)
            return {"items": []}
        recovered = _recover_first_document(raw)
        if recovered is not None:
            backup = _backup_corrupt(path)
            _save(workspace_root, recovered)
            logger.warning(
                "cortex_items: recovered a torn index %s (%d item(s)); the original is kept as %s",
                path,
                len(recovered["items"]),
                backup,
            )
            return recovered
        reason = str(parse_error) if parse_error is not None else "not an index object"
        raise CortexIndexError(
            f"The Cortex index {path} is unreadable ({reason}). Nothing was changed; "
            "repair or move the file before adding or editing items."
        )


def _save(workspace_root: Path, data: dict[str, Any]) -> None:
    """Atomic write: temp file + rename, so a reader never sees a partial file
    and two writers can never leave a torn one. The lock makes the surrounding
    read-modify-write exclusive; this makes the write itself indivisible."""
    from olano.memory.markdown_memory import atomic_write_text

    path = _index_path(workspace_root)
    with _index_lock(workspace_root):
        atomic_write_text(path, json.dumps(data, indent=2))


# ───────────────────────────────── public API ───────────────────────────────


def list_items(
    workspace_root: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    include_discarded: bool = True,
) -> list[dict[str, Any]]:
    """Return items (newest first), optionally filtered by kind/status."""
    items = _load(workspace_root)["items"]
    out = []
    for it in items:
        if kind and it.get("kind") != kind:
            continue
        if status and it.get("status") != status:
            continue
        if not include_discarded and it.get("status") == "discarded":
            continue
        out.append(it)
    out.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return out


def get_item(workspace_root: Path, item_id: str) -> dict[str, Any] | None:
    for it in _load(workspace_root)["items"]:
        if it.get("id") == item_id:
            return dict(it)
    return None


def preview_for(workspace_root: Path, item: dict[str, Any], *, max_len: int = 240) -> str:
    """A short human description for an item card.

    Prefers the stored ``summary``; when empty (older items, or workers that
    didn't emit one), falls back to the first real paragraph of the item's
    detail file (skipping the title heading and markdown noise). Returns "" when
    nothing readable is available — the UI then shows a neutral placeholder.
    """
    summary = (item.get("summary") or "").strip()
    if summary:
        return summary[:max_len]
    detail_file = item.get("detail_file") or ""
    if not detail_file:
        return ""
    path = workspace_root / detail_file
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):  # skip headings
            continue
        if line.startswith(("-", "*", ">", "|", "```")):  # skip lists/quotes/tables/code
            continue
        # Strip light inline markdown so the card stays clean.
        line = re.sub(r"[*_`]", "", line)
        return line[:max_len]
    return ""


def resolve_detail(workspace_root: Path, item: dict[str, Any]) -> tuple[str, list[str]]:
    """Best available detail markdown + the folder files for an item.

    Robust to items whose canonical ``detail_file`` was never written (e.g. a
    prototype registered against a folder the worker mkdir'd but never populated,
    or an item created with an empty ``detail``). Resolution order:

      1. the item's ``detail_file``
      2. the first non-empty ``*.md`` in its own folder (README.md first)
      3. the same for the project it was ``promoted_to`` (so a promoted prototype
         shows the project it became)
      4. a synthetic ``# title`` + ``summary``

    Files are gathered from the item's own folder and, when promoted, the target
    project's folder. Guarantees the detail drawer is never blank when there is
    anything meaningful to show.
    """

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        except OSError:
            return ""

    def _first_md(d: Path) -> str:
        if not d.is_dir():
            return ""
        cands = sorted(
            d.glob("*.md"), key=lambda p: (p.name.lower() != "readme.md", p.name.lower())
        )
        for c in cands:
            txt = _read(c)
            if txt.strip():
                return txt
        return ""

    # Guard empty path/promoted_to so we never resolve to the workspace ROOT and
    # rglob the entire tree (surfacing unrelated files as this item's detail).
    rel = (item.get("path") or "").strip()
    folder = workspace_root / rel if rel else None
    promoted_to = ((item.get("links") or {}).get("promoted_to") or "").strip()
    pfolder = workspace_root / promoted_to if promoted_to else None

    detail = _read(workspace_root / (item.get("detail_file") or ""))
    if not detail.strip() and folder is not None:
        detail = _first_md(folder)
    if not detail.strip() and pfolder is not None:
        detail = _first_md(pfolder)
    if not detail.strip():
        summary = (item.get("summary") or "").strip()
        if summary:
            detail = f"# {item.get('title') or 'Untitled'}\n\n{summary}"

    files: list[str] = []
    for base in (folder, pfolder):
        if base is not None and base.is_dir():
            files.extend(str(p.relative_to(workspace_root)) for p in base.rglob("*") if p.is_file())
    return detail, sorted(set(files))


def counts_by_kind(workspace_root: Path) -> dict[str, int]:
    """Active (non-discarded) item count per kind — for the dashboard tab badges."""
    out = {k: 0 for k in KINDS}
    for it in _load(workspace_root)["items"]:
        if it.get("status") == "discarded":
            continue
        k = it.get("kind")
        if k in out:
            out[k] += 1
    return out


def _unique_slug(
    workspace_root: Path, kind: str, base_slug: str, *, adopt_folder: bool = False
) -> str:
    """Avoid collisions with existing folders/index entries.

    ``adopt_folder`` registers a folder that already exists on disk under the
    requested slug (a worker scaffolded it before the item existed); only an
    index entry already holding that slug then forces a suffix. Without it a
    prototype the worker wrote to ``prototypes/solar-kiln/`` was registered
    as ``solar-kiln-2`` against an empty folder the index had just created.
    """
    folder = workspace_root / KINDS[kind]["dir"]
    existing_ids = {
        it.get("slug") for it in _load(workspace_root)["items"] if it.get("kind") == kind
    }
    slug = base_slug
    n = 2
    while (not adopt_folder and (folder / slug).exists()) or slug in existing_ids:
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


def add_item(
    workspace_root: Path,
    *,
    kind: str,
    title: str,
    summary: str = "",
    detail: str = "",
    tags: list[str] | None = None,
    source: str = "autonomous",
    status: str = "active",
    slug: str | None = None,
    write_detail: bool = True,
    links: dict[str, str] | None = None,
    adopt_folder: bool = False,
) -> dict[str, Any]:
    """Create an item folder (+ detail file) and register it in the index.

    Returns the item dict. Regenerates the kind's markdown mirror. When
    ``write_detail`` is False the folder is assumed to already hold its files
    (e.g. a prototype the worker scaffolded itself) — only the detail file is
    skipped, not the registration. ``adopt_folder`` registers that existing
    folder under its own slug instead of minting a suffixed sibling.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown cortex kind: {kind!r}. Known: {tuple(KINDS)}")
    title = (title or "").strip() or "Untitled"
    base_slug = slugify(slug or title)
    with _index_lock(workspace_root):
        # Fail before touching the disk when the index cannot be read: a fresh
        # index saved below would bury the one that still holds the data.
        data = _load(workspace_root)
        slug = _unique_slug(workspace_root, kind, base_slug, adopt_folder=adopt_folder)

        rel_dir = f"{KINDS[kind]['dir']}/{slug}"
        folder = workspace_root / rel_dir
        folder.mkdir(parents=True, exist_ok=True)

        if write_detail and detail:
            detail_path = folder / KINDS[kind]["detail"]
            header = f"# {title}\n\n" if not detail.lstrip().startswith("#") else ""
            detail_path.write_text(header + detail.strip() + "\n", encoding="utf-8")

        now = _now()
        item = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "slug": slug,
            "title": title,
            "status": status if status in STATUSES else "active",
            "summary": (summary or "").strip(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
            "path": rel_dir,
            "detail_file": f"{rel_dir}/{KINDS[kind]['detail']}",
            "source": source,
            "created_at": now,
            "updated_at": now,
            "links": links or {},
        }
        data["items"].append(item)
        _save(workspace_root, data)
        regenerate_mirror(workspace_root, kind)
    logger.info("cortex_items: added %s '%s' (%s)", kind, title, item["id"])
    return item


def upsert_item(
    workspace_root: Path,
    *,
    kind: str,
    title: str,
    summary: str = "",
    detail: str = "",
    tags: list[str] | None = None,
    source: str = "autonomous",
    threshold: float = SIMILAR_TITLE_THRESHOLD,
    existing: dict[str, Any] | None = None,
    **add_kwargs: Any,
) -> tuple[dict[str, Any], bool]:
    """Create an item, or — when a near-identical active item of the same kind
    already exists — IMPROVE that one instead of adding a duplicate.

    ``existing`` names the item to improve when the caller already found it
    (the generation runners match on content across the whole portfolio,
    which is wider than the title match this function runs on its own).

    Returns ``(item, created)``; ``created`` is False when an existing item was
    improved (a longer summary replaces a shorter one, a richer detail body is
    written to its detail file, tags merge, ``updated_at`` bumps) rather than a new
    near-copy added. Used by the autonomous generation runners so the funnel never
    emits two copies of the same idea/brainstorm.
    """
    with _index_lock(workspace_root):
        return _upsert_item_locked(
            workspace_root,
            kind=kind,
            title=title,
            summary=summary,
            detail=detail,
            tags=tags,
            source=source,
            threshold=threshold,
            existing=existing,
            **add_kwargs,
        )


def _upsert_item_locked(
    workspace_root: Path,
    *,
    kind: str,
    title: str,
    summary: str,
    detail: str,
    tags: list[str] | None,
    source: str,
    threshold: float,
    existing: dict[str, Any] | None = None,
    **add_kwargs: Any,
) -> tuple[dict[str, Any], bool]:
    if existing is None:
        existing = find_similar(workspace_root, kind, title, threshold=threshold)
    if existing is not None:
        updates: dict[str, Any] = {}
        incoming = (summary or "").strip()
        if incoming and len(incoming) > len((existing.get("summary") or "").strip()):
            updates["summary"] = incoming
        merged_tags = list(dict.fromkeys([*(existing.get("tags") or []), *(tags or [])]))
        if merged_tags != list(existing.get("tags") or []):
            updates["tags"] = merged_tags
        # Persist a richer regenerated detail body so the improvement isn't lost
        # (it otherwise only lands in summary/tags). Only when strictly longer than
        # what's on disk, so we never overwrite hand-developed detail with less.
        incoming_detail = (detail or "").strip()
        if incoming_detail:
            detail_file = existing.get("detail_file") or ""
            dpath = workspace_root / detail_file if detail_file else None
            current = ""
            if dpath is not None and dpath.is_file():
                try:
                    current = dpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    current = ""
            if dpath is not None and len(incoming_detail) > len(current.strip()):
                header = "" if incoming_detail.lstrip().startswith("#") else f"# {title}\n\n"
                dpath.parent.mkdir(parents=True, exist_ok=True)
                dpath.write_text(header + incoming_detail + "\n", encoding="utf-8")
        updated = update_item(workspace_root, str(existing.get("id", "")), **updates)
        logger.info(
            "cortex_items: improved existing %s '%s' (%s) instead of adding a duplicate",
            kind,
            existing.get("title"),
            existing.get("id"),
        )
        return (updated or existing), False
    item = add_item(
        workspace_root,
        kind=kind,
        title=title,
        summary=summary,
        detail=detail,
        tags=tags,
        source=source,
        **add_kwargs,
    )
    return item, True


def update_item(workspace_root: Path, item_id: str, **fields: Any) -> dict[str, Any] | None:
    with _index_lock(workspace_root):
        data = _load(workspace_root)
        found: dict[str, Any] | None = None
        for it in data["items"]:
            if it.get("id") == item_id:
                it.update({k: v for k, v in fields.items() if k not in ("id", "kind")})
                it["updated_at"] = _now()
                found = it
                break
        if found is None:
            return None
        _save(workspace_root, data)
        regenerate_mirror(workspace_root, found.get("kind", ""))
        return dict(found)


def set_status(workspace_root: Path, item_id: str, status: str) -> dict[str, Any] | None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}. Known: {STATUSES}")
    return update_item(workspace_root, item_id, status=status)


def set_statuses(workspace_root: Path, item_ids: list[str], status: str) -> list[dict[str, Any]]:
    """Set ``status`` on every item in ``item_ids`` in one read-modify-write.

    The bulk form of :func:`set_status`: one index load, one atomic save and one
    mirror regeneration per kind touched, so discarding forty brainstorms is not
    forty rewrites of the index. Unknown ids are skipped; returns the updated
    items in index order.
    """
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}. Known: {STATUSES}")
    wanted = {str(i) for i in item_ids if i}
    if not wanted:
        return []
    with _index_lock(workspace_root):
        data = _load(workspace_root)
        now = _now()
        changed: list[dict[str, Any]] = []
        for it in data["items"]:
            if it.get("id") in wanted:
                it["status"] = status
                it["updated_at"] = now
                changed.append(it)
        if not changed:
            return []
        _save(workspace_root, data)
        for kind in sorted({str(it.get("kind") or "") for it in changed}):
            regenerate_mirror(workspace_root, kind)
        return [dict(it) for it in changed]


def set_rating(
    workspace_root: Path,
    item_id: str,
    score: int | float,
    *,
    rationale: str = "",
    dimensions: dict[str, Any] | None = None,
    recommendation: str = "keep",
    next_step: str = "",
    rated_by: str = "cortex",
) -> dict[str, Any] | None:
    """Write an item's ``rating`` block (the one writer, so the shape is uniform).

    ``score`` is clamped to ``[RATING_MIN, RATING_MAX]``; an unknown
    ``recommendation`` reads as ``keep``. ``dimensions`` is free-form
    (``{"value": 8, "feasibility": 6, ...}``) and kept verbatim when it is a
    dict of numbers. Returns the updated item, or ``None`` when ``item_id`` is
    unknown.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    value = max(float(RATING_MIN), min(float(RATING_MAX), value))
    dims: dict[str, float] = {}
    if isinstance(dimensions, dict):
        for k, v in dimensions.items():
            try:
                dims[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    rec = str(recommendation or "").strip().lower()
    if rec not in RECOMMENDATIONS:
        rec = "keep"
    rating = {
        "score": round(value, 1),
        "rationale": (rationale or "").strip()[:600],
        "dimensions": dims,
        "recommendation": rec,
        "next_step": (next_step or "").strip()[:300],
        "rated_at": _now(),
        "rated_by": rated_by,
    }
    return update_item(workspace_root, item_id, rating=rating)


def rating_score(item: dict[str, Any]) -> float | None:
    """The item's numeric score, or ``None`` when it has never been rated."""
    rating = item.get("rating")
    if not isinstance(rating, dict) or rating.get("score") is None:
        return None
    try:
        return float(rating["score"])
    except (TypeError, ValueError):
        return None


def _file_count(workspace_root: Path, item: dict[str, Any]) -> int:
    rel = (item.get("path") or "").strip()
    if not rel:  # never count the whole workspace for a path-less item
        return 0
    folder = workspace_root / rel
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.rglob("*") if p.is_file())


def merge_duplicates(workspace_root: Path, kind: str | None = None) -> int:
    """Collapse EXACT-duplicate active items (same kind + same normalized title):
    keep the most-developed one, mark the rest ``discarded`` and link them to the
    keeper. Returns how many were discarded.

    Conservative on purpose — only exact normalized-title matches are merged (never
    fuzzy), so this can run autonomously (it's called at the start of the ``advance``
    phase) without ever losing genuinely distinct work. Discarded items stay on disk.
    """
    with _index_lock(workspace_root):
        return _merge_duplicates_locked(workspace_root, kind)


def _merge_duplicates_locked(workspace_root: Path, kind: str | None) -> int:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in _load(workspace_root)["items"]:
        if it.get("status") == "discarded":
            continue
        if kind and it.get("kind") != kind:
            continue
        norm = _normalize_title(it.get("title", ""))
        if not norm:
            continue
        groups.setdefault((str(it.get("kind", "")), norm), []).append(it)

    # Lifecycle rank — a promoted/exploring item must never be discarded in favour
    # of a fresher but less-advanced duplicate.
    status_rank = {"promoted": 3, "exploring": 2, "active": 1}

    discarded = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # Keep the most-developed: furthest-along status, then most files, then
        # longest summary, then oldest (most established).
        members.sort(
            key=lambda it: (
                -status_rank.get(str(it.get("status", "")), 0),
                -_file_count(workspace_root, it),
                -len(it.get("summary") or ""),
                it.get("created_at") or "",
            )
        )
        keeper = members[0]
        for dup in members[1:]:
            update_item(
                workspace_root,
                str(dup.get("id", "")),
                status="discarded",
                links={**(dup.get("links") or {}), "duplicate_of": str(keeper.get("id", ""))},
            )
            discarded += 1
        logger.info(
            "cortex_items: merged %d duplicate(s) of '%s' into %s",
            len(members) - 1,
            keeper.get("title"),
            keeper.get("id"),
        )
    return discarded


# ──────────────────────────────── md mirrors ────────────────────────────────

_MIRROR_MARK = "<!-- auto-generated by Cortex — edit the per-item detail files, not this table -->"


def regenerate_mirror(workspace_root: Path, kind: str) -> None:
    """Rewrite the lightweight ``*.md`` index table for a kind from the index."""
    if kind not in KINDS:
        return
    meta = KINDS[kind]
    items = [it for it in _load(workspace_root)["items"] if it.get("kind") == kind]
    items.sort(key=lambda i: i.get("created_at", ""), reverse=True)

    def _rows(status_filter) -> str:
        rows = [it for it in items if it.get("status") in status_filter]
        if not rows:
            return "_None yet._\n"
        out = ["| Title | Status | Score | Detail | Updated |", "|---|---|---|---|---|"]
        for it in rows:
            title = (it.get("title") or "").replace("|", "\\|")
            detail = it.get("detail_file") or it.get("path") or ""
            updated = (it.get("updated_at") or "")[:10]
            score = rating_score(it)
            score_cell = f"{score:g}/10" if score is not None else "–"
            out.append(f"| {title} | {it.get('status')} | {score_cell} | `{detail}` | {updated} |")
        return "\n".join(out) + "\n"

    body = (
        f"# {meta['label']}s\n\n{_MIRROR_MARK}\n\n"
        f"Full detail lives in `{meta['dir']}/<slug>/`. Manage these from the dashboard "
        f"Cortex view (discard · explore more · chat · promote to project).\n\n"
        f"## Active\n\n{_rows(('active', 'exploring'))}\n"
        f"## Promoted\n\n{_rows(('promoted',))}\n"
        f"## Discarded\n\n{_rows(('discarded',))}\n"
    )
    from olano.memory.markdown_memory import atomic_write_text

    atomic_write_text(workspace_root / meta["mirror"], body)
