"""Inbound WhatsApp media reaches the agent — the voice-note path above all.

WhatsApp serves media encrypted (a CDN url plus a ``mediaKey`` only Baileys can
use), so the Python adapter can never fetch an attachment itself: the Node
bridge decrypts it, writes a file, and reports the absolute path on
``mediaUrls``. The bridge used to report ``mediaUrls: []`` unconditionally, and
a voice note carries no text body — so an inbound voice message reached the
runner as a **0-char** prompt and speech-to-text never saw a file. Telegram was
unaffected because its adapter downloads via ``msg.voice.get_file()``.

These tests pin the adapter half of that contract:

* a bridge-supplied absolute path becomes ``media_urls`` + a *truthful*
  ``media_types`` entry, which is what ``inbound.build_agent_message`` keys on
  to transcribe (``mime.startswith("audio/")``);
* the mime follows the file extension, because the speech provider infers the
  audio container from the filename it is posted under and
  ``uploads.sanitize_filename`` derives a saved image's extension from its mime
  — a `.mp3` labelled ``audio/ogg`` is a rejected transcription, and a PNG
  labelled ``image/jpeg`` is a ``.jpg`` holding PNG bytes;
* an attachment the bridge could **not** hand over is never silent: the agent
  gets a note telling the sender to resend, rather than an empty message.
"""

from __future__ import annotations

from typing import Any

import pytest

from olano.platforms.base import MessageType
from olano.platforms.config import PlatformConfig
from olano.platforms.whatsapp import WhatsAppAdapter


def _adapter(tmp_path, monkeypatch) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_LISTEN_ONLY", raising=False)
    monkeypatch.delenv("WHATSAPP_REQUIRE_MENTION", raising=False)
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra={}), agent_name="andrew")


def _voice_event(path: str, mime: str = "audio/ogg; codecs=opus", **over: Any) -> dict[str, Any]:
    """A bridge payload for a DM voice note — no body, media on disk."""
    data: dict[str, Any] = {
        "messageId": "WAMSG1",
        "chatId": "12025550143@s.whatsapp.net",
        "chatName": "Chak",
        "isGroup": False,
        "senderId": "12025550143@s.whatsapp.net",
        "senderName": "Chak",
        "body": "",
        "hasMedia": True,
        "mediaType": mime,
        "mediaUrls": [path] if path else [],
        "mediaMime": mime,
        "mediaFileName": "",
        "botIds": ["6511111111@s.whatsapp.net"],
    }
    data.update(over)
    return data


async def test_voice_note_reaches_the_agent_as_audio_media(tmp_path, monkeypatch):
    """The regression: a voice note must arrive with a transcribable file."""
    audio = tmp_path / "audio_deadbeefcafe.ogg"
    audio.write_bytes(b"OggS-fake")

    adapter = _adapter(tmp_path, monkeypatch)
    event = await adapter._build_message_event(_voice_event(str(audio)))

    assert event is not None
    assert event.message_type is MessageType.VOICE
    assert event.media_urls == [str(audio)]
    # What build_agent_message() branches on to call the speech layer.
    assert event.media_types == ["audio/ogg"]
    assert event.media_types[0].startswith("audio/")


async def test_transcribable_payload_is_built_from_the_event(tmp_path, monkeypatch):
    """End of the chain: the empty body becomes a transcript, not 0 chars."""
    from olano.platforms import inbound

    audio = tmp_path / "audio_deadbeefcafe.ogg"
    audio.write_bytes(b"OggS-fake")

    adapter = _adapter(tmp_path, monkeypatch)
    event = await adapter._build_message_event(_voice_event(str(audio)))
    assert event is not None

    monkeypatch.setattr(inbound, "_transcribe_audio", _fake_transcribe)
    payload = await inbound.build_agent_message(event.text, event, workspace_root=None)

    assert "[Voice message transcript] hello from whatsapp" in payload
    assert payload.strip(), "a voice note must never reach the agent as an empty prompt"


async def _fake_transcribe(path: str) -> tuple[str | None, str | None]:
    return "hello from whatsapp", None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("audio_1.ogg", "audio/ogg"),
        ("audio_1.opus", "audio/ogg"),
        # A forwarded music file: mislabelling this `audio/ogg` is what the
        # speech provider rejects, since it reads the container off the name.
        ("audio_1.mp3", "audio/mpeg"),
        ("audio_1.m4a", "audio/mp4"),
        ("audio_1.wav", "audio/wav"),
    ],
)
async def test_audio_mime_follows_the_file_extension(
    tmp_path, monkeypatch, filename: str, expected: str
):
    path = tmp_path / filename
    path.write_bytes(b"x")
    adapter = _adapter(tmp_path, monkeypatch)

    event = await adapter._build_message_event(_voice_event(str(path), mime="audio/mpeg"))

    assert event is not None
    assert event.media_types == [expected]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("img_1.jpg", "image/jpeg"), ("img_1.png", "image/png"), ("img_1.webp", "image/webp")],
)
async def test_image_mime_follows_the_file_extension(
    tmp_path, monkeypatch, filename: str, expected: str
):
    """The bridge reports `image/jpeg` for every image; the file knows better."""
    path = tmp_path / filename
    path.write_bytes(b"x")
    adapter = _adapter(tmp_path, monkeypatch)

    event = await adapter._build_message_event(
        _voice_event(str(path), mime="image/jpeg", body="look at this")
    )

    assert event is not None
    assert event.message_type is MessageType.PHOTO
    assert event.media_types == [expected]


