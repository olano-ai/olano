"""Tests for inbound attachment handling (:func:`build_agent_message`).

Every platform adapter (Telegram, WhatsApp, Discord, Slack, …) downloads voice
notes and attachments to a local cache and records them on
``MessageEvent.media_urls`` / ``media_types``. The shared inbound layer folds
those into the payload streamed to the agent: audio is transcribed to text,
images are copied into the agent workspace's ``uploads/`` dir and referenced
by a saved-path note (same contract as dashboard chat attachments — the agent
views them via a vision tool or uses the path directly), binary documents get
their text extracted inline, and every file (documents, videos, unknown types)
is copied into ``uploads/`` the same way.
These tests pin that behaviour without hitting the network (the STT call is
patched).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import olano.platforms.inbound as inbound
from olano.platforms.inbound import build_agent_message

# A 1x1 PNG so the image path exercises real bytes / base64 encoding.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@dataclass
class FakeEvent:
    """Minimal stand-in for MessageEvent (only the fields the helper reads)."""

    text: str = ""
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    reply_to_media_urls: list[str] = field(default_factory=list)
    reply_to_media_types: list[str] = field(default_factory=list)


def _write_png(tmp_path: Any, name: str = "photo.png") -> str:
    p = tmp_path / name
    p.write_bytes(_PNG_BYTES)
    return str(p)


async def test_no_media_returns_text_unchanged() -> None:
    event = FakeEvent(text="hello there")
    result = await build_agent_message("hello there", event)
    assert result == "hello there"


# ───────────────────────────── voice / audio ────────────────────────────────


async def test_voice_transcript_merged_into_text(monkeypatch) -> None:
    async def fake_transcribe(path: str) -> tuple[str | None, str | None]:
        assert path == "/cache/audio_abc.ogg"
        return "what is the weather in Paris", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)

    event = FakeEvent(media_urls=["/cache/audio_abc.ogg"], media_types=["audio/ogg"])
    # Empty text is the real-world voice-note case (no caption/body).
    result = await build_agent_message("", event)

    assert isinstance(result, str)
    assert "what is the weather in Paris" in result
    assert "[Voice message transcript]" in result


async def test_voice_transcript_appended_after_existing_text(monkeypatch) -> None:
    async def fake_transcribe(path: str) -> tuple[str | None, str | None]:
        return "second part spoken", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)

    event = FakeEvent(media_urls=["/cache/a.ogg"], media_types=["audio/ogg"])
    result = await build_agent_message("[from @bob]: typed caption", event)

    assert isinstance(result, str)
    # Original caption preserved, transcript appended after it.
    assert result.startswith("[from @bob]: typed caption")
    assert "second part spoken" in result


async def test_voice_failure_injects_note(monkeypatch) -> None:
    async def fake_transcribe(path: str) -> tuple[str | None, str | None]:
        return None, inbound.STT_FAILED_NOTE  # provider error / missing key

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)

    event = FakeEvent(media_urls=["/cache/a.ogg"], media_types=["audio/ogg"])
    result = await build_agent_message("", event)

    # The agent must learn a voice message arrived rather than see an empty
    # prompt (the original bug: empty message → generic reply).
    assert isinstance(result, str)
    assert result.strip() != ""
    assert "voice message" in result.lower()
    assert "could not be transcribed" in result.lower()


async def test_voice_disabled_by_default_injects_resend_note(tmp_path, monkeypatch) -> None:
    """Real path, no monkeypatched transcriber: STT defaults to disabled, so the
    agent is told to ask the sender to resend as text — even when an API key
    happens to be set in the environment (config opt-in wins)."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))  # empty config.yaml
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LIONCLAW_STT_PROVIDER", raising=False)
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake-ogg")

    event = FakeEvent(media_urls=[str(audio)], media_types=["audio/ogg"])
    result = await build_agent_message("", event)

    assert isinstance(result, str)
    assert "speech-to-text is disabled" in result.lower()
    assert "couldn't transcribe, please resend as text" in result.lower()


# ─────────────────────────────── images ─────────────────────────────────────


