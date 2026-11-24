"""Persist dashboard-chat media attachments into the agent's workspace.

The dashboard composer sends attachments to ``/api/chat`` / ``/api/chat/stream``
as base64 wire items ``{kind, name, mime, data}``. Historically image
attachments were inlined into the model input as base64 vision blocks — which
silently showed nothing on non-vision models, re-billed vision tokens on every
later turn of the thread (the checkpointer replays history), and left the agent
with no file it could actually act on (publish, resize, attach, …).

This module implements the save-and-reference contract instead: binary media
(image / audio / video) is decoded and saved under ``<workspace>/uploads/``
with a sanitized, collision-safe filename (via the shared
:mod:`olano.uploads` store — messaging-platform inbound images use the same
one), and the message text gains a note per file telling the agent the
absolute saved path (plus a transcript for audio/video when speech-to-text is
enabled — same notes as the messaging platforms). The agent decides what the
attachment is *for* from the user's message: analyze it with a vision tool
(``image_describe``), pass the path to a publishing/upload tool, or just keep
the file.

Because files land inside the workspace, the agent's jailed file tools can
read them, the Files view lists them, and the chat renders them inline via
``runtime/media_refs.py`` when a thread is reopened.

Documents (``kind == "document"``: PDF, Word, Excel, PowerPoint and any other
non-text file) follow the same save-and-reference contract. The note names the
saved path and points the agent at ``document_to_text``, rather than inlining
an extract: the agent then works on the real file (every row and formula of a
spreadsheet, a template to fill in), the extract is not replayed on every later
turn, and the user's own message stays a short line plus a file card when the
thread is reopened.

Text-file attachments are untouched here — ``runtime/api.py:
_build_multimodal_content`` keeps inlining their content into the prompt.
Callers gate on the requesting user: read-only members must not write to the
workspace, so their attachments keep the legacy inline path, where a
document's text is extracted in memory instead (:func:`inline_chat_documents`)
and a recording is transcribed from a temp file (:func:`transcribe_chat_audio`).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from pathlib import Path

from olano.platforms import inbound
from olano.uploads import (
    KIND_LABEL,
    MEDIA_GUIDANCE_NOTE,
    UPLOADS_DIRNAME,
    file_category,
    human_size,
    sanitize_filename,
    save_media_bytes,
)

__all__ = [
    "MAX_MEDIA_BYTES",
    "MEDIA_GUIDANCE_NOTE",
    "MEDIA_KINDS",
    "PERSISTED_KINDS",
    "UPLOADS_DIRNAME",
    "ingest_chat_media",
    "inline_chat_documents",
    "sanitize_filename",
    "transcribe_chat_audio",
]

logger = logging.getLogger(__name__)

# Binary media kinds. "file" (text) attachments stay inline.
MEDIA_KINDS = frozenset({"image", "audio", "video"})
# Every kind persisted to disk: media plus documents (any other binary file).
PERSISTED_KINDS = MEDIA_KINDS | {"document"}

# Per-file decoded cap — mirrors the composer's MAX_ATTACHMENT_BYTES
# (dashboard_2.0/src/views/chat/attachments.ts) so the server enforces what
# the UI promises.
MAX_MEDIA_BYTES = 10 * 1024 * 1024


def _decode_payload(data: object) -> bytes | None:
    """Decode a wire attachment's base64 payload; ``None`` when malformed."""
    if not isinstance(data, str) or not data:
        return None
    raw = data.strip()
    # Tolerate a full data URL slipping through (the composer strips it, but
    # other API clients may not).
    if raw.startswith("data:"):
        raw = raw.partition(",")[2]
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return None


def _document_note(name: str, path: Path | str, mime: str, size: int, data: bytes) -> str:
    """The saved-path note for a non-media file, naming the tool that reads it.

    One instruction for every file would be wrong for most of them:
    ``document_to_text`` is the reader for office files and PDFs, a text file of
    any extension is read with ``read_file`` (which pages through a long one),
    and an archive or other binary needs the shell. The category comes from
    :func:`olano.uploads.file_category`: extension for documents and archives,
    the bytes for text.
    """
    where = f"saved to {path} ({mime or 'unknown type'}, {human_size(size)})"
    category = file_category(path, data[:8192])
    if category == "document":
        return (
            f"[Document attached: {name}, {where}. Its contents are NOT shown inline: "
            "to read it, call document_to_text with this path (it converts older Office "
            "formats and runs OCR on scanned PDFs); use a document skill to edit it.]"
        )
    if category == "text":
        return (
            f"[Attachment: {name}, {where}. It is a text file, NOT shown inline: read it "
            "with read_file, paging through it with offset and limit if it is long.]"
        )
    if category == "archive":
        return (
            f"[Attachment: {name}, {where}. It is an archive: list or unpack it with the "
            "shell (unzip -l / tar -tf) if you have one, then read the files inside; "
            "otherwise tell the user you cannot open archives.]"
        )
    return (
        f"[Attachment: {name}, {where}. It is a binary file that document_to_text "
        "cannot read: inspect it with the shell (for example `file` on its path) if "
        "you have one; otherwise tell the user what you can and cannot do with it.]"
    )


