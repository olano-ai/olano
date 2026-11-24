"""Tests for the provider-independent speech layer (:mod:`olano.speech`).

The provider is selected in ``config.yaml → speech:`` (dashboard Config →
Speech) and **defaults to disabled** — an API key in the environment must NOT
silently enable transcription. These tests pin the resolution semantics
without any network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.system_settings import (
    SpeechSettings,
    load_speech_settings,
    set_speech_settings,
)
from olano.speech import (
    SpeechDisabledError,
    SpeechNotConfiguredError,
    resolve_stt,
    resolve_tts,
    stt_available,
    synthesize_speech,
    transcribe_file,
    tts_available,
)

_ENV_VARS = (
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "DASHSCOPE_API_KEY",
    "LIONCLAW_STT_PROVIDER",
    "LIONCLAW_TTS_PROVIDER",
    "LIONCLAW_STT_URL",
    "LIONCLAW_STT_API_KEY",
    "LIONCLAW_SPEECH_API_KEY",
)


@pytest.fixture()
def speech_home(monkeypatch, tmp_path) -> Path:
    """A fresh LIONCLAW_HOME with no config.yaml and a scrubbed environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


# ─────────────────────────── default: disabled ──────────────────────────────


def test_disabled_by_default(speech_home) -> None:
    settings = load_speech_settings()
    assert settings.stt.provider == "disabled"
    assert settings.tts.provider == "disabled"
    assert not stt_available()
    assert not tts_available()


