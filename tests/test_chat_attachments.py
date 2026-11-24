"""Tests for dashboard chat attachment persistence (:mod:`runtime.chat_attachments`).

The dashboard composer ships attachments as base64 wire items. Media kinds
(image / audio / video) are saved into the agent workspace's ``uploads/`` dir
and replaced by saved-path notes in the message text — the agent then decides
whether to analyze the file (``describe_image``), pass the path to another
tool, or just keep it. Text-file items pass through for prompt inlining.
These tests pin that contract without hitting the network (speech helpers are
patched at the :mod:`olano.platforms.inbound` seam the module calls through).
"""

from __future__ import annotations

import base64
from pathlib import Path

import olano.platforms.inbound as inbound
import olano.runtime.chat_attachments as chat_attachments
from olano.runtime.chat_attachments import (
    MEDIA_GUIDANCE_NOTE,
    ingest_chat_media,
    sanitize_filename,
)

# A 1x1 PNG so image saves exercise real bytes / base64 round-tripping.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def _att(
    kind: str = "image", name: str = "photo.png", mime: str = "image/png", data: str = _PNG_B64
) -> dict:
    return {"kind": kind, "name": name, "mime": mime, "data": data}


# ───────────────────────────── pass-through ─────────────────────────────────


async def test_no_attachments_returns_message_unchanged(tmp_path) -> None:
    msg, remaining = await ingest_chat_media("hello", None, tmp_path)
    assert msg == "hello"
    assert remaining is None
    assert not (tmp_path / "uploads").exists()


async def test_text_file_passes_through_untouched(tmp_path) -> None:
    txt = {"kind": "file", "name": "notes.md", "mime": "text/markdown", "data": "# hi"}
    msg, remaining = await ingest_chat_media("read this", [txt], tmp_path)
    assert msg == "read this"  # no notes, no guidance
    assert remaining == [txt]
    assert not (tmp_path / "uploads").exists()


# ─────────────────────────────── images ─────────────────────────────────────


async def test_image_saved_with_path_note_and_guidance(tmp_path) -> None:
    msg, remaining = await ingest_chat_media("add this to the product", [_att()], tmp_path)

    saved = tmp_path / "uploads" / "photo.png"
    assert saved.read_bytes() == _PNG_BYTES
    assert remaining is None
    # Original text first, then the note carrying the absolute path.
    assert msg.startswith("add this to the product")
    assert f"saved to {saved}" in msg
    assert "[Image attached: photo.png" in msg
    assert "image/png" in msg
    # The one-shot guidance tells the agent how to act on the path.
    assert MEDIA_GUIDANCE_NOTE in msg
    assert "image_describe" in msg


async def test_media_only_message_still_gets_notes(tmp_path) -> None:
    msg, remaining = await ingest_chat_media("", [_att()], tmp_path)
    assert msg.strip().startswith("[Image attached:")
    assert remaining is None


async def test_identical_resend_reuses_existing_file(tmp_path) -> None:
    msg1, _ = await ingest_chat_media("first", [_att()], tmp_path)
    msg2, _ = await ingest_chat_media("again", [_att()], tmp_path)

    uploads = sorted(p.name for p in (tmp_path / "uploads").iterdir())
    assert uploads == ["photo.png"]  # no photo-2.png duplicate
    assert f"{tmp_path / 'uploads' / 'photo.png'}" in msg2
    assert msg1 != msg2


async def test_same_name_different_bytes_gets_versioned(tmp_path) -> None:
    other = base64.b64encode(b"\x89PNG\r\n\x1a\n-different").decode("ascii")
    await ingest_chat_media("one", [_att()], tmp_path)
    msg, _ = await ingest_chat_media("two", [_att(data=other)], tmp_path)

    uploads = sorted(p.name for p in (tmp_path / "uploads").iterdir())
    assert uploads == ["photo-2.png", "photo.png"]
    assert "photo-2.png" in msg


async def test_traversal_filename_is_jailed_to_uploads(tmp_path) -> None:
    evil = _att(name="../../../etc/evil.png")
    msg, remaining = await ingest_chat_media("x", [evil], tmp_path)

    assert (tmp_path / "uploads" / "evil.png").is_file()
    assert remaining is None
    # Nothing escaped the workspace tmp dir.
    assert not (tmp_path.parent / "etc").exists()
    assert "evil.png" in msg