def _extract_document_bytes(name: str, mime: str, data: bytes) -> str | None:
    """Extract a document's text without writing it into a workspace.

    For read-only members, whose attachments are never persisted. Text of any
    extension is decoded directly (capped like every inline extract); the
    document converters dispatch on the file suffix, so other bytes go to a
    temp file named like the sanitized upload. ``None`` when the format is not
    one the converters read, or nothing could be extracted (a scanned PDF).
    """
    if file_category(name, data[:8192]) == "text":
        text = data.decode("utf-8", errors="replace")
        limit = inbound.DOC_INJECT_MAX_CHARS
        if len(text) > limit:
            text = text[:limit] + f"\n[... truncated, {len(text) - limit} chars omitted]"
        return text if text.strip() else None
    filename = sanitize_filename(name, mime, "document")
    with tempfile.TemporaryDirectory(prefix="olano-chat-doc-") as tmp:
        path = Path(tmp) / filename
        path.write_bytes(data)
        return inbound._document_text(str(path), kept=False)


async def inline_chat_documents(
    message: str, attachments: list[dict] | None
) -> tuple[str, list[dict] | None]:
    """Inline the text of document attachments that cannot be persisted.

    Returns ``(augmented_message, remaining_attachments)`` with every
    ``kind == "document"`` item folded into the message as its extracted text,
    or an explicit note when there is none; other items pass through. The
    extraction runs off the event loop (a large PDF takes seconds).
    """
    if not attachments:
        return message, attachments

    remaining: list[dict] = []
    notes: list[str] = []
    for att in attachments:
        if not isinstance(att, dict) or att.get("kind") != "document":
            remaining.append(att)
            continue
        name = str(att.get("name", "")).strip() or "attachment"
        mime = str(att.get("mime", "")).strip().lower()
        data = _decode_payload(att.get("data"))
        if not data:
            notes.append(f"[Document attachment '{name}' could not be decoded; it was discarded.]")
            continue
        if len(data) > MAX_MEDIA_BYTES:
            notes.append(
                f"[Document attachment '{name}' exceeds the "
                f"{MAX_MEDIA_BYTES // (1024 * 1024)} MB limit; it was discarded.]"
            )
            continue
        try:
            text = await asyncio.to_thread(_extract_document_bytes, name, mime, data)
        except Exception as exc:
            logger.info("chat document extraction failed for %r: %s", name, exc)
            text = None
        if text:
            notes.append(f"[Content of {name}]:\n{text}")
        else:
            notes.append(
                f"[Document attached: {name} ({mime or 'document'}, {human_size(len(data))}): "
                "no text could be extracted, and the file was not saved.]"
            )

    if notes:
        joined = "\n\n".join(notes)
        message = f"{message}\n\n{joined}" if message.strip() else joined
    return message, (remaining or None)


async def transcribe_chat_audio(
    message: str, attachments: list[dict] | None
) -> tuple[str, list[dict] | None]:
    """Transcribe audio attachments that cannot be persisted.

    The member counterpart of :func:`ingest_chat_media` for ``kind == "audio"``
    (a voice note from the mobile app): the recording is written to a temp
    file only for as long as speech-to-text reads it, and replaced by the same
    ``[Voice message transcript]`` note, or the same note saying why there is
    none, that a saved recording gets. Without it the inline path drops audio,
    and a voice note sent on its own reaches the agent as an empty message.
    Other items pass through.
    """
    if not attachments:
        return message, attachments

    remaining: list[dict] = []
    notes: list[str] = []
    for att in attachments:
        if not isinstance(att, dict) or att.get("kind") != "audio":
            remaining.append(att)
            continue
        name = str(att.get("name", "")).strip() or "attachment"
        mime = str(att.get("mime", "")).strip().lower()
        data = _decode_payload(att.get("data"))
        if not data:
            notes.append(f"[Audio attachment '{name}' could not be decoded; it was discarded.]")
            continue
        if len(data) > MAX_MEDIA_BYTES:
            notes.append(
                f"[Audio attachment '{name}' exceeds the "
                f"{MAX_MEDIA_BYTES // (1024 * 1024)} MB limit; it was discarded.]"
            )
            continue
        # The speech provider reads the format off the file name.
        filename = sanitize_filename(name, mime, "audio")
        with tempfile.TemporaryDirectory(prefix="olano-chat-audio-") as tmp:
            path = Path(tmp) / filename
            await asyncio.to_thread(path.write_bytes, data)
            transcript, stt_note = await inbound._transcribe_audio(str(path))
        if transcript:
            notes.append(f"[Voice message transcript] {transcript}")
        elif stt_note:
            notes.append(stt_note)

    if notes:
        joined = "\n\n".join(notes)
        message = f"{message}\n\n{joined}" if message.strip() else joined
    return message, (remaining or None)


