"""Automatic media attachment from file references in agent replies.

An agent that mentions a file it produced should get that file delivered as a
native attachment on whatever platform the conversation is on — no notification
tool call required. Models overwhelmingly write that reference as a
``file:///…`` URI, and ``extract_local_files`` used to ignore that form
entirely (its lookbehind exists to skip ``https://…/x.png`` and excluded
``file://`` as collateral). The user then received the URI as literal text with
no image.

The explicit path had the mirror-image bug: ``send_media`` checked
``os.path.exists`` against the raw reference, so a ``file://`` URI missed the
local branch, fell through to "remote URL", was rejected by the SSRF guard, and
was silently downgraded to a text message — returning a platform message id that
a model then reported to the user as an HTTP status code
(2026-07-29: "delivery failed with HTTP 451").

Also covers container paths: under ``OlanoDockerBackend`` the agent's
workspace is bind-mounted at ``/workspace``, so paths it reports do not exist on
the host until they are re-rooted.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from olano.platforms.base import BasePlatformAdapter, SendResult
from olano.platforms.media_paths import (
    media_kind,
    resolve_local_media,
    strip_file_uri,
)


class _Recorder(BasePlatformAdapter):
    """Adapter that records which native send each reference routed to."""

    name = "Rec"

    def __init__(self, agent_name: str = "tester") -> None:
        self.agent_name = agent_name
        self.calls: list[tuple[str, str]] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        self.calls.append(("text", content))
        return SendResult(success=True, message_id="t1")

    async def send_image_file(self, chat_id, image_path, caption=None, **kw) -> SendResult:
        self.calls.append(("photo", image_path))
        return SendResult(success=True, message_id="p1")

    async def send_video(self, chat_id, video_path, caption=None, **kw) -> SendResult:
        self.calls.append(("video", video_path))
        return SendResult(success=True, message_id="v1")

    async def send_voice(self, chat_id, audio_path, caption=None, **kw) -> SendResult:
        self.calls.append(("voice", audio_path))
        return SendResult(success=True, message_id="a1")

    async def send_document(self, chat_id, file_path, caption=None, **kw) -> SendResult:
        self.calls.append(("document", file_path))
        return SendResult(success=True, message_id="d1")


# ── file:// URI normalization ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("file:///a/b.png", "/a/b.png"),
        ("file://localhost/a/b.png", "/a/b.png"),
        ("file:/a/b.png", "/a/b.png"),
        ("file:///a/my%20file.png", "/a/my file.png"),
        ("<file:///a/b.png>", "/a/b.png"),
        ("/a/b.png", "/a/b.png"),  # plain paths pass through
        ("https://x.test/b.png", "https://x.test/b.png"),  # URLs untouched
    ],
)
def test_strip_file_uri_forms(uri, expected):
    assert strip_file_uri(uri) == expected


@pytest.mark.parametrize(
    "name,kind",
    [
        ("a.png", "image"),
        ("a.JPG", "image"),
        ("a.mp4", "video"),
        ("a.mp3", "audio"),
        ("a.ogg", "audio"),
        ("a.pdf", "document"),
        ("a.csv", "document"),
        ("a.unknown", "document"),
    ],
)
def test_media_kind_routing(name, kind):
    assert media_kind(name) == kind


# ── implicit attach: the agent just mentions the file ────────────────────────


def test_file_uri_in_reply_is_attached_and_stripped(tmp_path):
    """The reported bug: a file:// URI reached the user as text, not an image."""
    img = tmp_path / "draft.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    files, cleaned = adapter.extract_local_files(
        f"X draft visual for review. Not published or scheduled.\nfile://{img}"
    )

    assert files == [str(img)]
    assert cleaned == "X draft visual for review. Not published or scheduled."
    assert "file://" not in cleaned


def test_bare_path_still_attached(tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    files, cleaned = adapter.extract_local_files(f"Report ready {img} — take a look")

    assert files == [str(img)]
    assert "take a look" in cleaned


def test_documents_and_audio_are_attached(tmp_path):
    """Not just images/video — the toolkit promises photo, document, video, audio."""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"ID3")
    adapter = _Recorder()

    files, _ = adapter.extract_local_files(f"Deck: file://{pdf} and audio {mp3}")

    assert set(files) == {str(pdf), str(mp3)}