async def test_image_saved_to_workspace_uploads_with_note(tmp_path) -> None:
    """An inbound photo is copied into <workspace>/uploads/ and noted by path."""
    from olano.uploads import MEDIA_GUIDANCE_NOTE

    cache = tmp_path / "cache"
    cache.mkdir()
    img = _write_png(cache)
    ws = tmp_path / "ws"

    event = FakeEvent(text="what is this?", media_urls=[img], media_types=["image/png"])
    result = await build_agent_message("what is this?", event, workspace_root=ws)

    assert isinstance(result, str)
    saved = ws / "uploads" / "photo.png"
    assert saved.read_bytes() == _PNG_BYTES
    assert result.startswith("what is this?")
    assert f"saved to {saved}" in result
    assert "[Image attached: photo.png" in result
    # The one-shot guidance tells the agent how to act on the path.
    assert MEDIA_GUIDANCE_NOTE in result
    # The cached original is untouched (adapters manage their own cleanup).
    assert Path(img).is_file()


async def test_image_without_workspace_notes_cache_path(tmp_path) -> None:
    """No workspace known → the note points at the adapter's cached file."""
    img = _write_png(tmp_path)

    event = FakeEvent(text="", media_urls=[img], media_types=["image/png"])
    result = await build_agent_message("", event)

    assert isinstance(result, str)
    assert f"saved to {img}" in result
    assert "[Image attached: photo.png" in result


async def test_image_copy_failure_falls_back_to_cache_path(tmp_path, monkeypatch) -> None:
    import olano.uploads as uploads

    def boom(*a: Any, **k: Any):
        raise OSError("disk full")

    monkeypatch.setattr(uploads, "save_media_bytes", boom)
    img = _write_png(tmp_path)

    event = FakeEvent(text="x", media_urls=[img], media_types=["image/png"])
    result = await build_agent_message("x", event, workspace_root=tmp_path / "ws")

    assert isinstance(result, str)
    assert f"saved to {img}" in result  # cache path, image never silent


async def test_missing_image_file_skipped_gracefully() -> None:
    event = FakeEvent(text="look", media_urls=["/does/not/exist.jpg"], media_types=["image/jpeg"])
    result = await build_agent_message("look", event)
    # No readable image → falls back to the plain text string.
    assert result == "look"


async def test_voice_and_image_combined(tmp_path, monkeypatch) -> None:
    async def fake_transcribe(path: str) -> tuple[str | None, str | None]:
        return "describe the attached picture", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)

    img = _write_png(tmp_path)
    event = FakeEvent(
        media_urls=["/cache/a.ogg", img],
        media_types=["audio/ogg", "image/png"],
    )
    result = await build_agent_message("", event, workspace_root=tmp_path / "ws")

    assert isinstance(result, str)
    assert "describe the attached picture" in result
    assert "[Image attached: photo.png" in result
    assert (tmp_path / "ws" / "uploads" / "photo.png").is_file()


# ────────────────────────────── documents ───────────────────────────────────