async def test_unknown_extension_falls_back_to_the_branch_default(tmp_path, monkeypatch):
    """An older bridge, or a container we don't map, keeps the old behaviour."""
    path = tmp_path / "audio_1.weird"
    path.write_bytes(b"x")
    adapter = _adapter(tmp_path, monkeypatch)

    payload = _voice_event(str(path))
    payload.pop("mediaMime")  # pre-`mediaMime` bridge
    event = await adapter._build_message_event(payload)

    assert event is not None
    assert event.media_types == ["audio/ogg"]


async def test_declared_mime_cannot_move_media_between_families(tmp_path, monkeypatch):
    """A sender-set mimetype must not turn a voice note into an image."""
    path = tmp_path / "audio_1.weird"
    path.write_bytes(b"x")
    adapter = _adapter(tmp_path, monkeypatch)

    event = await adapter._build_message_event(
        _voice_event(str(path), mime="audio/ogg", mediaMime="image/png")
    )

    assert event is not None
    assert event.media_types == ["audio/ogg"]


async def test_unretrievable_attachment_is_never_silent(tmp_path, monkeypatch):
    """No file (oversized, expired, failed, or an old bridge) ⇒ an explicit note.

    This is the guard on the original symptom: `chars=0` into the runner.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    event = await adapter._build_message_event(_voice_event(""))

    assert event is not None
    assert event.media_urls == []
    assert event.text.strip(), "an empty prompt is the bug this test exists for"
    assert "voice message" in event.text
    assert "resend" in event.text


async def test_unretrievable_attachment_keeps_the_caption(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    event = await adapter._build_message_event(
        _voice_event("", mime="image/jpeg", body="what is in this photo?")
    )

    assert event is not None
    assert "what is in this photo?" in event.text
    assert "photo" in event.text


async def test_text_message_is_untouched(tmp_path, monkeypatch):
    """No media ⇒ no note, no media lists: the common path stays exactly as-is."""
    adapter = _adapter(tmp_path, monkeypatch)
    event = await adapter._build_message_event(
        {
            "messageId": "WAMSG2",
            "chatId": "12025550143@s.whatsapp.net",
            "isGroup": False,
            "senderId": "12025550143@s.whatsapp.net",
            "body": "just text",
            "hasMedia": False,
            "mediaType": "",
            "mediaUrls": [],
            "botIds": [],
        }
    )

    assert event is not None
    assert event.text == "just text"
    assert event.message_type is MessageType.TEXT
    assert event.media_urls == []


# ---------------------------------------------------------------------------
# Adopting a pre-fix bridge
#
# The bridge is spawned with `setsid`, so it outlives the runtime that started
# it: an upgrade ships new bridge code and `connect()` happily re-adopts the
# still-running old process, which means every voice note keeps arriving empty
# after the fix is deployed. The capability probe is what forces a restart.
# ---------------------------------------------------------------------------


class _Payload:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self) -> _Payload:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Session:
    def __init__(self, resp: Any) -> None:
        self._resp = resp

    def get(self, *a: Any, **kw: Any) -> Any:
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _patch_root_probe(monkeypatch, resp: Any) -> None:
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session(resp))


@pytest.mark.parametrize(
    ("resp", "expected", "why"),
    [
        (
            _Payload(
                200,
                {"capabilities": ["send", "voice-notes", "inbound-media", "quoted-media"]},
            ),
            False,
            "current bridge is reused",
        ),
        (
            _Payload(200, {"capabilities": ["send", "edit", "send-media", "voice-notes"]}),
            True,
            "pre-fix bridge is restarted",
        ),
        (
            # Every required capability is checked, not just the first one
            # added: a bridge that decrypts a voice note but not the photo a
            # reply points at is still older than the code driving it.
            _Payload(200, {"capabilities": ["send", "voice-notes", "inbound-media"]}),
            True,
            "bridge without quoted-media is restarted",
        ),
        (_Payload(404, None), True, "bridge too old to serve / at all"),
        (_Payload(200, {}), True, "no capabilities key means no inbound media"),
        # Conservative: never restart a healthy bridge on every boot because a
        # probe blipped.
        (_Payload(500, None), False, "server error is not proof of staleness"),
        (OSError("connection refused"), False, "transport error is not proof"),
        (_Payload(200, "not-a-dict"), False, "unparseable shape is not proof"),
    ],
)
async def test_stale_bridge_detection(tmp_path, monkeypatch, resp, expected, why):
    adapter = _adapter(tmp_path, monkeypatch)
    _patch_root_probe(monkeypatch, resp)

    assert await adapter._bridge_lacks_inbound_media() is expected, why


def test_bridge_advertises_every_capability_the_adapter_demands():
    """The two lists are one contract, and disagreeing is a restart loop.

    ``_bridge_lacks_inbound_media`` restarts any bridge missing a required
    capability. Requiring a name the bridge never advertises therefore does not
    fail loudly — it restarts a healthy bridge on every boot, forever. So the
    Python side's demands must be a subset of what ``index.js`` claims.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "whatsapp-bridge" / "index.js").read_text(
        encoding="utf-8"
    )
    advertised = source.split("capabilities: [", 1)[1].split("]", 1)[0]
    for cap in WhatsAppAdapter._REQUIRED_BRIDGE_CAPABILITIES:
        assert f"'{cap}'" in advertised, f"bridge never advertises {cap!r}"