def test_same_file_twice_sends_once_but_strips_both(tmp_path):
    img = tmp_path / "one.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    files, cleaned = adapter.extract_local_files(f"See file://{img} or {img}")

    assert files == [str(img)]
    assert str(img) not in cleaned


def test_urls_code_spans_and_missing_files_are_ignored(tmp_path):
    img = tmp_path / "real.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    for text in (
        "A remote image https://example.test/pic.png stays text",
        f"In code: `file://{img}`",
        "Invented: file:///nope/ghost.png",
    ):
        files, cleaned = adapter.extract_local_files(text)
        assert files == [], text
        assert cleaned == text.strip(), text


def test_credential_shaped_paths_are_never_auto_attached(tmp_path):
    """Existence is not enough on the implicit path — a mention must not leak keys."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "id_rsa.txt"
    key.write_text("PRIVATE KEY")
    adapter = _Recorder()

    files, _ = adapter.extract_local_files(f"my key is at {key}")

    assert files == []


# ── OlanoDockerBackend: container path → host path ────────────────────────


def test_container_path_maps_to_host_workspace(tmp_path, monkeypatch):
    """A sandboxed agent reports /workspace/…; the host file lives elsewhere."""
    from olano.platforms import media_paths

    (tmp_path / "out").mkdir()
    img = tmp_path / "out" / "render.png"
    img.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    assert resolve_local_media("/workspace/out/render.png", agent_name="boxed") == str(img)
    assert resolve_local_media("file:///workspace/out/render.png", agent_name="boxed") == str(img)


def test_container_path_cannot_escape_workspace(tmp_path, monkeypatch):
    from olano.platforms import media_paths

    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    assert resolve_local_media("/workspace/../outside.png", agent_name="boxed") is None


# ── explicit tool path: send_media routing ───────────────────────────────────


async def test_send_media_routes_file_uri_to_native_photo(tmp_path):
    """The 451 bug: file:// missed the local branch and became a text message."""
    img = tmp_path / "draft.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    msg_id = await adapter.send_media(f"file://{img}", "image", "123", caption="cap")

    assert msg_id == "p1"
    assert adapter.calls == [("photo", str(img))]


async def test_send_media_uses_file_type_over_generic_document(tmp_path):
    """A .png sent via notification_send_document still arrives viewable."""
    img = tmp_path / "draft.png"
    img.write_bytes(b"\x89PNG\r\n")
    adapter = _Recorder()

    await adapter.send_media(str(img), "document", "123")

    assert adapter.calls == [("photo", str(img))]


@pytest.mark.parametrize(
    "filename,expected",
    [("clip.mp4", "video"), ("note.mp3", "voice"), ("report.pdf", "document")],
)
async def test_send_media_routes_each_kind(tmp_path, filename, expected):
    target = tmp_path / filename
    target.write_bytes(b"data")
    adapter = _Recorder()

    await adapter.send_media(f"file://{target}", "", "123")

    assert adapter.calls == [(expected, str(target))]


async def test_send_media_raises_on_missing_local_file(tmp_path):
    """A dead local reference must not 'succeed' as a text message."""
    adapter = _Recorder()

    with pytest.raises(RuntimeError, match="no such file"):
        await adapter.send_media("file:///nope/ghost.png", "image", "123")

    assert adapter.calls == []


async def test_remote_urls_still_go_through_url_send(tmp_path):
    """Normalization must not break genuine remote media."""
    adapter = _Recorder()

    await adapter.send_media("https://example.test/pic.png", "document", "123", caption="c")

    # No local branch taken; falls back to the base link-as-text behaviour.
    assert adapter.calls and adapter.calls[0][0] == "text"


def test_extract_is_available_to_every_platform_adapter():
    """The pipeline lives on the base class — no adapter opts out of it."""
    assert asyncio.iscoroutinefunction(BasePlatformAdapter.send_media)
    assert callable(BasePlatformAdapter.extract_local_files)


