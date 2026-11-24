"""Workspace ``uploads/`` store for user-provided media files.

Shared by the two ingestion surfaces that receive files *from the user* and
hand the agent a saved path instead of inline pixels:

* **dashboard chat attachments** — ``olano/runtime/chat_attachments.py``
  (base64 wire items from the composer);
* **messaging-platform inbound media** — ``olano/platforms/inbound.py``
  (files the adapters downloaded to the local cache).

Both write into ``<workspace>/uploads/`` so the agent's jailed file tools can
read the files, the Files view lists them, and the dashboard chat renders them
inline via ``runtime/media_refs.py``. Keep this module stdlib-only: it sits
below both ``runtime`` and ``platforms`` and must import from neither.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

# Workspace subdirectory user-provided files land in (sibling of the
# tool-output conventions: images/, screenshots/, web_scraping/).
UPLOADS_DIRNAME = "uploads"

# Trailing guidance appended once per message that saved at least one file.
# Phrased so an agent without vision tools still knows the file exists and
# where it is, instead of claiming no image arrived.
MEDIA_GUIDANCE_NOTE = (
    "[Note: the attached file(s) above are saved in your workspace but are NOT "
    "shown to you inline. Decide from the user's message what to do with them: "
    "to view or analyze an image, call a vision tool such as image_describe "
    "with the saved path; to publish, send, or process a file, pass the saved "
    "path to the relevant tool; if the user only wants the file kept, no tool "
    "call is needed.]"
)

# Human labels for note prefixes, keyed by attachment kind.
KIND_LABEL = {"image": "Image", "audio": "Audio", "video": "Video", "document": "Document"}

# Extension fallbacks for common media MIME types where the filename has no
# usable suffix (e.g. clipboard pastes). ``mimetypes.guess_extension`` covers
# the long tail but picks oddities for these (".jpe", ".oga").
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/webm": ".weba",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/ogg": ".ogv",
}

# Path separators, shell-hostile and control characters — replaced with "_".
_UNSAFE_CHARS = re.compile(r"[\\/:*?\"'<>|\x00-\x1f`$&;]")
_MAX_STEM_LEN = 64


# How much of a file the text sniff reads. Enough to see past a header, small
# enough to cost nothing on a 100 MB upload.
TEXT_SNIFF_BYTES = 8192

# Formats `document_to_text` reads by converting (native ones, or through
# LibreOffice for the older and open-document ones). Decided by extension:
# a PDF can open with an all-ASCII first block and would sniff as text.
DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".xlsm",
        ".html",
        ".htm",
        ".doc",
        ".xls",
        ".ppt",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
    }
)
ARCHIVE_EXTENSIONS = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar"}
)


def looks_like_text(sample: bytes) -> bool:
    """Whether ``sample`` (a file's first bytes) is UTF-8 text.

    How "is this text?" is answered for an attachment instead of by extension
    list or the browser's MIME type, which are both unreliable (a browser types
    TypeScript ``.ts`` as an MPEG video, and an iPhone often sends no type at
    all). Rejects a NUL byte, invalid UTF-8 (except a character cut in half at
    the end of the sample) and more than 1% control characters other than
    whitespace and ESC. An empty file counts as text.
    """
    sample = sample[:TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character split by the sample boundary is not binary.
        if exc.start < len(sample) - 3 or exc.reason != "unexpected end of data":
            return False
    control = sum(1 for b in sample if b < 0x20 and b not in (0x09, 0x0A, 0x0C, 0x0D, 0x1B))
    return control <= len(sample) // 100


def file_category(path: Path | str, sample: bytes | None = None) -> str:
    """What an agent should reach for to read a saved file.

    ``"document"`` (``document_to_text``), ``"archive"`` (the shell),
    ``"text"`` (``read_file``) or ``"binary"`` (the shell, if anything). The
    extension decides documents and archives; the bytes decide text. ``sample``
    saves re-reading a file the caller already holds.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    if sample is None:
        try:
            with p.open("rb") as fh:
                sample = fh.read(TEXT_SNIFF_BYTES)
        except OSError:
            return "binary"
    return "text" if looks_like_text(sample) else "binary"


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def sanitize_filename(name: str, mime: str, kind: str) -> str:
    """A safe single-component filename for a user-provided file.

    Strips any directory parts (POSIX and Windows), neutralizes separator /
    control / shell-metacharacters, collapses whitespace to ``_`` (a path with
    spaces would break ``media_refs`` token detection and shell quoting), caps
    the stem length, and guarantees an extension (from the original name, else
    derived from the MIME type) so downstream tools that dispatch on suffix
    (``image_describe``, ``media_refs``) recognize the file.
    """
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_CHARS.sub("_", base)
    base = re.sub(r"\s+", "_", base).strip(" ._")

    stem, dot, ext = base.rpartition(".")
    if dot and stem and re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
        ext = "." + ext.lower()
    else:
        stem, ext = base, ""
    if not ext:
        low_mime = (mime or "").lower()
        ext = _MIME_EXT.get(low_mime) or mimetypes.guess_extension(low_mime) or ""
    if not stem:
        stem = f"{kind}-attachment"
    if len(stem) > _MAX_STEM_LEN:
        stem = stem[:_MAX_STEM_LEN].rstrip(" .") or f"{kind}-attachment"
    return stem + ext


