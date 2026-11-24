"""Tests for ``POST /api/transcribe`` — the dashboard composer's voice input.

This is the *fallback* half of the mic: browsers with the Web Speech API
dictate locally and never call it, so everything here is about the browsers
that can't (Firefox) posting a MediaRecorder blob once, on release.

The thing worth pinning is the **filename extension**. STT providers validate
the upload by extension, and ``olano/uploads.py`` maps ``audio/webm`` to
``.weba`` — a name OpenAI-compatible endpoints reject, and webm is exactly what
MediaRecorder produces in Chrome and Firefox. So the endpoint has its own
mapping, and these tests assert the suffix that reaches ``transcribe_file``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# MediaRecorder container headers, as the browser would send them.
EBML_WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60  # Chrome / Firefox
FTYP_MP4 = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 48  # Safari
OGG_OPUS = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead" + b"\x00" * 30
RIFF_WAV = b"RIFF\x24\x00\x00\x00WAVE" + b"\x00" * 50


@pytest.fixture
def mock_runtime(tmp_workspace):
    """A minimal mock RuntimeServer — the transcribe route never touches it."""
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.channels = []

    runner = MagicMock()
    runner.config = config
    runner.ainvoke = AsyncMock(return_value="hi")

    gw = MagicMock()
    gw._runners = {"test_agent": runner}
    gw._channels = []
    gw.get_runner.return_value = runner
    return gw


@pytest.fixture
def api_client(mock_runtime, tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    return TestClient(create_api(mock_runtime))


@pytest.fixture
def captured(monkeypatch):
    """Capture the path ``transcribe_file`` is handed, returning a transcript."""
    seen: dict[str, object] = {}

    def fake_transcribe(file_path: str, language: str = "", prompt: str = "") -> str:
        seen["path"] = file_path
        seen["language"] = language
        return "hello world"

    monkeypatch.setattr("olano.speech.transcribe_file", fake_transcribe)
    return seen


def _post(client, data: bytes, mime: str = "audio/webm", filename: str = "clip.webm", **kw):
    return client.post("/api/transcribe", files={"file": (filename, data, mime)}, data=kw or None)


# ── Extension mapping (the bug this endpoint exists to avoid) ──────────────


@pytest.mark.parametrize(
    ("payload", "mime", "filename", "expected"),
    [
        (EBML_WEBM, "audio/webm;codecs=opus", "clip.webm", ".webm"),
        (FTYP_MP4, "audio/mp4", "clip.mp4", ".m4a"),
        (OGG_OPUS, "audio/ogg;codecs=opus", "clip.ogg", ".ogg"),
        (RIFF_WAV, "audio/wav", "clip.wav", ".wav"),
        # Unsniffable bytes fall back to the declared mime...
        (b"\x00" * 64, "audio/mp4", "clip.bin", ".m4a"),
        # ...then to the filename, then to webm (MediaRecorder's default).
        (b"\x00" * 64, "application/octet-stream", "clip.m4a", ".m4a"),
        (b"\x00" * 64, "application/octet-stream", "blob", ".webm"),
    ],
)
def test_upload_extension_matches_container(
    api_client, captured, payload, mime, filename, expected
):
    resp = _post(api_client, payload, mime, filename)
    assert resp.status_code == 200
    assert str(captured["path"]).endswith(expected)


def test_webm_never_lands_as_weba(api_client, captured):
    """uploads.py would name this ``.weba``; providers reject that."""
    _post(api_client, EBML_WEBM, "audio/webm", "clip.webm")
    assert not str(captured["path"]).endswith(".weba")


# ── Happy path ─────────────────────────────────────────────────────────────


def test_returns_transcript(api_client, captured):
    resp = _post(api_client, EBML_WEBM)
    assert resp.status_code == 200
    assert resp.json() == {"text": "hello world"}


def test_language_hint_is_forwarded(api_client, captured):
    resp = _post(api_client, EBML_WEBM, language="es")
    assert resp.status_code == 200
    assert captured["language"] == "es"


def test_empty_transcript_is_not_an_error(api_client, monkeypatch):
    """Silence is a normal outcome; the UI says "didn't catch anything"."""
    monkeypatch.setattr("olano.speech.transcribe_file", lambda *a, **k: "")
    resp = _post(api_client, EBML_WEBM)
    assert resp.status_code == 200
    assert resp.json() == {"text": ""}


def test_raw_body_upload(api_client, captured):
    """Non-multipart POST works too, for programmatic callers."""
    resp = api_client.post(
        "/api/transcribe", content=EBML_WEBM, headers={"Content-Type": "audio/webm"}
    )
    assert resp.status_code == 200
    assert str(captured["path"]).endswith(".webm")


def test_temp_file_is_removed(api_client, captured):
    from pathlib import Path

    _post(api_client, EBML_WEBM)
    assert not Path(str(captured["path"])).exists()


# ── Errors (stable string codes — the dashboard localizes them) ────────────


def test_missing_file_field(api_client):
    resp = api_client.post("/api/transcribe", data={"language": "en"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_file"


def test_empty_upload(api_client):
    resp = _post(api_client, b"")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_file"


def test_oversize_upload(api_client):
    resp = _post(api_client, b"\x00" * (25 * 1024 * 1024 + 1))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "audio_too_large"


def test_stt_disabled(api_client, monkeypatch):
    from olano.speech import SpeechDisabledError

    def boom(*a, **k):
        raise SpeechDisabledError("speech is disabled")

    monkeypatch.setattr("olano.speech.transcribe_file", boom)
    resp = _post(api_client, EBML_WEBM)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stt_disabled"


def test_stt_not_configured(api_client, monkeypatch):
    from olano.speech import SpeechNotConfiguredError

    def boom(*a, **k):
        raise SpeechNotConfiguredError("no api key")

    monkeypatch.setattr("olano.speech.transcribe_file", boom)
    resp = _post(api_client, EBML_WEBM)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stt_not_configured"


def test_provider_failure_is_a_gateway_error(api_client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("olano.speech.transcribe_file", boom)
    resp = _post(api_client, EBML_WEBM)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "stt_provider_error"


# ── Capability flag ────────────────────────────────────────────────────────


# The capability cache is per-app and lives 5s, so each of these builds its
# own client rather than trying to invalidate a shared one.


def test_status_exposes_stt_enabled(mock_runtime, tmp_workspace, monkeypatch):
    """The flag gates whether a Web-Speech-less browser gets a mic at all."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    monkeypatch.setattr("olano.speech.stt_available", lambda: True)

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    client = TestClient(create_api(mock_runtime))
    caps = client.get("/api/status").json().get("capabilities", {})
    assert caps.get("stt_enabled") is True


def test_status_stt_disabled_when_unavailable(mock_runtime, tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    monkeypatch.setattr("olano.speech.stt_available", lambda: False)

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    client = TestClient(create_api(mock_runtime))
    caps = client.get("/api/status").json().get("capabilities", {})
    assert caps.get("stt_enabled") is False


def test_transcribe_route_is_registered(api_client):
    """A 405 (not 404) proves the path exists and only POST is allowed."""
    assert api_client.get("/api/transcribe").status_code in (405, 422)