async def test_docx_content_extracted_inline(tmp_path) -> None:
    import docx

    doc_path = tmp_path / "doc_abc123_notes.docx"
    d = docx.Document()
    d.add_paragraph("quarterly revenue grew 40 percent")
    d.save(doc_path)

    event = FakeEvent(
        text="summarize this",
        media_urls=[str(doc_path)],
        media_types=["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    )
    result = await build_agent_message("summarize this", event)

    assert isinstance(result, str)
    assert "quarterly revenue grew 40 percent" in result
    # Cache-prefix stripped in the display name.
    assert "[Content of notes.docx]" in result


async def test_pdf_without_text_layer_gets_saved_note(tmp_path) -> None:
    """Scanned/blank PDFs (no extractable text) → path note, not silence."""
    import pypdf

    pdf_path = tmp_path / "scan.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as fh:
        writer.write(fh)

    event = FakeEvent(text="", media_urls=[str(pdf_path)], media_types=["application/pdf"])
    result = await build_agent_message("", event)

    assert isinstance(result, str)
    assert "no text could be extracted" in result
    assert str(pdf_path) in result


async def test_document_text_truncated(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(inbound, "DOC_INJECT_MAX_CHARS", 50)
    import docx

    doc_path = tmp_path / "big.docx"
    d = docx.Document()
    d.add_paragraph("x" * 500)
    d.save(doc_path)

    event = FakeEvent(text="", media_urls=[str(doc_path)], media_types=["application/msword"])
    result = await build_agent_message("", event)
    assert isinstance(result, str)
    assert "truncated" in result
    assert "x" * 51 not in result


# ─────────────────────────────── video ──────────────────────────────────────


async def test_video_gets_saved_path_note(tmp_path) -> None:
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"not-a-real-video")

    event = FakeEvent(text="", media_urls=[str(vid)], media_types=["video/mp4"])
    result = await build_agent_message("", event)

    # ffmpeg/STT may be unavailable in CI — the saved-path note must always be
    # present so the agent knows a video arrived.
    assert isinstance(result, str)
    assert "[Video attachment saved at" in result
    assert str(vid) in result


# ─────────────────────────── other attachments ──────────────────────────────


async def test_unknown_attachment_gets_note(tmp_path) -> None:
    zpath = tmp_path / "doc_ff00aa_archive.zip"
    zpath.write_bytes(b"PK\x03\x04")
    event = FakeEvent(
        text="here is a file",
        media_urls=[str(zpath)],
        media_types=["application/zip"],
    )
    result = await build_agent_message("here is a file", event)
    assert isinstance(result, str)
    assert result.startswith("here is a file")
    assert "[Attachment saved: archive.zip" in result


# ─────────────────────── workspace copies of files ──────────────────────────
#
# The reported gap: a pre-trade Excel pack sent on WhatsApp reached the agent
# as a 50-rows-per-sheet text extract plus a path in the adapter cache, which
# sits outside the workspace a sandboxed agent can open. The file itself has to
# land in uploads/ the way an image does.


async def test_spreadsheet_is_copied_into_workspace_and_extracted(tmp_path) -> None:
    import openpyxl

    cache = tmp_path / "cache"
    cache.mkdir()
    xlsx = cache / "doc_0a1b2c_PretradePack_20260929.xlsx"
    wb = openpyxl.Workbook()
    ws_sheet = wb.active
    ws_sheet["A1"] = "Trade ID"
    ws_sheet["A2"] = "GCF1-20260929-001"
    ws_sheet["B2"] = "=1+1"
    wb.save(xlsx)
    ws = tmp_path / "ws"

    event = FakeEvent(
        text="new pack",
        media_urls=[str(xlsx)],
        media_types=["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
    )
    result = await build_agent_message("new pack", event, workspace_root=ws)

    saved = ws / "uploads" / "PretradePack_20260929.xlsx"
    assert saved.read_bytes() == xlsx.read_bytes()
    assert "GCF1-20260929-001" in result
    assert f"[Original file saved to {saved}" in result
    # The workspace copy is the real file, formulas included.
    assert openpyxl.load_workbook(saved)["Sheet"]["B2"].value == "=1+1"
    assert "Note: the attached file(s) above are saved in your workspace" in result


async def test_document_without_text_still_copied(tmp_path) -> None:
    import pypdf

    pdf_path = tmp_path / "doc_00ff11_confirmation.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as fh:
        writer.write(fh)
    ws = tmp_path / "ws"

    event = FakeEvent(text="", media_urls=[str(pdf_path)], media_types=["application/pdf"])
    result = await build_agent_message("", event, workspace_root=ws)

    saved = ws / "uploads" / "confirmation.pdf"
    assert saved.is_file()
    assert f"at {saved}" in result
    assert "no text could be extracted" in result


async def test_truncated_document_names_the_workspace_copy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(inbound, "DOC_INJECT_MAX_CHARS", 50)
    import docx

    doc_path = tmp_path / "big.docx"
    d = docx.Document()
    d.add_paragraph("x" * 500)
    d.save(doc_path)
    ws = tmp_path / "ws"

    event = FakeEvent(text="", media_urls=[str(doc_path)], media_types=["application/msword"])
    result = await build_agent_message("", event, workspace_root=ws)
    assert f"full file saved at {ws / 'uploads' / 'big.docx'}" in result


async def test_video_and_other_files_are_copied(tmp_path) -> None:
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"not-a-real-video")
    zpath = tmp_path / "doc_ff00aa_archive.zip"
    zpath.write_bytes(b"PK\x03\x04")
    ws = tmp_path / "ws"

    event = FakeEvent(
        text="",
        media_urls=[str(vid), str(zpath)],
        media_types=["video/mp4", "application/zip"],
    )
    result = await build_agent_message("", event, workspace_root=ws)

    assert f"[Video attachment saved at {ws / 'uploads' / 'clip.mp4'}" in result
    assert f"[Attachment saved: archive.zip at {ws / 'uploads' / 'archive.zip'}" in result
    assert (ws / "uploads" / "archive.zip").read_bytes() == b"PK\x03\x04"


async def test_oversized_file_stays_in_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(inbound, "STASH_MAX_BYTES", 3)
    zpath = tmp_path / "big.zip"
    zpath.write_bytes(b"PK\x03\x04")
    ws = tmp_path / "ws"

    event = FakeEvent(text="", media_urls=[str(zpath)], media_types=["application/zip"])
    result = await build_agent_message("", event, workspace_root=ws)

    assert f"at {zpath}" in result
    assert not (ws / "uploads").exists()
    # Nothing was copied, so no "saved in your workspace" guidance either.
    assert "saved in your workspace" not in result


# ──────────────────────── quoted (replied-to) media ─────────────────────────
#
# The reported bug: on WhatsApp, replying to a photo reached the agent as
# "[replying to an earlier message] (no text — media or unavailable)" and the
# photo the whole message was about never arrived. The renderer names the kind
# (tests/test_reply_context.py); these pin that the file itself is folded into
# the turn, and that it is never mistaken for something the sender just sent.


async def test_quoted_image_is_saved_and_labelled_as_quoted(tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    img = _write_png(cache, "quoted.png")
    ws = tmp_path / "ws"

    event = FakeEvent(
        text="what does this say?",
        reply_to_media_urls=[img],
        reply_to_media_types=["image/png"],
    )
    result = await build_agent_message("what does this say?", event, workspace_root=ws)

    saved = ws / "uploads" / "quoted.png"
    assert saved.read_bytes() == _PNG_BYTES
    assert f"saved to {saved}" in result
    # The wording is what tells the agent which message the file belongs to.
    assert "[Image on the quoted message: quoted.png" in result
    assert "[Image attached:" not in result


async def test_quoted_voice_note_is_transcribed_under_quoted_wording(monkeypatch) -> None:
    async def fake_transcribe(path: str) -> tuple[str | None, str | None]:
        return "meet me at six", None

    monkeypatch.setattr(inbound, "_transcribe_audio", fake_transcribe)

    event = FakeEvent(reply_to_media_urls=["/cache/q.ogg"], reply_to_media_types=["audio/ogg"])
    result = await build_agent_message("what did she say?", event)

    assert "[Quoted voice message transcript] meet me at six" in result
    assert "[Voice message transcript]" not in result


async def test_quoted_media_precedes_the_senders_own(tmp_path, monkeypatch) -> None:
    # Ordering matters: the quoted file is what the message is *about*, and
    # the reply-context block above the body has already announced it.
    cache = tmp_path / "cache"
    cache.mkdir()
    quoted_img = _write_png(cache, "theirs.png")
    own_img = _write_png(cache, "mine.png")

    event = FakeEvent(
        media_urls=[own_img],
        media_types=["image/png"],
        reply_to_media_urls=[quoted_img],
        reply_to_media_types=["image/png"],
    )
    result = await build_agent_message("same thing?", event, workspace_root=tmp_path / "ws")

    assert result.index("theirs.png") < result.index("mine.png")
    assert "[Image on the quoted message: theirs.png" in result
    assert "[Image attached: mine.png" in result


async def test_quoted_image_alone_still_gets_the_guidance_note(tmp_path) -> None:
    # The note is what tells the agent to call a vision tool on the path. A
    # message whose only image is the quoted one needs it just as much.
    from olano.uploads import MEDIA_GUIDANCE_NOTE

    img = _write_png(tmp_path)
    event = FakeEvent(reply_to_media_urls=[img], reply_to_media_types=["image/png"])
    result = await build_agent_message("read it", event, workspace_root=tmp_path / "ws")

    assert MEDIA_GUIDANCE_NOTE in result


async def test_no_quoted_media_leaves_text_untouched() -> None:
    # Every ordinary reply. The common path must stay a cheap string.
    event = FakeEvent(text="sure")
    assert await build_agent_message("sure", event) == "sure"


# Provider selection / config semantics live in tests/test_speech.py.