async def test_extension_derived_from_mime_when_missing(tmp_path) -> None:
    pasted = _att(name="clipboard", mime="image/png")
    await ingest_chat_media("x", [pasted], tmp_path)
    assert (tmp_path / "uploads" / "clipboard.png").is_file()


# ─────────────────────────── failure handling ───────────────────────────────


async def test_bad_base64_becomes_explicit_note(tmp_path) -> None:
    msg, remaining = await ingest_chat_media("x", [_att(data="!!not-base64!!")], tmp_path)
    assert "could not be decoded" in msg
    assert remaining is None
    assert not (tmp_path / "uploads").exists()
    assert MEDIA_GUIDANCE_NOTE not in msg  # nothing was saved


async def test_oversized_media_discarded_with_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chat_attachments, "MAX_MEDIA_BYTES", 4)
    msg, remaining = await ingest_chat_media("x", [_att()], tmp_path)
    assert "exceeds the" in msg and "discarded" in msg
    assert remaining is None
    assert not (tmp_path / "uploads").exists()


async def test_image_save_failure_falls_back_to_inline(tmp_path, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(chat_attachments.Path, "write_bytes", boom)
    att = _att()
    msg, remaining = await ingest_chat_media("x", [att], tmp_path)
    # The wire item survives so the caller's inline-vision path still runs.
    assert remaining == [att]
    assert msg == "x"


# ─────────────────────────── audio & video ──────────────────────────────────


async def test_audio_saved_and_transcribed(tmp_path, monkeypatch) -> None:
    async def fake_transcribe(path: str):
        assert path.endswith("note.ogg")
        return "remind me tomorrow", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    voice = _att(kind="audio", name="note.ogg", mime="audio/ogg")

    msg, remaining = await ingest_chat_media("", [voice], tmp_path)

    assert (tmp_path / "uploads" / "note.ogg").read_bytes() == _PNG_BYTES
    assert remaining is None
    assert "[Audio attached: note.ogg" in msg
    assert "[Voice message transcript] remind me tomorrow" in msg
    # The words lead, and nothing invites the agent to process the recording:
    # a spoken question must run the same turn as the typed one (no second
    # transcription, no "let me look at the file" narration).
    assert msg.startswith("[Voice message transcript] remind me tomorrow")
    assert MEDIA_GUIDANCE_NOTE not in msg
    assert "already transcribed" in msg


async def test_untranscribed_audio_keeps_guidance(tmp_path, monkeypatch) -> None:
    async def fake_transcribe(path: str):
        return None, inbound.STT_DISABLED_NOTE

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    voice = _att(kind="audio", name="note.ogg", mime="audio/ogg")

    msg, _ = await ingest_chat_media("", [voice], tmp_path)
    # No words came back: the file is all the agent has, so it is told how to use it.
    assert MEDIA_GUIDANCE_NOTE in msg


async def test_audio_stt_disabled_note_passthrough(tmp_path, monkeypatch) -> None:
    async def fake_transcribe(path: str):
        return None, inbound.STT_DISABLED_NOTE

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    voice = _att(kind="audio", name="note.ogg", mime="audio/ogg")

    msg, _ = await ingest_chat_media("", [voice], tmp_path)
    assert inbound.STT_DISABLED_NOTE in msg
    assert "[Audio attached: note.ogg" in msg  # path note regardless of STT


async def test_member_voice_note_is_transcribed_and_not_kept(tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    async def fake_transcribe(path: str):
        # The provider reads the format off the name, so the extension survives.
        assert path.endswith("voice-20261017-101500.m4a")
        assert Path(path).read_bytes() == _PNG_BYTES
        seen.append(path)
        return "check the blue box", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    txt = {"kind": "file", "name": "a.txt", "mime": "text/plain", "data": "abc"}
    voice = _att(kind="audio", name="voice-20261017-101500.m4a", mime="audio/mp4")

    msg, remaining = await chat_attachments.transcribe_chat_audio("", [voice, txt])

    assert msg == "[Voice message transcript] check the blue box"
    assert remaining == [txt]
    # Read from a temp file that is gone once the transcript is back.
    assert seen and not Path(seen[0]).exists()


async def test_member_voice_note_without_speech_to_text_says_so(monkeypatch) -> None:
    async def fake_transcribe(path: str):
        return None, inbound.STT_DISABLED_NOTE

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    voice = _att(kind="audio", name="voice.webm", mime="audio/webm")

    msg, remaining = await chat_attachments.transcribe_chat_audio("listen", [voice])

    assert remaining is None
    assert msg == f"listen\n\n{inbound.STT_DISABLED_NOTE}"


async def test_member_bad_audio_payload_is_explicit() -> None:
    voice = _att(kind="audio", name="voice.webm", mime="audio/webm", data="%%%not-base64")
    msg, remaining = await chat_attachments.transcribe_chat_audio("", [voice])
    assert remaining is None
    assert "could not be decoded" in msg


async def test_video_saved_with_transcript_segments(tmp_path, monkeypatch) -> None:
    async def fake_segments(path: str):
        return ["[Video audio transcript] unboxing the gloves"]

    monkeypatch.setattr(inbound, "_video_transcript_segments", fake_segments)
    clip = _att(kind="video", name="clip.mp4", mime="video/mp4")

    msg, remaining = await ingest_chat_media("what's said here?", [clip], tmp_path)

    assert (tmp_path / "uploads" / "clip.mp4").is_file()
    assert remaining is None
    assert "[Video attached: clip.mp4" in msg
    assert "[Video audio transcript] unboxing the gloves" in msg


async def test_mixed_media_and_text_attachments(tmp_path, monkeypatch) -> None:
    async def fake_transcribe(path: str):
        return "hola", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)
    txt = {"kind": "file", "name": "a.txt", "mime": "text/plain", "data": "abc"}
    items = [_att(), txt, _att(kind="audio", name="v.mp3", mime="audio/mpeg")]

    msg, remaining = await ingest_chat_media("do it", items, tmp_path)

    assert remaining == [txt]
    assert (tmp_path / "uploads" / "photo.png").is_file()
    assert (tmp_path / "uploads" / "v.mp3").is_file()
    assert msg.count(MEDIA_GUIDANCE_NOTE) == 1  # guidance appended once


# ────────────────────────────── documents ───────────────────────────────────

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _xlsx_b64(tmp_path) -> str:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Ticker", "Qty"])
    ws.append(["ACME", 1200])
    path = tmp_path / "src.xlsx"
    wb.save(path)
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _docx_b64(tmp_path) -> str:
    import docx

    d = docx.Document()
    d.add_paragraph("Decision: approve the hedge.")
    path = tmp_path / "src.docx"
    d.save(path)
    return base64.b64encode(path.read_bytes()).decode("ascii")


async def test_document_saved_with_read_instruction(tmp_path) -> None:
    ws = tmp_path / "ws"
    item = _att(
        kind="document",
        name="PM Trade Blotter_Eddy.xlsx",
        mime=_XLSX_MIME,
        data=_xlsx_b64(tmp_path),
    )

    msg, remaining = await ingest_chat_media("summarize this", [item], ws)

    saved = ws / "uploads" / "PM_Trade_Blotter_Eddy.xlsx"
    assert saved.is_file()
    assert remaining is None
    assert msg.startswith("summarize this\n\n")
    assert f"[Document attached: PM Trade Blotter_Eddy.xlsx, saved to {saved}" in msg
    assert "document_to_text" in msg
    # The contents stay out of the message (the agent reads the real file), and
    # the vision-oriented image guidance is not added for a document.
    assert "ACME" not in msg
    assert MEDIA_GUIDANCE_NOTE not in msg


async def test_any_binary_file_is_saved(tmp_path) -> None:
    item = _att(kind="document", name="bundle.zip", mime="application/zip", data=_PNG_B64)
    msg, _ = await ingest_chat_media("", [item], tmp_path)
    assert (tmp_path / "uploads" / "bundle.zip").read_bytes() == _PNG_BYTES
    assert msg.startswith("[Attachment: bundle.zip")
    assert "archive" in msg and "unzip -l" in msg


async def test_each_saved_file_names_the_tool_that_reads_it(tmp_path) -> None:
    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    items = [
        _att(kind="document", name="report.pdf", mime="application/pdf", data=b64(b"%PDF-1.4")),
        _att(
            kind="document", name="server.log", mime="text/plain", data=b64(b"GET / 200\n" * 9000)
        ),
        _att(kind="document", name="schema.sql", mime="", data=b64(b"select 1;\n")),
        _att(kind="document", name="firmware.bin", mime="", data=b64(b"\x00\x01\x02")),
    ]
    msg, _ = await ingest_chat_media("", items, tmp_path)
    notes = msg.split("\n\n")

    assert notes[0].startswith("[Document attached: report.pdf") and "document_to_text" in notes[0]
    # Text of any extension, however long, is read with read_file, whole.
    for note, name in ((notes[1], "server.log"), (notes[2], "schema.sql")):
        assert note.startswith(f"[Attachment: {name}") and "read_file" in note
        assert "document_to_text" not in note
    assert notes[3].startswith("[Attachment: firmware.bin") and "binary file" in notes[3]


async def test_member_large_text_file_is_inlined_as_text(tmp_path) -> None:
    data = base64.b64encode(b"SELECT * FROM trades;\n").decode("ascii")
    item = _att(kind="document", name="q.sql", mime="", data=data)
    msg, remaining = await chat_attachments.inline_chat_documents("", [item])
    assert remaining is None
    assert msg == "[Content of q.sql]:\nSELECT * FROM trades;\n"


async def test_document_save_failure_falls_back_to_inline_text(tmp_path, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(chat_attachments, "save_media_bytes", boom)
    item = _att(kind="document", name="report.docx", mime=_DOCX_MIME, data=_docx_b64(tmp_path))

    msg, remaining = await ingest_chat_media("read", [item], tmp_path / "ws")
    assert remaining == [item]
    msg, remaining = await chat_attachments.inline_chat_documents(msg, remaining)

    assert remaining is None
    assert "[Content of report.docx]:" in msg
    assert "Decision: approve the hedge." in msg


async def test_member_documents_inline_extracted_text(tmp_path) -> None:
    txt = {"kind": "file", "name": "a.txt", "mime": "text/plain", "data": "abc"}
    items = [
        _att(kind="document", name="blotter.xlsx", mime=_XLSX_MIME, data=_xlsx_b64(tmp_path)),
        txt,
    ]

    msg, remaining = await chat_attachments.inline_chat_documents("what's in it?", items)

    assert remaining == [txt]
    assert "[Content of blotter.xlsx]:" in msg
    assert "ACME" in msg and "1200" in msg


async def test_member_unreadable_document_gets_a_note(tmp_path) -> None:
    item = _att(kind="document", name="old.doc", mime="application/msword", data=_PNG_B64)
    msg, remaining = await chat_attachments.inline_chat_documents("", [item])
    assert remaining is None
    assert msg.startswith("[Document attached: old.doc")
    assert "no text could be extracted" in msg


async def test_member_bad_document_payload_is_explicit(tmp_path) -> None:
    item = _att(kind="document", name="x.pdf", mime="application/pdf", data="%%%not-base64")
    msg, _ = await chat_attachments.inline_chat_documents("hi", [item])
    assert "could not be decoded" in msg


def test_unprocessed_document_is_never_silent() -> None:
    from olano.runtime.api import _build_multimodal_content

    out = _build_multimodal_content(
        "hi", [_att(kind="document", name="x.pdf", mime="application/pdf")]
    )
    assert isinstance(out, str)
    assert "x.pdf" in out and "could not be read" in out


# ─────────────────────────── sanitize_filename ──────────────────────────────


def test_sanitize_strips_directories_and_unsafe_chars() -> None:
    # Whitespace collapses to "_" — a spaced path would break media_refs
    # token detection and shell quoting downstream.
    assert (
        sanitize_filename("../../x/../y/EVIL name?.PNG", "image/png", "image") == "EVIL_name_.png"
    )
    assert sanitize_filename("C:\\Users\\me\\pic.jpg", "image/jpeg", "image") == "pic.jpg"


def test_sanitize_handles_empty_and_dotty_names() -> None:
    assert sanitize_filename("", "image/jpeg", "image") == "image-attachment.jpg"
    assert sanitize_filename("...", "audio/mpeg", "audio") == "audio-attachment.mp3"


def test_sanitize_caps_stem_length() -> None:
    long = "a" * 200 + ".png"
    out = sanitize_filename(long, "image/png", "image")
    assert out.endswith(".png") and len(out) <= 64 + len(".png")