def test_env_key_alone_does_not_enable(speech_home, monkeypatch) -> None:
    """An API key in the environment must not opt the deployment into STT."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert not stt_available()
    with pytest.raises(SpeechDisabledError):
        resolve_stt()


def test_transcribe_file_raises_disabled(speech_home, tmp_path) -> None:
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")
    with pytest.raises(SpeechDisabledError):
        transcribe_file(str(audio))


# ───────────────────────── config-driven providers ──────────────────────────


def test_openai_provider_via_config(speech_home, monkeypatch) -> None:
    set_speech_settings(stt={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    r = resolve_stt()
    assert "api.openai.com" in r.url
    assert r.model == "gpt-4o-mini-transcribe"
    assert r.api_key == "sk-test"
    assert stt_available()


def test_groq_provider_with_model_override(speech_home, monkeypatch) -> None:
    set_speech_settings(stt={"provider": "groq", "model": "whisper-large-v3"})
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    r = resolve_stt()
    assert "api.groq.com" in r.url
    assert r.model == "whisper-large-v3"
    assert r.api_key == "gsk-test"


def test_enabled_without_key_is_not_configured(speech_home) -> None:
    set_speech_settings(stt={"provider": "openai"})
    with pytest.raises(SpeechNotConfiguredError):
        resolve_stt()
    assert not stt_available()  # enabled but unusable


def test_custom_provider_requires_url(speech_home) -> None:
    set_speech_settings(stt={"provider": "custom"})
    with pytest.raises(SpeechNotConfiguredError):
        resolve_stt()


def test_custom_provider_keyless(speech_home) -> None:
    """Self-hosted servers on a private network commonly run without a key."""
    set_speech_settings(
        stt={"provider": "custom", "url": "http://10.0.0.5:8000/v1/audio/transcriptions"}
    )
    r = resolve_stt()
    assert r.url.startswith("http://10.0.0.5:8000")
    assert r.model == "whisper-1"  # lowest-common-denominator default
    assert r.api_key == ""
    assert stt_available()


def test_custom_provider_key_name(speech_home, monkeypatch) -> None:
    set_speech_settings(
        stt={
            "provider": "custom",
            "url": "http://localhost:8000/v1/audio/transcriptions",
            "api_key_name": "MY_SPEACHES_KEY",
            "model": "Systran/faster-whisper-small",
        }
    )
    monkeypatch.setenv("MY_SPEACHES_KEY", "local-secret")
    r = resolve_stt()
    assert r.api_key == "local-secret"
    assert r.model == "Systran/faster-whisper-small"


def test_tts_settings_independent_of_stt(speech_home, monkeypatch) -> None:
    set_speech_settings(tts={"provider": "openai", "voice": "shimmer"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert not stt_available()  # STT still off
    r = resolve_tts()
    assert r.model == "gpt-4o-mini-tts"
    assert r.voice == "shimmer"
    assert tts_available()


# ──────────────────────────── env override hatch ────────────────────────────


def test_env_provider_override(speech_home, monkeypatch) -> None:
    """LIONCLAW_STT_PROVIDER overrides config.yaml for headless deployments."""
    monkeypatch.setenv("LIONCLAW_STT_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    r = resolve_stt()  # config says disabled; env says groq
    assert "api.groq.com" in r.url


def test_env_override_can_disable(speech_home, monkeypatch) -> None:
    set_speech_settings(stt={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LIONCLAW_STT_PROVIDER", "disabled")
    assert not stt_available()


# ───────────────────────────── config hygiene ───────────────────────────────


def test_unknown_provider_coerces_to_disabled(speech_home) -> None:
    (Path(speech_home) / "config.yaml").write_text(
        "speech:\n  stt:\n    provider: bananas\n", encoding="utf-8"
    )
    settings = load_speech_settings()
    assert settings.stt.provider == "disabled"


def test_set_speech_settings_preserves_other_keys(speech_home) -> None:
    (Path(speech_home) / "config.yaml").write_text(
        "default_agent: merli\ncognition:\n  intensity: relaxed\n", encoding="utf-8"
    )
    set_speech_settings(stt={"provider": "openai"}, tts={"provider": "groq"})
    import yaml

    raw = yaml.safe_load((Path(speech_home) / "config.yaml").read_text(encoding="utf-8"))
    assert raw["default_agent"] == "merli"
    assert raw["cognition"]["intensity"] == "relaxed"
    assert raw["speech"]["stt"]["provider"] == "openai"
    assert raw["speech"]["tts"]["provider"] == "groq"


def test_settings_model_defaults() -> None:
    s = SpeechSettings()
    assert s.stt.provider == "disabled"
    assert s.tts.provider == "disabled"
    assert s.stt.model == "" and s.stt.url == "" and s.stt.api_key_name == ""


# ─────────────────────────── qwen (DashScope) TTS ───────────────────────────
# Qwen-TTS is TTS-only and does NOT speak the OpenAI audio shape: the request
# nests input.{text,voice} and the response returns an audio URL to fetch.


def test_qwen_tts_provider_defaults(speech_home, monkeypatch) -> None:
    set_speech_settings(tts={"provider": "qwen"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    r = resolve_tts()
    assert r.provider == "qwen"
    assert "dashscope-intl.aliyuncs.com" in r.url
    assert r.url.endswith("/multimodal-generation/generation")
    assert r.model == "qwen3-tts-flash"
    assert r.voice == "Cherry"
    assert r.api_key == "sk-dash"
    assert tts_available()
    assert not stt_available()  # qwen is TTS-only; STT stays disabled


def test_qwen_tts_without_key_is_not_configured(speech_home) -> None:
    set_speech_settings(tts={"provider": "qwen"})
    with pytest.raises(SpeechNotConfiguredError):
        resolve_tts()
    assert not tts_available()


def test_qwen_tts_model_and_voice_override(speech_home, monkeypatch) -> None:
    set_speech_settings(tts={"provider": "qwen", "model": "qwen-tts", "voice": "Dylan"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    r = resolve_tts()
    assert r.model == "qwen-tts"
    assert r.voice == "Dylan"


def test_qwen_tts_endpoint_override_for_beijing(speech_home, monkeypatch) -> None:
    """Built-in providers honor speech.tts.url — e.g. the Beijing DashScope host."""
    beijing = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    set_speech_settings(tts={"provider": "qwen", "url": beijing})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    r = resolve_tts()
    assert r.url == beijing
    assert "dashscope-intl" not in r.url


def test_qwen_for_stt_is_rejected(speech_home, monkeypatch) -> None:
    """qwen is a valid provider value but TTS-only; STT must fail cleanly."""
    set_speech_settings(stt={"provider": "qwen"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    with pytest.raises(SpeechNotConfiguredError, match="does not support transcription"):
        resolve_stt()
    assert not stt_available()


class _FakeResp:
    """Minimal httpx.Response stand-in for POST (json) and GET (content)."""

    def __init__(
        self,
        *,
        json_data: dict | None = None,
        content: bytes = b"",
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json = json_data
        self.content = content
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        return None

    def json(self) -> dict:
        assert self._json is not None
        return self._json


def test_synthesize_speech_qwen_uses_dashscope_shape(speech_home, monkeypatch, tmp_path) -> None:
    set_speech_settings(tts={"provider": "qwen"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post_url"] = url
        calls["post_headers"] = headers or {}
        calls["post_json"] = json
        return _FakeResp(json_data={"output": {"audio": {"url": "https://oss.example/o.wav"}}})

    def fake_get(url, timeout=None):
        calls["get_url"] = url
        return _FakeResp(content=b"WAVBYTES")

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    out = tmp_path / "out.wav"
    result = synthesize_speech("Hello world", str(out), voice="Ethan")

    assert result == str(out)
    assert out.read_bytes() == b"WAVBYTES"
    assert "dashscope-intl.aliyuncs.com" in calls["post_url"]
    assert calls["post_url"].endswith("/multimodal-generation/generation")
    # Nested input.{text,voice} — the DashScope shape, not OpenAI's flat payload.
    assert calls["post_json"] == {
        "model": "qwen3-tts-flash",
        "input": {"text": "Hello world", "voice": "Ethan"},
    }
    assert calls["post_headers"]["Authorization"] == "Bearer sk-dash"
    # Second hop downloads the returned audio URL.
    assert calls["get_url"] == "https://oss.example/o.wav"


def test_synthesize_speech_qwen_missing_url_surfaces_error(
    speech_home, monkeypatch, tmp_path
) -> None:
    set_speech_settings(tts={"provider": "qwen"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")

    def fake_post(url, headers=None, json=None, timeout=None):
        # DashScope error envelope (no output.audio.url).
        return _FakeResp(json_data={"code": "InvalidApiKey", "message": "bad key"})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="bad key"):
        synthesize_speech("Hi", str(tmp_path / "x.wav"))


def test_synthesize_speech_openai_keeps_flat_shape(speech_home, monkeypatch, tmp_path) -> None:
    """Regression: the OpenAI path still POSTs a flat payload and writes bytes."""
    set_speech_settings(tts={"provider": "openai", "voice": "shimmer"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return _FakeResp(content=b"MP3BYTES")

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = tmp_path / "o.mp3"
    synthesize_speech("Hello", str(out))
    assert out.read_bytes() == b"MP3BYTES"
    assert "api.openai.com" in calls["url"]
    # OpenAI: input is the raw string, voice is top-level.
    assert calls["json"]["input"] == "Hello"
    assert calls["json"]["voice"] == "shimmer"


def test_synthesize_speech_records_a_service_call(speech_home, monkeypatch, tmp_path) -> None:
    """TTS lands in the analytics service ledger, tagged with who paid.

    Speech spends money — olano credits on the managed gateway path, the
    operator's own key otherwise — without producing a token, so the LLM usage
    ledger never sees it and Insights would under-report a credits account's
    real burn without this row.
    """
    from olano.analytics import service_ledger

    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: _FakeResp(content=b"ID3\x04\x00\x00" + b"\x00" * 32)
    )
    synthesize_speech("Hello", str(tmp_path / "o.mp3"))

    events = service_ledger.read_service_events()
    assert [(e.service, e.provider, e.billing_source, e.credits) for e in events] == [
        ("tts", "openai", "customer", None)
    ]


# ───────────────────── dashboard dropdown option catalog ─────────────────────
# The Config → Speech model/voice dropdowns are populated from speech_catalog();
# custom (and qwen-under-STT) have no options so the UI keeps a free-text input.


def test_speech_catalog_shape_and_scope(speech_home) -> None:
    from olano.speech import speech_catalog

    cat = speech_catalog()
    # TTS providers carry both model and voice option lists.
    assert set(cat["tts"]) == {"olano", "openai", "groq", "qwen"}
    assert cat["tts"]["qwen"]["models"][0] == "qwen3-tts-flash"
    assert cat["tts"]["qwen"]["voices"][0] == "Cherry"
    assert "nova" in cat["tts"]["openai"]["voices"]
    assert "Fritz-PlayAI" in cat["tts"]["groq"]["voices"]
    # Olano exposes exactly its gateway group names — never a provider model.
    assert cat["tts"]["olano"]["models"] == ["olano-tts"]
    assert "nova" in cat["tts"]["olano"]["voices"]
    assert cat["stt"]["olano"]["models"] == ["olano-stt"]
    # STT is transcription-only: models, no voices; qwen and custom absent.
    assert set(cat["stt"]) == {"olano", "openai", "groq"}
    assert "voices" not in cat["stt"]["openai"]
    assert "qwen" not in cat["stt"]
    assert "custom" not in cat["tts"] and "custom" not in cat["stt"]


def test_speech_catalog_defaults_are_resolver_defaults(speech_home, monkeypatch) -> None:
    """The first catalogued model/voice is what the resolver falls back to."""
    from olano.speech import speech_catalog

    cat = speech_catalog()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    set_speech_settings(tts={"provider": "qwen"})
    r = resolve_tts()
    assert r.model == cat["tts"]["qwen"]["models"][0]
    assert r.voice == cat["tts"]["qwen"]["voices"][0]


# ───────────────── audio container: request, override, sniffing ─────────────
#
# The container is not cosmetic: the chat platforms render a *voice bubble*
# only for Ogg/Opus and fall back to an audio-file card for everything else,
# so a wrong (or wrongly-labelled) container silently downgrades every voice
# reply. These pin both halves — what we ask the provider for, and what we
# name the file once the bytes come back.

_OGG_OPUS = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 8
_WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 8
_MP3 = b"ID3\x04\x00\x00" + b"\x00" * 32


def _capture_post(monkeypatch, content: bytes) -> dict:
    """Stub httpx.post and return the dict the request lands in."""
    import httpx

    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return _FakeResp(content=content)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_openai_tts_requests_opus_by_default(speech_home, monkeypatch, tmp_path) -> None:
    """Opus is the only container a platform can turn into a voice bubble."""
    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = _capture_post(monkeypatch, _OGG_OPUS)

    assert resolve_tts().audio_format == "opus"
    out = synthesize_speech("Hello", str(tmp_path / "reply.ogg"))
    assert calls["json"]["response_format"] == "opus"
    assert out.endswith(".ogg")


def test_groq_tts_requests_wav_not_opus(speech_home, monkeypatch, tmp_path) -> None:
    """PlayAI documents WAV only — asking it for Opus would 400."""
    set_speech_settings(tts={"provider": "groq"})
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    calls = _capture_post(monkeypatch, _WAV)

    assert resolve_tts().audio_format == "wav"
    out = synthesize_speech("Hello", str(tmp_path / "reply.ogg"))
    assert calls["json"]["response_format"] == "wav"
    # Sniffed as WAV despite the .ogg path we asked for: the extension must
    # never claim a bubble the bytes cannot deliver.
    assert out.endswith(".wav")


def test_custom_provider_gets_no_format_until_asked(speech_home, monkeypatch, tmp_path) -> None:
    """An unknown self-hosted server must not start receiving a new parameter."""
    set_speech_settings(tts={"provider": "custom", "url": "http://localhost:8000/v1/audio/speech"})
    calls = _capture_post(monkeypatch, _MP3)

    assert resolve_tts().audio_format == ""
    synthesize_speech("Hello", str(tmp_path / "o.bin"))
    assert "response_format" not in calls["json"]


def test_configured_audio_format_overrides_the_provider_default(
    speech_home, monkeypatch, tmp_path
) -> None:
    set_speech_settings(
        tts={"provider": "custom", "url": "http://x/v1/audio/speech", "audio_format": "opus"}
    )
    calls = _capture_post(monkeypatch, _OGG_OPUS)

    assert resolve_tts().audio_format == "opus"
    synthesize_speech("Hello", str(tmp_path / "o.ogg"))
    assert calls["json"]["response_format"] == "opus"


def test_unknown_audio_format_falls_back_to_auto(speech_home, monkeypatch) -> None:
    """Fail-open, like `provider`: a typo must not take voice replies down."""
    set_speech_settings(tts={"provider": "openai", "audio_format": "flack"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert load_speech_settings().tts.audio_format == ""
    assert resolve_tts().audio_format == "opus"


def test_output_extension_is_corrected_from_the_bytes(speech_home, monkeypatch, tmp_path) -> None:
    """A provider that ignores response_format still gets a truthful filename."""
    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _capture_post(monkeypatch, _MP3)

    out = synthesize_speech("Hello", str(tmp_path / "reply.ogg"))
    assert out.endswith(".mp3")
    assert Path(out).read_bytes() == _MP3
    assert not (tmp_path / "reply.ogg").exists()


def test_unrecognized_bytes_keep_the_requested_path(speech_home, monkeypatch, tmp_path) -> None:
    """Rename only on a positive identification — never guess."""
    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _capture_post(monkeypatch, b"not audio at all")

    out = tmp_path / "reply.ogg"
    assert synthesize_speech("Hello", str(out)) == str(out)


def test_output_path_never_reintroduces_a_format(speech_home, monkeypatch, tmp_path) -> None:
    """Naming the file .mp3 must not send `response_format` to a custom server.

    The tool's default ``output_path=/tmp/lionclaw_speech.mp3`` would otherwise
    start pushing a parameter at self-hosted servers that never implemented it.
    """
    set_speech_settings(tts={"provider": "custom", "url": "http://x/v1/audio/speech"})
    calls = _capture_post(monkeypatch, _MP3)

    out = synthesize_speech("Hello", str(tmp_path / "clip.mp3"))
    assert "response_format" not in calls["json"]
    assert out.endswith(".mp3")  # named from the bytes that came back


def test_explicit_argument_overrides_the_resolved_format(
    speech_home, monkeypatch, tmp_path
) -> None:
    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = _capture_post(monkeypatch, _WAV)

    synthesize_speech("Hello", str(tmp_path / "clip.ogg"), audio_format="wav")
    assert calls["json"]["response_format"] == "wav"


def test_qwen_never_gets_a_response_format(speech_home, monkeypatch, tmp_path) -> None:
    """DashScope has no such parameter; the extension comes from the bytes."""
    import httpx

    set_speech_settings(tts={"provider": "qwen"})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["json"] = json
        return _FakeResp(json_data={"output": {"audio": {"url": "https://oss.example/o.wav"}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _FakeResp(content=_WAV))

    assert resolve_tts().audio_format == ""
    out = synthesize_speech("Hello", str(tmp_path / "o.ogg"))
    assert "response_format" not in calls["json"]
    assert out.endswith(".wav")


def test_tts_audio_format_is_empty_when_disabled(speech_home) -> None:
    from olano.speech import audio_extension_for_format, tts_audio_format

    assert tts_audio_format() == ""
    # Unknown/blank container ⇒ the near-universal mp3 default.
    assert audio_extension_for_format("") == ".mp3"
    assert audio_extension_for_format("opus") == ".ogg"


def test_rejected_response_format_retries_without_it(speech_home, monkeypatch, tmp_path) -> None:
    """A server that does not implement the parameter must not cost the reply."""
    import httpx

    set_speech_settings(
        tts={"provider": "custom", "url": "http://x/v1/audio/speech", "audio_format": "opus"}
    )
    payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        payloads.append(dict(json))
        if "response_format" in json:
            return _FakeResp(status_code=400, text='{"error":"unsupported response_format: opus"}')
        return _FakeResp(content=_MP3)

    monkeypatch.setattr(httpx, "post", fake_post)

    out = synthesize_speech("Hello", str(tmp_path / "reply.ogg"))
    assert [p.get("response_format") for p in payloads] == ["opus", None]
    # Degraded honestly: an mp3 named .mp3, not an .ogg bubble that won't play.
    assert out.endswith(".mp3")


def test_unrelated_400_is_not_retried(speech_home, monkeypatch, tmp_path) -> None:
    """Only a format complaint earns the second call."""
    import httpx

    set_speech_settings(tts={"provider": "openai"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))
        return _FakeResp(status_code=400, text='{"error":"model not found"}')

    monkeypatch.setattr(httpx, "post", fake_post)

    synthesize_speech("Hello", str(tmp_path / "reply.ogg"))
    assert len(calls) == 1


def test_settings_vocabulary_matches_the_audio_tables() -> None:
    """The config schema and the audio layer must not drift apart.

    ``TTS_AUDIO_FORMATS`` lives in ``config/system_settings.py`` on purpose —
    the validator runs on every settings load, including from background
    threads, so it must not import a module that drags in the tool registry.
    """
    from olano.config.system_settings import TTS_AUDIO_FORMATS
    from olano.speech import _FORMAT_EXTENSIONS

    assert set(TTS_AUDIO_FORMATS) <= set(_FORMAT_EXTENSIONS)
    # `ogg` is what sniffing reports for a non-Opus Ogg stream; it is not a
    # container an operator can select.
    assert set(_FORMAT_EXTENSIONS) - set(TTS_AUDIO_FORMATS) == {"ogg"}


# ── gateway charge extraction (managed speech bills per call via headers) ────


def test_speech_credits_prices_managed_calls_from_headers(monkeypatch) -> None:
    from types import SimpleNamespace

    from olano import speech

    charged = SimpleNamespace(headers={"x-olano-credits-charged": "0.8"})
    # Olano's own charge header is trusted on any provider's response.
    assert speech._speech_credits(charged, "olano") == 0.8
    assert speech._speech_credits(charged, "openai") == 0.8

    monkeypatch.setattr("olano.catalog.credits_registry.credits_per_usd", lambda: 1000.0)
    managed = SimpleNamespace(headers={"x-litellm-response-cost": "0.002"})
    assert speech._speech_credits(managed, "olano") == 2.0
    # BYOK cost telemetry must never fabricate an olano charge.
    assert speech._speech_credits(managed, "openai") is None
    assert speech._speech_credits(SimpleNamespace(headers={}), "olano") is None


def test_record_speech_service_carries_the_charge(speech_home) -> None:
    from olano import speech
    from olano.analytics.service_ledger import read_service_events

    speech._record_speech_service("tts", "olano", 0.8)
    speech._record_speech_service("stt", "openai", None)

    events = {e.service: e for e in read_service_events()}
    assert events["tts"].credits == 0.8 and events["tts"].billing_source == "olano"
    assert events["stt"].credits is None and events["stt"].billing_source != "olano"
