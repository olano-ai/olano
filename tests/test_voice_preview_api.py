"""Tests for ``POST /api/system/config/speech/preview``, Config -> Speech's play button.

The preview reads a short sample in the voice the form describes, saved or not,
so these tests drive the real resolver (``resolve_tts`` with the form's values)
down to the HTTP call and only stand in for the provider itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32
URL = "/api/system/config/speech/preview"


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    gw = MagicMock()
    gw._runners = {}
    gw._channels = []
    return TestClient(create_api(gw))


@pytest.fixture
def provider(monkeypatch):
    """Stand in for the provider's HTTP endpoint and record what it was sent."""
    seen: dict[str, object] = {"body": MP3, "calls": 0}

    def fake_post(url, headers=None, json=None, timeout=None, **_):
        seen.update(url=url, headers=headers, json=json)
        seen["calls"] = int(seen["calls"]) + 1  # type: ignore[call-overload]
        resp = MagicMock()
        resp.status_code = 200
        resp.content = seen["body"]
        resp.headers = {}
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr("httpx.post", fake_post)
    return seen


def _form(**over):
    base = {"provider": "openai", "model": "", "voice": "", "url": "", "api_key_name": ""}
    return {**base, **over}


def test_plays_the_unsaved_voice(api_client, provider, monkeypatch):
    """Nothing is saved (TTS is disabled on disk); the form's values are what speak."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-home")
    resp = api_client.post(
        URL, json={"tts": _form(voice="onyx", model="tts-1"), "text": "One. Two."}
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == MP3
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["cache-control"] == "no-store"
    sent = provider["json"]
    assert sent == {
        "model": "tts-1",
        "input": "One. Two.",
        "voice": "onyx",
        "response_format": "mp3",
    }
    assert provider["headers"] == {"Authorization": "Bearer sk-home"}


def test_preview_never_persists_the_form(api_client, provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-home")
    api_client.post(URL, json={"tts": _form(voice="onyx")})

    from olano.config.system_settings import load_speech_settings

    assert load_speech_settings().tts.provider == "disabled"


def test_blank_voice_uses_the_provider_default(api_client, provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-home")
    api_client.post(URL, json={"tts": _form()})
    assert provider["json"]["voice"] == "nova"  # type: ignore[index]


def test_text_is_capped_and_defaulted(api_client, provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-home")
    api_client.post(URL, json={"tts": _form(), "text": "x" * 5000})
    assert len(provider["json"]["input"]) == 300  # type: ignore[index]
    api_client.post(URL, json={"tts": _form(), "text": "   "})
    assert provider["json"]["input"]  # type: ignore[index]


def test_a_provider_without_formats_is_sent_none(api_client, provider, monkeypatch):
    """A custom server gets no response_format until opted in; its bytes decide the type."""
    provider["body"] = WAV
    resp = api_client.post(
        URL, json={"tts": _form(provider="custom", url="http://speech.local/v1/audio/speech")}
    )
    assert resp.status_code == 200, resp.text
    assert "response_format" not in provider["json"]  # type: ignore[operator]
    assert resp.headers["content-type"] == "audio/wav"


def test_disabled_is_a_stable_code(api_client, provider):
    resp = api_client.post(URL, json={"tts": _form(provider="disabled")})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "tts_disabled"
    assert provider["calls"] == 0


def test_missing_key_is_explained(api_client, provider, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = api_client.post(URL, json={"tts": _form()})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "tts_not_configured"
    assert "OPENAI_API_KEY" in detail["message"]
    assert provider["calls"] == 0


def test_provider_failure_is_a_gateway_error(api_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-home")

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("httpx.post", boom)
    resp = api_client.post(URL, json={"tts": _form()})
    assert resp.status_code == 502
    assert resp.json()["detail"] == {"code": "tts_provider_error", "message": "connection reset"}


def test_tts_must_be_an_object(api_client, provider):
    resp = api_client.post(URL, json={"tts": "openai"})
    assert resp.status_code == 400


def test_preview_is_admin_only():
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

    assert URL.startswith(_ADMIN_ONLY_PREFIXES)
