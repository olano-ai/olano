"""Turn a file reference an agent wrote into a real, sendable local file.

Agents name files in whatever form the model produces — a bare absolute path, a
``file:///…`` URI, a tilde path, or a container-local path like
``/workspace/out.png`` when they run under ``OlanoDockerBackend``. The
platform adapters can only upload a path that ``open()`` resolves *on the host*,
so every one of those forms has to collapse to one before delivery.

Before this module the collapse didn't happen. ``file:///…`` failed
``os.path.exists``, so a photo send fell through to the "remote URL" branch,
where the SSRF guard rejected the ``file:`` scheme and the adapter quietly
downgraded the whole thing to a **plain-text message containing the URI** — the
user saw a path instead of their image, and the tool returned a Telegram message
id that the model then reported as an HTTP error code.

Two entry points share this logic:

* the explicit tools (``notification_send_photo`` / ``_send_document``), via
  ``BasePlatformAdapter.send_media``;
* the implicit path — scanning an agent's reply text for file references and
  attaching them automatically (``extract_local_files``), which is how this is
  meant to work day to day: the agent just mentions the file.

``media_kind`` then decides photo vs video vs voice vs document, so the same
reference routes to the right native attachment on every platform.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"})
AUDIO_EXTS = frozenset({".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".aac"})
#: Deliberately conservative. This set also gates the *implicit* attach path,
#: where the file name comes from model output — so it lists document formats a
#: user would plausibly want pushed to a chat and omits executables, archives of
#: source, and anything credential-shaped (``.env``, ``.pem``, ``.key``, ``.sh``).
DOCUMENT_EXTS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".tsv",
        ".txt",
        ".md",
        ".json",
        ".yaml",
        ".yml",
        ".zip",
        ".ics",
        ".epub",
    }
)

MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | DOCUMENT_EXTS

#: Container-only aliases under ``OlanoDockerBackend``. The sandbox keeps one
#: path namespace — the workspace is mounted at its *host* path as well as at
#: ``/workspace`` — so a sandboxed agent normally reports a path that already
#: resolves here and never reaches this fallback. ``/workspace`` remains the
#: container working dir and a stable alias, so a path can still arrive in that
#: form (a ``pwd``-relative answer, an older persistent container); re-rooting it
#: is what keeps such a reference deliverable. This is delivery-side only —
#: prompts and middleware must keep speaking host paths (see CLAUDE.md,
#: "One path namespace").
_CONTAINER_MOUNTS = ("/workspace", "/shared")

#: Path fragments never auto-attached on the *implicit* path. The agent can
#: still send these deliberately via ``notification_send_document``; what this
#: blocks is a stray mention in reply text turning into an upload.
_SENSITIVE_FRAGMENTS = (
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "/etc/shadow",
    "/etc/passwd",
    # The home's vault directory, under every name the home has had.
    "/olano/vault",
    "/.olano/vault",
    "/.lionclaw/vault",
    "id_rsa",
    "credentials.json",
)


_EXT_ALTERNATION = "|".join(sorted(e.lstrip(".") for e in MEDIA_EXTS))

#: The file references the implicit attach path treats as candidates — kept
#: here so ``extract_local_files`` (attach) and ``defang_local_file_refs``
#: (suppress) can never drift apart. Two alternatives, tried in order:
#:   uri  — file: URIs (file:/p, file://p, file:///p, file://localhost/p)
#:   path — bare absolute/home paths. The (?<![/:\w.]) lookbehind keeps this
#:          branch out of http(s) URLs and relative ./foo.png, which is also
#:          why it can never match the file: form above.
#:   rel  — workspace-relative paths with at least one directory component
#:          (``reports/chart.png``), resolved against the agent's workspace by
#:          ``resolve_local_media``. A bare file name (``chart.png``) is
#:          deliberately not a candidate: attaching is a heavier act than a
#:          chat card, and prose that mentions ``logo.png`` must not upload
#:          it. ``./`` and ``../`` forms are excluded by the first-character
#:          class, and the lookbehind keeps it out of URLs and longer paths.
LOCAL_FILE_REF_RE = re.compile(
    r"(?P<uri>file:/{0,3}[^\s<>\"'`]+\.(?:" + _EXT_ALTERNATION + r"))"
    r"|(?P<path>(?<![/:\w.])(?:~/|/)(?:[\w.\-]+/)*[\w.\-]+\.(?:" + _EXT_ALTERNATION + r"))\b"
    r"|(?P<rel>(?<![/:\w.~\-])(?:[\w\-][\w.\-]*/)+[\w.\-]+\.(?:" + _EXT_ALTERNATION + r"))\b",
    re.IGNORECASE,
)

#: Trailing sentence punctuation trimmed off a matched reference before it is
#: resolved — ``see /a/b.png.`` names the file, not ``b.png.``.
REF_TRAILING_PUNCTUATION = ".,;:!?)]}"


def code_span_ranges(text: str) -> list[tuple[int, int]]:
    """Spans covered by fenced code blocks (``` … ```) and inline code (`…`).

    File references inside these are never auto-attached (and never defanged),
    so code samples pass through chat untouched.
    """
    spans = [(m.start(), m.end()) for m in re.finditer(r"```[^\n]*\n.*?```", text, re.DOTALL)]
    spans.extend((m.start(), m.end()) for m in re.finditer(r"`[^`\n]+`", text))
    return spans


def defang_local_file_refs(text: str, *, agent_name: str | None = None) -> str:
    """Backtick-wrap file references in *text* that would otherwise auto-attach.

    The implicit attach path (``BasePlatformAdapter.extract_local_files``)
    skips code spans, so wrapping a path in backticks keeps it visible as text
    while guaranteeing no attachment. Used for command output (`/memory`,
    `/session`, …): a command reply is a listing, never a file delivery, but it
    may echo content — e.g. MEMORY.md naming a real ``.md``/``.pdf`` — that the
    adapter would happily upload.

    Only references that would actually attach are wrapped (same resolution as
    the extractor); everything else, including refs already inside code spans,
    passes through byte-for-byte. Idempotent — a second pass finds the wrapped
    refs inside inline code and leaves them alone.
    """
    if not text or "/" not in text:
        return text
    spans = code_span_ranges(text)

    def _in_code(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    pieces: list[str] = []
    last = 0
    for match in LOCAL_FILE_REF_RE.finditer(text):
        if _in_code(match.start()):
            continue
        ref = match.group(0).rstrip(REF_TRAILING_PUNCTUATION)
        if resolve_local_media(ref, agent_name=agent_name) is None:
            continue
        pieces.append(text[last : match.start()])
        pieces.append(f"`{ref}`")
        last = match.start() + len(ref)
    if not pieces:
        return text
    pieces.append(text[last:])
    return "".join(pieces)


def strip_file_uri(ref: str) -> str:
    """``file:///a/b.png`` → ``/a/b.png``; non-``file:`` refs pass through.

    Handles the shapes models actually emit: ``file:///abs``, ``file://abs``,
    ``file:/abs``, ``file://localhost/abs``, and percent-encoded spaces.
    """
    text = (ref or "").strip().strip("<>")
    if not text.lower().startswith("file:"):
        return text
    parsed = urlparse(text)
    path = unquote(parsed.path or "")
    # ``file://host/path`` puts "host" in netloc. "localhost"/"" are the only
    # meaningful values for a local file; anything else is a remote reference we
    # deliberately do not fetch, so keep its netloc in the path and let the
    # caller's existence check reject it.
    netloc = parsed.netloc or ""
    if netloc and netloc.lower() != "localhost":
        path = f"//{netloc}{path}"
    elif not path.startswith("/"):
        # ``file:relative`` / ``file:abs`` without the double slash.
        path = "/" + path.lstrip("/")
    return path or text


def media_kind(path: str) -> str:
    """Classify by extension → ``image`` | ``video`` | ``audio`` | ``document``."""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "document"


def is_sensitive_path(path: str) -> bool:
    """Whether *path* looks like something never to auto-attach."""
    lowered = path.replace("\\", "/").lower()
    return any(fragment in lowered for fragment in _SENSITIVE_FRAGMENTS)


def _agent_workspace_root(agent_name: str) -> Path | None:
    try:
        from olano.config.loader import load_agent_config
        from olano.config.workspace import get_workspace

        return get_workspace(load_agent_config(agent_name).workspace).root
    except Exception as exc:  # missing agent, unreadable config — not fatal
        logger.debug("media_paths: no workspace root for agent '%s': %s", agent_name, exc)
        return None


def _container_candidates(path: str, agent_name: str | None) -> list[str]:
    """Host paths a container-local *path* could correspond to.

    Only consulted when *path* did not resolve as-is: ``/workspace/a/b.png``
    refers to the same bytes as ``<workspace_root>/a/b.png`` on the host, since
    both are mounts of the same directory.
    """
    if not agent_name:
        return []
    root = _agent_workspace_root(agent_name)
    if root is None:
        return []
    for mount in _CONTAINER_MOUNTS:
        if path == mount or path.startswith(mount + "/"):
            relative = path[len(mount) :].lstrip("/")
            if not relative:
                continue
            candidate = (root / relative).resolve()
            # Never let ``..`` segments walk out of the workspace.
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                logger.warning(
                    "media_paths: refusing container path '%s' — escapes workspace root", path
                )
                continue
            return [str(candidate)]
    return []


def _relative_candidates(path: str, agent_name: str | None) -> list[str]:
    """Host path a workspace-relative *path* (``reports/chart.png``) refers to.

    Only consulted when *path* did not resolve as-is or as a container alias:
    the agent's workspace root is the one base a relative reference can mean.
    Absolute and home paths never reach here, and ``..`` can never walk out of
    the workspace.
    """
    if not agent_name or not path or os.path.isabs(path) or path.startswith("~"):
        return []
    root = _agent_workspace_root(agent_name)
    if root is None:
        return []
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        logger.warning("media_paths: refusing relative path '%s' — escapes workspace root", path)
        return []
    return [str(candidate)]


def resolve_local_media(ref: str, *, agent_name: str | None = None) -> str | None:
    """Resolve *ref* to an existing local file, or ``None`` if it isn't one.

    Accepts bare paths, ``~`` paths, ``file://`` URIs and container-local paths.
    Returning ``None`` means "not a local file" — the caller should treat the
    reference as a remote URL (or ignore it), never as a path.
    """
    if not ref:
        return None
    path = os.path.expanduser(strip_file_uri(ref))
    if os.path.isfile(path):
        return path
    for candidate in _container_candidates(path, agent_name):
        if os.path.isfile(candidate):
            logger.debug("media_paths: mapped container path '%s' → '%s'", ref, candidate)
            return candidate
    for candidate in _relative_candidates(path, agent_name):
        if os.path.isfile(candidate):
            logger.debug("media_paths: resolved workspace-relative '%s' → '%s'", ref, candidate)
            return candidate
    return None