async def ingest_chat_media(
    message: str,
    attachments: list[dict] | None,
    workspace_root: Path | str,
    agent: str | None = None,
) -> tuple[str, list[dict] | None]:
    """Persist media attachments to ``<workspace>/uploads/`` and note the paths.

    Returns ``(augmented_message, remaining_attachments)``:

    * every image / audio / video / document item is decoded, saved, and replaced by a
      ``[<Kind> attached: name — saved to <abs path> (mime, size)]`` note in
      the message text (audio/video additionally get a transcript when the
      speech layer is enabled — same wording as the messaging platforms; a
      document's note points the agent at ``document_to_text``);
    * an undecodable or oversized item becomes an explicit failure note (an
      attachment must never be silent);
    * an image or document whose *save* fails stays in
      ``remaining_attachments`` so the caller's legacy inline path still shows
      it to the model;
    * ``kind == "file"`` (text) items pass through untouched in
      ``remaining_attachments`` for prompt inlining.

    Defensive by design: per-item failures degrade to notes, never raise.
    """
    if not attachments:
        return message, attachments

    remaining: list[dict] = []
    notes: list[str] = []
    saved_any = False
    root = Path(workspace_root)

    for att in attachments:
        kind = att.get("kind") if isinstance(att, dict) else None
        if kind not in PERSISTED_KINDS:
            remaining.append(att)
            continue

        name = str(att.get("name", "")).strip() or "attachment"
        mime = str(att.get("mime", "")).strip().lower()
        label = KIND_LABEL[kind]

        data = _decode_payload(att.get("data"))
        if not data:
            notes.append(f"[{label} attachment '{name}' could not be decoded — it was discarded.]")
            continue
        if len(data) > MAX_MEDIA_BYTES:
            notes.append(
                f"[{label} attachment '{name}' exceeds the "
                f"{MAX_MEDIA_BYTES // (1024 * 1024)} MB limit — it was discarded.]"
            )
            continue

        try:
            path, _reused = save_media_bytes(root, name, mime, kind, data, agent=agent)
        except OSError as exc:
            logger.warning("failed to save chat attachment %r: %s", name, exc)
            if kind in ("image", "document"):
                # Keep the wire item: the legacy inline path still shows it to
                # the model this turn (vision blocks for an image, the
                # extracted text for a document via inline_chat_documents).
                remaining.append(att)
            else:
                notes.append(f"[{label} attachment '{name}' could not be saved: {exc}]")
            continue

        logger.info("chat attachment saved: %s (%s, %d bytes)", path, mime or kind, len(data))
        if kind == "document":
            notes.append(_document_note(name, path, mime, len(data), data))
            continue
        saved_note = f"[{label} attached: {name} — saved to {path} ({mime or kind}, {human_size(len(data))})]"
        if kind == "audio":
            transcript, stt_note = await inbound._transcribe_audio(str(path))
            if transcript:
                # A transcribed voice note IS the message, exactly as it is on
                # Telegram or WhatsApp (`inbound._media_extras` appends the
                # transcript alone). This path used to add the saved-path note
                # and then the media guidance note, which tells the agent to
                # "pass the saved path to the relevant tool": the agent read
                # that as a file to process, narrated, and ran audio_to_text
                # over a recording it had already been given the words of. So
                # a spoken question got a visibly different turn (and a second
                # transcription) from the same question typed. The transcript
                # comes first and the path note says the file is done with;
                # the guidance note is for files the agent has yet to look at.
                notes.append(f"[Voice message transcript] {transcript}")
                notes.append(
                    f"[{label} attached: {name} — saved to {path} ({mime or kind}, "
                    f"{human_size(len(data))}); already transcribed above, so the "
                    "transcript is the user's message and the recording needs no tool.]"
                )
                continue
            saved_any = True
            notes.append(saved_note)
            if stt_note:
                notes.append(stt_note)
            continue
        saved_any = True
        notes.append(saved_note)
        if kind == "video":
            notes.extend(await inbound._video_transcript_segments(str(path)))

    if saved_any:
        notes.append(MEDIA_GUIDANCE_NOTE)
    if notes:
        joined = "\n\n".join(notes)
        message = f"{message}\n\n{joined}" if message.strip() else joined
    return message, (remaining or None)
