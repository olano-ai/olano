"""Tests for ``POST /api/speech/speak``, the mobile app's spoken replies.

The app reads a reply aloud in the box's voice when text-to-speech is set up,
and in the phone's own voice when it is not, which is why the "not set up"
answers must be stable codes (409 ``tts_disabled`` / ``tts_not_configured``)
rather than a generic failure. The audio comes back in the response and
nothing is written into the workspace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
OGG_OPUS = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead" + b"\x00" * 30


@pytest.fixture
def mock_runtime(tmp_workspace):
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
        env={"OPENAI_API_KEY": "sk-agent-own"},
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
def synth(monkeypatch):
    """Stand in for the provider: record the call, write ``payload`` where asked."""
    seen: dict[str, object] = {"payload": MP3, "ext": ".mp3"}

    def fake(text: str, output_path: str, voice: str = "", audio_format: str = "") -> str:
        from olano.tools._credentials import get_credential

        seen.update(text=text, path=output_path, format=audio_format)
        seen["key"] = get_credential("OPENAI_API_KEY", "")
        out = Path(output_path).with_suffix(str(seen["ext"]))
        out.write_bytes(seen["payload"])  # type: ignore[arg-type]
        return str(out)

    monkeypatch.setattr("olano.speech.synthesize_speech", fake)
    return seen


def _speak(client, **body):
    return client.post("/api/speech/speak", json={"agent": "test_agent", **body})


def test_returns_the_audio_as_mp3_by_default(api_client, synth):
    resp = _speak(api_client, text="Hello there.")
    assert resp.status_code == 200
    assert resp.content == MP3
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["cache-control"] == "no-store"
    assert synth["text"] == "Hello there."
    assert synth["format"] == "mp3"


def test_runs_under_the_agents_own_credentials(api_client, synth):
    """A key set on the agent (config.env) is the one the provider is called with."""
    _speak(api_client, text="Hi")
    assert synth["key"] == "sk-agent-own"


def test_content_type_follows_what_the_provider_returned(api_client, synth):
    """A provider may ignore the requested format; the bytes decide."""
    synth.update(payload=OGG_OPUS, ext=".ogg")
    resp = _speak(api_client, text="Hi", audio_format="opus")
    assert resp.status_code == 200
    assert synth["format"] == "opus"
    assert resp.headers["content-type"] == "audio/ogg"


def test_unknown_format_falls_back_to_mp3(api_client, synth):
    _speak(api_client, text="Hi", audio_format="midi")
    assert synth["format"] == "mp3"


def test_nothing_is_left_behind(api_client, synth):
    _speak(api_client, text="Hi")
    assert not Path(str(synth["path"])).exists()
    assert not Path(str(synth["path"])).parent.exists()


def test_empty_text_is_refused(api_client, synth):
    resp = _speak(api_client, text="   ")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_text"


def test_unknown_agent(api_client, synth):
    resp = api_client.post("/api/speech/speak", json={"agent": "nobody", "text": "Hi"})
    assert resp.status_code in (403, 404)


@pytest.mark.parametrize(
    ("error", "code"),
    [("SpeechDisabledError", "tts_disabled"), ("SpeechNotConfiguredError", "tts_not_configured")],
)
def test_not_set_up_is_a_stable_code(api_client, monkeypatch, error, code):
    import olano.speech as speech

    exc = getattr(speech, error)

    def boom(*a, **k):
        raise exc("no voice here")

    monkeypatch.setattr("olano.speech.synthesize_speech", boom)
    resp = _speak(api_client, text="Hi")
    assert resp.status_code == 409
    assert resp.json()["detail"] == code


def test_provider_failure_is_a_gateway_error(api_client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("olano.speech.synthesize_speech", boom)
    resp = _speak(api_client, text="Hi")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "tts_provider_error"


@pytest.mark.parametrize("available", [True, False])
def test_status_exposes_tts_enabled(mock_runtime, tmp_workspace, monkeypatch, available):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    monkeypatch.setattr("olano.speech.tts_available", lambda: available)

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    client = TestClient(create_api(mock_runtime))
    caps = client.get("/api/status").json().get("capabilities", {})
    assert caps.get("tts_enabled") is available