# ── defang: command output must never auto-attach ────────────────────────────


def test_defang_wraps_attachable_refs_and_extractor_skips_them(tmp_path):
    """A command reply naming a real file keeps the path as text, not a file."""
    from olano.platforms.media_paths import defang_local_file_refs

    doc = tmp_path / "plan.md"
    doc.write_text("roadmap")
    text = f"Roadmap lives at {doc}, review weekly."

    defanged = defang_local_file_refs(text)

    assert defanged == f"Roadmap lives at `{doc}`, review weekly."
    files, cleaned = _Recorder().extract_local_files(defanged)
    assert files == []
    assert cleaned == defanged


def test_defang_wraps_file_uris(tmp_path):
    from olano.platforms.media_paths import defang_local_file_refs

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")

    assert defang_local_file_refs(f"see file://{img} now") == f"see `file://{img}` now"


def test_defang_leaves_safe_text_untouched(tmp_path):
    """Missing files, code spans, URLs and plain prose pass through verbatim."""
    from olano.platforms.media_paths import defang_local_file_refs

    img = tmp_path / "real.png"
    img.write_bytes(b"\x89PNG\r\n")

    for text in (
        "Invented /nope/ghost.png stays as-is",
        f"already safe: `{img}`",
        f"```\n{img}\n```",
        "url https://example.test/pic.png untouched",
        "no paths here at all",
    ):
        assert defang_local_file_refs(text) == text, text


def test_defang_is_idempotent(tmp_path):
    from olano.platforms.media_paths import defang_local_file_refs

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    once = defang_local_file_refs(f"file at {img} end")
    assert once == f"file at `{img}` end"
    assert defang_local_file_refs(once) == once


# ── workspace-relative references ────────────────────────────────────────────


def test_workspace_relative_path_is_attached_and_stripped(tmp_path, monkeypatch):
    """`Saved: reports/chart.png` — the hand-off form the prompt teaches — must
    deliver the file on a messenger exactly like an absolute path does."""
    from olano.platforms import media_paths

    (tmp_path / "reports").mkdir()
    img = tmp_path / "reports" / "chart.png"
    img.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    adapter = _Recorder(agent_name="boxed")
    files, cleaned = adapter.extract_local_files("Here is the chart.\nSaved: reports/chart.png")

    assert files == [str(img)]
    assert "reports/chart.png" not in cleaned
    assert "Here is the chart." in cleaned


def test_workspace_relative_path_cannot_escape_the_workspace(tmp_path, monkeypatch):
    from olano.platforms import media_paths

    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    adapter = _Recorder(agent_name="boxed")
    files, cleaned = adapter.extract_local_files("Saved: reports/../../outside.pdf")
    assert files == []
    assert resolve_local_media("reports/../../outside.pdf", agent_name="boxed") is None


def test_bare_file_name_is_not_a_relative_reference(tmp_path, monkeypatch):
    """Prose that mentions `logo.png` must not upload it: attaching needs a
    directory component (or an absolute path)."""
    from olano.platforms import media_paths

    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    adapter = _Recorder(agent_name="boxed")
    files, _ = adapter.extract_local_files("Use logo.png on the slide.")
    assert files == []


def test_relative_reference_inside_a_code_span_is_left_alone(tmp_path, monkeypatch):
    from olano.platforms import media_paths

    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "chart.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    adapter = _Recorder(agent_name="boxed")
    files, cleaned = adapter.extract_local_files("Run `open reports/chart.png` yourself.")
    assert files == []
    assert cleaned == "Run `open reports/chart.png` yourself."


def test_defang_wraps_a_relative_reference_that_would_attach(tmp_path, monkeypatch):
    from olano.platforms import media_paths
    from olano.platforms.media_paths import defang_local_file_refs

    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "chart.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(media_paths, "_agent_workspace_root", lambda name: tmp_path)

    out = defang_local_file_refs("Latest: reports/chart.png", agent_name="boxed")
    assert out == "Latest: `reports/chart.png`"