def dedup_path(uploads_dir: Path, filename: str, data: bytes) -> tuple[Path, bool]:
    """Pick the target path for ``filename``, reusing byte-identical files.

    Returns ``(path, already_exists)``. A resend / retry of the same file maps
    onto the existing copy instead of accumulating ``photo-2.png`` duplicates;
    a *different* file with the same name gets a ``-N`` suffix.
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    else:
        ext = "." + ext
    candidate = uploads_dir / filename
    for n in range(2, 51):
        if not candidate.exists():
            return candidate, False
        try:
            if candidate.stat().st_size == len(data) and candidate.read_bytes() == data:
                return candidate, True
        except OSError:  # unreadable existing file — fall through to next name
            pass
        candidate = uploads_dir / f"{stem}-{n}{ext}"
    return uploads_dir / f"{stem}-{uuid.uuid4().hex[:8]}{ext}", False


def save_media_bytes(
    workspace_root: Path | str,
    name: str,
    mime: str,
    kind: str,
    data: bytes,
    *,
    agent: str | None = None,
) -> tuple[Path, bool]:
    """Persist ``data`` under ``<workspace_root>/uploads/`` and return the path.

    Returns ``(path, reused)`` — ``reused`` is True when a byte-identical file
    already existed and was reused instead of written. Raises ``OSError`` when
    the file cannot be saved (caller decides the fallback).

    ``agent`` names the workspace's owner so a file the runtime writes here can
    be handed to that agent's sandbox user. The dashboard's chat-attachment path
    must pass it — that request runs outside ``agent_runtime_context``, where the
    hand-off's own contextvar fallback finds nothing; the messenger path already
    runs inside one and can leave it unset.
    """
    uploads_dir = Path(workspace_root) / UPLOADS_DIRNAME
    dir_existed = uploads_dir.exists()
    uploads_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(name, mime, kind)
    path, reused = dedup_path(uploads_dir, filename, data)
    # sanitize_filename yields a single component, but keep the containment
    # invariant explicit (mirrors the document cache).
    if path.resolve().parent != uploads_dir.resolve():
        raise OSError(f"upload path escaped {UPLOADS_DIRNAME}/: {path}")
    if not reused:
        path.write_bytes(data)
    from olano.tools._sandbox_exec import handoff_to_sandbox

    # `reused` too: a byte-identical file written by an earlier run — before this
    # hand-off existed, or by a different surface — is still root-owned.
    handoff_to_sandbox(path, created=() if dir_existed else (uploads_dir,), agent=agent)
    return path, reused
