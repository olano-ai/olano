"""Detect workspace files referenced in chat text for inline display and cards.

When a tool saves a file into the agent's workspace — a chart, a browser
screenshot, a generated image, a spreadsheet, a PDF — and its path shows up in
a tool result, in the agent's own reply, or in a chat-upload note, the
dashboard can render it instead of showing a bare path: images, audio and
video inline, every other file as a card with Download / Open in Files. This
module scans a string for path tokens that resolve to an existing regular file
**inside** the agent's workspace and returns lightweight descriptors the chat
renders via ``/api/files-raw`` (bytes) and ``/files?path=…`` (the Files tab).

Only files under the workspace root are surfaced, so this never widens what
the file endpoint already exposes (that endpoint re-checks containment on
serve). Two policies keep prose from sprouting cards: a non-media file must be
named with a directory component or an absolute path (a bare ``MEMORY.md`` in
a sentence is not a reference), and hidden paths, credential-shaped names and
the workspace's own bootstrap files are never surfaced as cards.

**What gets scanned is a third policy, and it lives in ``SCAN_TOOL_RESULTS``
below**: only the agent's finished reply, never the tool results it produced
along the way.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from olano.platforms.media_paths import DOCUMENT_EXTS, is_sensitive_path, strip_file_uri

logger = logging.getLogger(__name__)

# Extension → (kind, mime) for the three inline-renderable media families the
# dashboard chat supports; the frontend renders each kind with an <img> /
# <audio> / <video> element.
_MEDIA_TYPES: dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".bmp": ("image", "image/bmp"),
    ".svg": ("image", "image/svg+xml"),
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"),
    ".m4v": ("video", "video/x-m4v"),
    ".ogv": ("video", "video/ogg"),
    ".mp3": ("audio", "audio/mpeg"),
    ".wav": ("audio", "audio/wav"),
    ".ogg": ("audio", "audio/ogg"),
    ".m4a": ("audio", "audio/mp4"),
    ".flac": ("audio", "audio/flac"),
    ".aac": ("audio", "audio/aac"),
}

# Document-like files get the ``document`` kind (a card with a document icon);
# the platform adapters' DOCUMENT_EXTS is the shared definition, extended with
# the web/markup formats the Files tab previews.
_DOCUMENT_EXTS: frozenset[str] = DOCUMENT_EXTS | frozenset({".html", ".htm", ".xml", ".log"})

# Never surfaced as a card, whatever the extension says: these are secrets by
# convention, not deliverables.
_SECRET_EXTS: frozenset[str] = frozenset({".env", ".pem", ".key", ".p12", ".pfx"})
_SECRET_NAME_RE = re.compile(r"secret|token|credential|password", re.IGNORECASE)

# The workspace's own prompt/memory files: naming one in a reply ("I updated
# MEMORY.md") is bookkeeping, not a hand-off, so they never become cards.
_BOOTSTRAP_FILES: frozenset[str] = frozenset(
    {
        "AGENTS.md",
        "BRAINSTORMING.md",
        "CAPABILITIES.md",
        "CORE_VIBES.md",
        "CORTEX.md",
        "EXECUTION_BIAS.md",
        "IDEAS.md",
        "IDENTITY.md",
        "LEARNINGS.md",
        "MEMORY.md",
        "PROTOTYPING.md",
        "SAFETY_BOUNDARIES.md",
        "SOUL.md",
        "TEAM.md",
        "TOOLS.md",
        "USER.md",
    }
)

# A path-like token ending in an extension. The character class stops at
# whitespace and the delimiters chat text wraps paths in — quotes, parentheses
# (``… (1420.4 KB)``), backticks, commas, angle brackets — so the matched token
# is just the path; trailing sentence punctuation (``see reports/x.png.``) is
# left out of the token by the lookahead.
_PATH_RE = re.compile(
    r"""(?P<path>[^\s"'`(),<>]+\.[A-Za-z0-9]{1,8})(?=[.,;:!?]*(?:$|[\s"'`(),<>]))""",
)

# Container-local aliases a sandboxed agent may still report (the workspace is
# mounted at its host path AND at /workspace, so this is a re-rooting, not a
# widening).
_CONTAINER_WORKSPACE_PREFIX = "/workspace/"

# Bound the work one string can cause: tokens examined, and cards produced.
_MAX_CANDIDATES = 64
_MAX_ITEMS = 8

#: **Disabled.** Whether a *tool result* is scanned for files, in addition to
#: the agent's own finished reply.
#:
#: This ran on every ``tool_result`` frame, and the client folds a tool
#: result's media into the assistant message (``sessionStore.foldFrame``), so
#: any path a tool's output merely *mentioned* became a card under the answer.
#: A tool that reads a file returns that file's **contents**, so a document
#: citing a path attached it: an ``agentfather`` turn that read a skill
#: reference ("read ``system/knowledge/USER_GUIDE.md`` via ``workspace_context``")
#: shipped the 257 KB user guide to the user, who had asked about Salesforce.
#: A text scan cannot tell "this tool wrote this file" from "this tool's output
#: names this path", and intermediate files are not deliverables either — so
#: the reply is now the only thing that can hand a file over.
#:
#: The cost is a file whose path appears **only** in a tool result and never in
#: the agent's own words (a screenshot, a rendered chart, a pairing QR). The
#: prompt's hand-off rule covers that case from the other side: agents are told
#: to name every file they produced, with its full path, in their final message
#: (``core/prompt_assembler.py`` → Workspace Environment).
#:
#: Kept as a switch rather than deleted so the old behaviour is one edit away,
#: and so every call site keeps naming the policy it follows.
SCAN_TOOL_RESULTS = False


def file_raw_url(rel_path: str, agent: str) -> str:
    """Build the ``/api/files-raw`` URL the dashboard's Files view already uses.

    Each path segment is percent-encoded but the slashes are preserved (FastAPI's
    ``{path:path}`` matches the remaining slashes); ``agent`` scopes the read to
    that agent's workspace.
    """
    encoded = "/".join(quote(seg, safe="") for seg in rel_path.split("/"))
    suffix = f"?agent={quote(agent, safe='')}" if agent else ""
    return f"/api/files-raw/{encoded}{suffix}"


def files_view_url(rel_path: str, agent: str) -> str:
    """Deep link into the dashboard Files tab (``FilesView`` reads ``path`` + ``agent``)."""
    url = f"/files?path={quote(rel_path, safe='/')}"
    if agent:
        url += f"&agent={quote(agent, safe='')}"
    return url


def _classify(path: Path) -> tuple[str, str]:
    """``(kind, mime)`` for *path* — media kinds render inline, the rest are cards."""
    ext = path.suffix.lower()
    media = _MEDIA_TYPES.get(ext)
    if media is not None:
        return media
    guessed, _ = mimetypes.guess_type(path.name)
    mime = guessed or "application/octet-stream"
    if ext in _DOCUMENT_EXTS:
        return "document", mime
    return "file", mime


def _is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def _looks_like_secret(rel: Path) -> bool:
    return rel.suffix.lower() in _SECRET_EXTS or bool(_SECRET_NAME_RE.search(rel.name))


def _candidate_path(token: str, root: Path) -> Path | None:
    """Turn a matched token into an absolute path candidate, or ``None`` to skip it.

    Media may be named bare (``chart.png``); anything else needs a directory
    component or an absolute form, so ordinary prose never grows a card.
    """
    text = strip_file_uri(token)
    if "://" in text:
        return None  # an http(s) URL that happens to end in .png
    ext = Path(text).suffix.lower()
    is_media = ext in _MEDIA_TYPES
    if text.startswith("~"):
        # `Path.expanduser()` RAISES (``RuntimeError: Could not determine home
        # directory.``) when the leading component is a ``~name`` it cannot map
        # to a home — and a scan over prose hits that constantly, because the
        # token regex matches money and version strings too: an agent writing
        # "13 open at ~$2.115M" hands this a token of ``~$2.115M``, whose
        # "user" is ``$2.115M``. The message names HOME, so it reads as a
        # broken box; nothing about the box is broken. An unresolvable ``~`` is
        # simply not a workspace file.
        try:
            text = str(Path(text).expanduser())
        except (RuntimeError, OSError):
            return None
    if text.startswith(_CONTAINER_WORKSPACE_PREFIX):
        text = str(root / text[len(_CONTAINER_WORKSPACE_PREFIX) :])
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    if not is_media and "/" not in text:
        return None
    return root / candidate


def detect_media_refs(
    content: str, workspace_root: Path | str, agent: str
) -> list[dict[str, object]]:
    """Return descriptors for workspace files referenced in ``content``.

    Each descriptor is ``{"kind", "name", "path", "mime", "url", "size",
    "files_url", "download_url", "mentions"}`` where ``path`` is
    workspace-relative, ``url`` targets ``/api/files-raw``, ``files_url`` the
    Files tab, ``download_url`` the same bytes as an attachment, and
    ``mentions`` lists the exact tokens in the text that resolved to the file
    (so a renderer can turn them into links). Paths that don't exist, aren't
    regular files, are hidden, look like credentials, or resolve **outside**
    the workspace are skipped.

    **One card per file, however many times it is named.** Dedupe is keyed on
    the *resolved* workspace-relative path, so every spelling of the same file
    — bare, backticked, absolute, ``file://``, ``/workspace/…``, through a
    symlink, or with ``..`` in it — folds into a single descriptor whose
    ``mentions`` collects each token. A reply that says "Saved: reports/x.png"
    and then refers to ``reports/x.png`` again attaches the file once. Results
    are capped at ``_MAX_ITEMS``.

    Never raises — detection failures degrade to no media.
    """
    if not content:
        return []
    try:
        root = Path(workspace_root).resolve()
    except OSError:
        return []

    out: list[dict[str, object]] = []
    by_rel: dict[str, dict[str, object]] = {}
    examined = 0
    for match in _PATH_RE.finditer(content):
        examined += 1
        if examined > _MAX_CANDIDATES:
            break
        token = match.group("path")
        try:
            candidate = _candidate_path(token, root)
        except Exception:
            # The outer belt behind the "never raises" contract above. This runs
            # over free prose from a model, so the token stream is adversarial by
            # accident; one token that trips a path primitive must cost that
            # token, never the reply it was found in. (The known case — an
            # unresolvable ``~user`` — is handled in `_candidate_path`; this is
            # here so the next one is a missing card, not a failed turn.)
            logger.debug("media ref: skipping token %r", token, exc_info=True)
            continue
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(root)
        except (OSError, ValueError):
            continue  # unreadable, or outside the workspace
        if not rel.parts or _is_hidden(rel):
            continue
        try:
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
        except OSError:
            continue
        if is_sensitive_path(str(resolved)):
            continue
        kind, mime = _classify(resolved)
        if kind not in ("image", "audio", "video"):
            if _looks_like_secret(rel):
                continue
            if len(rel.parts) == 1 and rel.name in _BOOTSTRAP_FILES:
                continue
        rel_str = rel.as_posix()
        existing = by_rel.get(rel_str)
        if existing is not None:
            mentions = existing["mentions"]
            assert isinstance(mentions, list)
            if token not in mentions:
                mentions.append(token)
            continue
        if len(out) >= _MAX_ITEMS:
            break
        raw = file_raw_url(rel_str, agent)
        item: dict[str, object] = {
            "kind": kind,
            "name": resolved.name,
            "path": rel_str,
            "mime": mime,
            "url": raw,
            "size": size,
            "files_url": files_view_url(rel_str, agent),
            "download_url": raw + ("&" if "?" in raw else "?") + "download=1",
            "mentions": [token],
        }
        by_rel[rel_str] = item
        out.append(item)
    return out
