"""Provider-independent speech services (STT + TTS).

One translation layer between olano and whichever speech provider the
operator picks, so no caller ever hardcodes a vendor:

- the messaging platforms (voice-note transcription in
  ``platforms/inbound.py``, voice replies),
- the ``audio_to_text`` / ``text_to_speech`` agent tools.

The provider is selected in ``~/.olano/config.yaml → speech:`` (editable in
the dashboard **Config → Speech** section) and **defaults to disabled** — no
audio leaves the box until an operator opts in. Supported providers:

======== ==================================================================
disabled default — callers get :class:`SpeechDisabledError`
openai   api.openai.com (STT ``gpt-4o-mini-transcribe``, TTS ``gpt-4o-mini-tts``)
groq     api.groq.com  (STT ``whisper-large-v3-turbo`` ~9× cheaper; TTS ``playai-tts``)
qwen     **TTS only** — Alibaba DashScope Qwen-TTS (``qwen3-tts-flash`` by
         default, ``DASHSCOPE_API_KEY``, voice ``Cherry``). #1 on the
         Artificial Analysis TTS leaderboard; 16 languages.
custom   any OpenAI-compatible server — self-hosted Speaches / faster-whisper
         / whisper.cpp / LocalAI on your own server (``speech.stt.url``)
======== ==================================================================

Every provider except ``qwen`` speaks the OpenAI audio API shape
(``/v1/audio/transcriptions`` multipart, ``/v1/audio/speech`` JSON), which every
mainstream engine exposes natively or via a shim — that's the portability seam.
DashScope's Qwen-TTS is the exception: it nests ``input.{text,voice}`` and
returns an audio *URL* rather than raw bytes, so :func:`synthesize_speech`
handles that shape internally (there is no Qwen STT here — selecting ``qwen``
for STT raises :class:`SpeechNotConfiguredError`). API keys resolve per-agent
via :func:`olano.tools._credentials.get_credential` (config.env overlay →
vault → environment).

Ops escape hatch: ``LIONCLAW_STT_PROVIDER`` / ``LIONCLAW_TTS_PROVIDER``
override the config.yaml provider for headless deployments (same values).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # httpx stays a call-time import at runtime
    import httpx

from olano.config.system_settings import (
    SPEECH_PROVIDERS,
    SpeechServiceSettings,
    load_speech_settings,
)
from olano.config.system_settings import (
    TTS_AUDIO_FORMATS as TTS_AUDIO_FORMATS,  # re-exported: see the note below
)
from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)


class SpeechDisabledError(RuntimeError):
    """The service is switched off in config.yaml (the default)."""


class SpeechNotConfiguredError(RuntimeError):
    """The service is enabled but unusable (missing URL or API key)."""


@dataclass(frozen=True)
class ResolvedSpeech:
    """A ready-to-call speech endpoint."""

    provider: str
    url: str
    model: str
    api_key: str
    voice: str = ""
    #: TTS only — the container to ask the provider for (``response_format``).
    #: Empty means "send no format and take whatever the provider returns";
    #: :func:`synthesize_speech` sniffs the bytes either way, so the file on
    #: disk always carries a truthful extension.
    audio_format: str = ""


# ── Audio containers ────────────────────────────────────────────────────────
#
# Which container comes back matters far beyond file naming: a voice *bubble*
# (Telegram's round player, a WhatsApp PTT) is only possible for Ogg/Opus.
# Everything else is delivered as a plain audio file — playable, but not a
# voice note. Auto-TTS therefore asks for Opus wherever the provider supports
# it, and the platform adapters branch on the extension.
_FORMAT_EXTENSIONS: dict[str, str] = {
    "opus": ".ogg",
    "ogg": ".ogg",
    "mp3": ".mp3",
    "wav": ".wav",
    "flac": ".flac",
    "aac": ".aac",
    "m4a": ".m4a",
    "pcm": ".pcm",
}
#: ``TTS_AUDIO_FORMATS`` (the selectable values for ``speech.tts.audio_format``)
#: is defined in ``config/system_settings.py`` and re-exported here, so audio
#: callers have one import for the whole vocabulary. ``_FORMAT_EXTENSIONS`` is
#: the superset: ``ogg`` is a *sniff* result, never a setting.
#: Containers a messaging platform can render as a native voice bubble.
VOICE_NOTE_FORMATS = frozenset({"opus", "ogg"})
#: Extensions matching :data:`VOICE_NOTE_FORMATS`, for adapters that only see
#: a path (``platforms/telegram.py``, ``platforms/whatsapp.py``).
VOICE_NOTE_EXTENSIONS = frozenset({".ogg", ".opus"})

# provider → the ``response_format`` used when ``speech.tts.audio_format`` is
# blank. Opus where the provider documents it; conservative elsewhere:
#   * groq  — PlayAI TTS documents WAV, so asking for Opus would 400.
#   * qwen  — DashScope has no response_format at all (it returns a URL).
#   * custom— an unknown self-hosted server might reject a format it never
#             implemented, and a working setup must not break on upgrade.
#             Self-hosters opt into voice bubbles by setting audio_format.
_TTS_AUTO_FORMAT: dict[str, str] = {
    "olano": "opus",
    "openai": "opus",
    "groq": "wav",
    "qwen": "",
    "custom": "",
}


# provider → (endpoint, default model, default credential name)
# `olano` endpoints are resolved dynamically in _resolve() (the gateway URL is
# env/vault-configurable), hence the empty URL here; its models are the
# gateway's GROUP names, never provider model ids — the same product boundary
# as the olano chat tiers.
_STT_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "olano": ("", "olano-stt", "OLANO_GATEWAY_KEY"),
    "openai": (
        "https://api.openai.com/v1/audio/transcriptions",
        "gpt-4o-mini-transcribe",
        "OPENAI_API_KEY",
    ),
    "groq": (
        "https://api.groq.com/openai/v1/audio/transcriptions",
        "whisper-large-v3-turbo",
        "GROQ_API_KEY",
    ),
}
# provider → (endpoint, default model, default credential name, default voice)
_TTS_DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    "olano": ("", "olano-tts", "OLANO_GATEWAY_KEY", "nova"),
    "openai": (
        "https://api.openai.com/v1/audio/speech",
        "gpt-4o-mini-tts",
        "OPENAI_API_KEY",
        "nova",
    ),
    "groq": (
        "https://api.groq.com/openai/v1/audio/speech",
        "playai-tts",
        "GROQ_API_KEY",
        "Fritz-PlayAI",
    ),
    # Alibaba DashScope Qwen-TTS. NOT OpenAI-shaped — synthesize_speech() routes
    # this provider through _synthesize_qwen(). International endpoint by default;
    # mainland-China accounts override speech.tts.url with the Beijing host
    # (https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation).
    "qwen": (
        "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "qwen3-tts-flash",
        "DASHSCOPE_API_KEY",
        "Cherry",
    ),
}
# Qwen-TTS bilingual/multilingual voices (qwen3-tts-flash). The default is
# ``Cherry``; operators pick any of these via ``speech.tts.voice``.
QWEN_TTS_VOICES = (
    "Cherry",
    "Ethan",
    "Nofish",
    "Jennifer",
    "Ryan",
    "Katerina",
    "Elias",
    "Jada",
    "Dylan",
    "Sunny",
    "Li",
    "Marcus",
    "Roy",
    "Peter",
    "Rocky",
    "Kiki",
    "Eric",
)
# Fallback model names for `custom` endpoints: the lowest-common-denominator
# names that self-hosted OpenAI-compatible servers accept out of the box.
_CUSTOM_STT_MODEL = "whisper-1"
_CUSTOM_TTS_MODEL = "tts-1"
_CUSTOM_KEY_NAME = "LIONCLAW_SPEECH_API_KEY"
# Synthesis input cap. OpenAI's `/v1/audio/speech` hard-limits at 4096 chars and
# the `text_to_speech` tool documents the same ceiling, so we apply it uniformly
# (Qwen-TTS itself accepts longer, one-pass long-form input).
_TTS_MAX_CHARS = 4096


# ── Dashboard option catalogs ────────────────────────────────────────────────
# Curated model / voice options per built-in provider, surfaced to the dashboard
# Config → Speech dropdowns so operators pick a value instead of typing a raw id.
# The FIRST model in each tuple is the provider's default and must match the
# resolver defaults (_STT_DEFAULTS / _TTS_DEFAULTS). `custom` is intentionally
# absent — a self-hosted endpoint's model/voice names are unknowable, so the UI
# keeps free-text inputs for it. Selecting a provider with no catalog entry (or
# `qwen` under STT) likewise falls back to free text.

STT_MODEL_CATALOG: dict[str, tuple[str, ...]] = {
    # The one Olano STT group; what backs it is the gateway's routing decision.
    "olano": ("olano-stt",),
    "openai": ("gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"),
    "groq": ("whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en"),
}

TTS_MODEL_CATALOG: dict[str, tuple[str, ...]] = {
    "olano": ("olano-tts",),
    "openai": ("gpt-4o-mini-tts", "tts-1", "tts-1-hd"),
    "groq": ("playai-tts", "playai-tts-arabic"),
    # Non-realtime HTTP models only (the file-generating path). The realtime
    # `qwen-audio-3.0-tts-*` tiers are WebSocket-streaming and excluded here.
    "qwen": ("qwen3-tts-flash", "qwen-tts-latest", "qwen-tts"),
}

# OpenAI voices available on the default `gpt-4o-mini-tts` (superset of tts-1's).
_OPENAI_TTS_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
# Groq PlayAI ``playai-tts`` (English) voices.
_GROQ_TTS_VOICES = (
    "Fritz-PlayAI",
    "Arista-PlayAI",
    "Atlas-PlayAI",
    "Basil-PlayAI",
    "Briggs-PlayAI",
    "Calum-PlayAI",
    "Celeste-PlayAI",
    "Cheyenne-PlayAI",
    "Chip-PlayAI",
    "Cillian-PlayAI",
    "Deedee-PlayAI",
    "Gail-PlayAI",
    "Indigo-PlayAI",
    "Mamaw-PlayAI",
    "Mason-PlayAI",
    "Mikail-PlayAI",
    "Mitch-PlayAI",
    "Quinn-PlayAI",
    "Thunder-PlayAI",
)
# The managed TTS group accepts the six core voices only; the extended
# OpenAI voice list is not guaranteed to be served and would 400.
_OLANO_TTS_VOICES = ("alloy", "echo", "fable", "nova", "onyx", "shimmer")
TTS_VOICE_CATALOG: dict[str, tuple[str, ...]] = {
    "olano": _OLANO_TTS_VOICES,
    "openai": _OPENAI_TTS_VOICES,
    "groq": _GROQ_TTS_VOICES,
    "qwen": QWEN_TTS_VOICES,
}


def speech_catalog() -> dict[str, dict[str, dict[str, list[str]]]]:
    """Per-provider model/voice option lists for the dashboard dropdowns.

    Shape::

        {
            "stt": {provider: {"models": [...]}},
            "tts": {provider: {"models": [...], "voices": [...]}},
        }

    A provider absent from a section (``custom``, or ``qwen`` under STT) has no
    curated options; the UI renders a free-text input for it instead.
    """
    stt = {provider: {"models": list(models)} for provider, models in STT_MODEL_CATALOG.items()}
    tts = {
        provider: {
            "models": list(TTS_MODEL_CATALOG.get(provider, ())),
            "voices": list(TTS_VOICE_CATALOG.get(provider, ())),
        }
        for provider in sorted(set(TTS_MODEL_CATALOG) | set(TTS_VOICE_CATALOG))
    }
    return {"stt": stt, "tts": tts}


def _effective_provider(kind: str, configured: str) -> str:
    """Apply the LIONCLAW_STT_PROVIDER / LIONCLAW_TTS_PROVIDER env override."""
    override = get_credential(f"LIONCLAW_{kind.upper()}_PROVIDER", "").strip().lower()
    if override in SPEECH_PROVIDERS:
        return override
    if override:
        logger.warning("Ignoring unknown %s provider override: %r", kind, override)
    return configured


def _tts_format(kind: str, settings: SpeechServiceSettings) -> str:
    """The ``response_format`` for this TTS config (``''`` for STT / no-format).

    An explicit ``speech.tts.audio_format`` always wins; blank falls back to
    the per-provider auto pick.
    """
    if kind != "tts":
        return ""
    configured = (settings.audio_format or "").strip().lower()
    if configured in _FORMAT_EXTENSIONS:
        return configured
    provider = _effective_provider(kind, settings.provider)
    return _TTS_AUTO_FORMAT.get(provider, "")


def _resolve(
    kind: str,
    settings: SpeechServiceSettings,
    defaults_stt: dict[str, tuple[str, str, str]] | None,
    defaults_tts: dict[str, tuple[str, str, str, str]] | None,
) -> ResolvedSpeech:
    provider = _effective_provider(kind, settings.provider)
    if provider == "disabled":
        label = "Speech-to-text" if kind == "stt" else "Text-to-speech"
        raise SpeechDisabledError(
            f"{label} is disabled (the default). Enable it in the dashboard "
            f"Config → Speech section or config.yaml → speech.{kind}.provider."
        )
    if provider == "custom":
        url = settings.url.strip() or get_credential(f"LIONCLAW_{kind.upper()}_URL", "")
        if not url:
            raise SpeechNotConfiguredError(
                f"speech.{kind}.provider is 'custom' but no URL is set "
                f"(config.yaml speech.{kind}.url or LIONCLAW_{kind.upper()}_URL)."
            )
        key_name = settings.api_key_name.strip() or _CUSTOM_KEY_NAME
        api_key = get_credential(key_name, "") or get_credential(
            f"LIONCLAW_{kind.upper()}_API_KEY", ""
        )
        default_model = _CUSTOM_STT_MODEL if kind == "stt" else _CUSTOM_TTS_MODEL
        model = settings.model.strip() or default_model
        voice = settings.voice.strip()
        # Custom/self-hosted servers commonly run keyless on a private network.
        return ResolvedSpeech(provider, url, model, api_key, voice, _tts_format(kind, settings))

    if kind == "stt":
        assert defaults_stt is not None
        if provider not in defaults_stt:
            # SPEECH_PROVIDERS is shared across both directions; a TTS-only
            # backend (e.g. qwen) selected for STT lands here — fail with an
            # actionable message instead of a KeyError.
            raise SpeechNotConfiguredError(
                f"speech.stt.provider is '{provider}', which does not support "
                f"transcription. Choose one of: {', '.join(sorted(defaults_stt))}."
            )
        url, default_model, default_key = defaults_stt[provider]
        default_voice = ""
    else:
        assert defaults_tts is not None
        if provider not in defaults_tts:
            raise SpeechNotConfiguredError(
                f"speech.tts.provider is '{provider}', which does not support "
                f"synthesis. Choose one of: {', '.join(sorted(defaults_tts))}."
            )
        url, default_model, default_key, default_voice = defaults_tts[provider]
    if provider == "olano":
        # The Olano speech service rides the model gateway: endpoint from the
        # gateway URL (env/vault), credential = the per-deployment virtual key
        # (self-enrolling over the deployment token — the same resolution the
        # olano chat tiers use). Billed in olano credits, no provider key on
        # this box.
        from olano.deployment import enforcement
        from olano.deployment.gateway_key import ensure_gateway_key, openai_base_url

        # Per-service entitlement: a plan may withhold olano tts/stt. Treat a
        # withheld service as not-configured so the box falls back to another
        # provider (or disabled) instead of leading with a rung that will 402.
        if not enforcement.service_entitled(kind):
            raise SpeechNotConfiguredError(
                f"speech.{kind}.provider is 'olano' but this deployment's plan "
                f"does not include the Olano {kind} service. Choose another "
                "speech provider, or upgrade at cloud.olano.ai."
            )
        api_key = ensure_gateway_key() or ""
        if not api_key:
            raise SpeechNotConfiguredError(
                f"speech.{kind}.provider is 'olano' but this deployment has no "
                "gateway credential (not connected to cloud.olano.ai). Connect "
                "the deployment, or choose another speech provider."
            )
        path = "audio/transcriptions" if kind == "stt" else "audio/speech"
        url = settings.url.strip() or f"{openai_base_url()}/{path}"
        model = settings.model.strip() or default_model
        voice = settings.voice.strip() or default_voice
        return ResolvedSpeech(provider, url, model, api_key, voice, _tts_format(kind, settings))

    key_name = settings.api_key_name.strip() or default_key
    api_key = get_credential(key_name, "")
    if not api_key:
        raise SpeechNotConfiguredError(
            f"speech.{kind}.provider is '{provider}' but the credential "
            f"'{key_name}' is not set (config.env / vault / environment)."
        )
    model = settings.model.strip() or default_model
    voice = settings.voice.strip() or default_voice
    # Built-in providers accept an optional endpoint override (e.g. point `qwen`
    # at the Beijing `dashscope.aliyuncs.com` host, or `openai` at an Azure
    # gateway); the model / voice / credential defaults still apply.
    url = settings.url.strip() or url
    return ResolvedSpeech(provider, url, model, api_key, voice, _tts_format(kind, settings))


def resolve_stt() -> ResolvedSpeech:
    """The active STT endpoint. Raises SpeechDisabledError / SpeechNotConfiguredError."""
    return _resolve("stt", load_speech_settings().stt, _STT_DEFAULTS, None)


def resolve_tts(settings: SpeechServiceSettings | None = None) -> ResolvedSpeech:
    """The active TTS endpoint. Raises SpeechDisabledError / SpeechNotConfiguredError.

    ``settings`` replaces the saved ``speech.tts`` block for this one call: the
    dashboard's voice preview resolves the form's unsaved values through it.
    """
    return _resolve("tts", settings or load_speech_settings().tts, None, _TTS_DEFAULTS)


def stt_available() -> bool:
    """True when transcription is enabled AND usable (cheap; no network)."""
    try:
        resolve_stt()
        return True
    except (SpeechDisabledError, SpeechNotConfiguredError):
        return False


def tts_available() -> bool:
    """True when synthesis is enabled AND usable (cheap; no network)."""
    try:
        resolve_tts()
        return True
    except (SpeechDisabledError, SpeechNotConfiguredError):
        return False


def tts_audio_format() -> str:
    """The container auto-TTS should request from the active provider.

    Empty when TTS is off/unusable, or when the provider gets no
    ``response_format`` at all (qwen, and ``custom`` until an operator opts in).
    """
    try:
        return resolve_tts().audio_format
    except (SpeechDisabledError, SpeechNotConfiguredError):
        return ""


def audio_extension_for_format(audio_format: str) -> str:
    """File extension for a container name (``opus`` → ``.ogg``).

    Falls back to ``.mp3`` for unknown/blank formats — the near-universal
    default of the OpenAI audio API; :func:`synthesize_speech` corrects the
    extension from the bytes when the provider disagrees.
    """
    return _FORMAT_EXTENSIONS.get((audio_format or "").strip().lower(), ".mp3")


def sniff_audio_format(data: bytes) -> str:
    """Best-effort container detection from the leading bytes (``''`` if unsure).

    Providers are free to ignore ``response_format`` — a self-hosted server may
    not implement it at all, and DashScope has no such parameter. Sniffing is
    what keeps the extension on disk truthful, which is the whole contract the
    platform adapters branch on (``.ogg`` ⇒ voice bubble).
    """
    if len(data) < 12:
        return ""
    if data[:4] == b"OggS":
        return "opus" if b"OpusHead" in data[:64] else "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:3] == b"ID3":
        return "mp3"
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "aac"
    # MPEG audio frame sync (11 set bits) — MPEG-1/2 layer II/III.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return ""


def _record_speech_service(kind: str, provider: str, credits: float | None = None) -> None:
    """Mirror one speech call into the analytics service ledger.

    TTS/STT bill in olano credits on the managed path (``provider == "olano"``,
    through the model gateway) and against the operator's own key otherwise —
    either way no token is produced, so the LLM usage ledger never sees the
    call. Managed calls are non-streamed HTTP, so the gateway's per-call cost
    header IS present on the response — callers pass it through ``credits`` so
    the row carries the real charge instead of "counted, never priced".
    """
    try:
        from olano.analytics.service_ledger import record_service_event

        record_service_event(kind, provider=provider, managed=provider == "olano", credits=credits)
    except Exception:  # pragma: no cover - accounting must never break a reply
        logger.debug("service ledger: speech call not recorded", exc_info=True)


def _speech_credits(r: httpx.Response, provider: str) -> float | None:
    """Best-effort per-call charge from a gateway response's headers."""
    try:
        from olano.analytics.usage_recorder import credits_from_headers

        return credits_from_headers(r.headers, managed=provider == "olano")
    except Exception:  # pragma: no cover - accounting must never break a reply
        return None


def transcribe_file(file_path: str, language: str = "", prompt: str = "") -> str:
    """Transcribe a local audio file via the configured provider.

    Blocking (use ``asyncio.to_thread`` from async code — contextvars carrying
    per-agent credentials propagate). Raises :class:`SpeechDisabledError` /
    :class:`SpeechNotConfiguredError` for policy failures; network/HTTP errors
    propagate as-is for callers to map.
    """
    import httpx

    resolved = resolve_stt()
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with path.open("rb") as f:
        files = {"file": (path.name, f, "application/octet-stream")}
        data = {"model": resolved.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {resolved.api_key}"} if resolved.api_key else {}
        r = httpx.post(resolved.url, headers=headers, files=files, data=data, timeout=120)
    r.raise_for_status()
    _record_speech_service("stt", resolved.provider, _speech_credits(r, resolved.provider))
    return str(r.json().get("text", "")).strip()


def synthesize_speech(
    text: str,
    output_path: str,
    voice: str = "",
    audio_format: str = "",
    settings: SpeechServiceSettings | None = None,
) -> str:
    """Generate speech audio from text via the configured provider.

    Writes the audio next to ``output_path`` and returns **the path actually
    written** — which may differ in extension: the container is sniffed from
    the returned bytes and the file renamed to match, so a caller can trust
    ``.ogg`` to really mean Ogg/Opus (the platform adapters gate voice bubbles
    on exactly that). Blocking; same error contract as :func:`transcribe_file`.

    The requested container is ``audio_format`` when given, else whatever
    :func:`resolve_tts` settled on (the configured ``speech.tts.audio_format``,
    or the provider's auto pick). ``output_path``'s extension deliberately does
    **not** feed into that: a provider we send no format to (``qwen``, and
    ``custom`` until an operator opts in) must stay that way no matter what the
    caller named the file.

    ``settings`` is passed through to :func:`resolve_tts` (the voice preview's
    unsaved form values); every other caller uses the saved configuration.
    """
    import httpx

    resolved = resolve_tts(settings)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {resolved.api_key}"} if resolved.api_key else {}
    requested = (audio_format or "").strip().lower() or resolved.audio_format

    if resolved.provider == "qwen":
        audio = _synthesize_qwen(resolved, text, voice, headers)
        _record_speech_service("tts", resolved.provider)
        return _write_audio(out, audio)

    # OpenAI-compatible `/v1/audio/speech`: the response body *is* the audio.
    payload = {
        "model": resolved.model,
        "input": text[:_TTS_MAX_CHARS],
        "voice": voice.strip() or resolved.voice or "alloy",
    }
    if requested in _FORMAT_EXTENSIONS:
        payload["response_format"] = requested
    r = httpx.post(resolved.url, headers=headers, json=payload, timeout=120)
    if r.status_code == 400 and "response_format" in payload and "format" in r.text.lower():
        # An OpenAI-compatible server is free to not implement the parameter,
        # or to not support this container. Losing the whole voice reply over
        # a *preference* is the wrong trade — retry without it and let the
        # sniffing name whatever comes back.
        logger.info(
            "TTS provider %s rejected response_format=%s; retrying without it",
            resolved.provider,
            payload.pop("response_format"),
        )
        r = httpx.post(resolved.url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    _record_speech_service("tts", resolved.provider, _speech_credits(r, resolved.provider))
    return _write_audio(out, r.content)


def _write_audio(out: Path, data: bytes) -> str:
    """Write ``data`` to ``out``, renaming it to match the real container.

    Only renames on a *positive* identification — unknown bytes keep the
    caller's path rather than being guessed into the wrong extension.
    """
    detected = sniff_audio_format(data)
    if detected:
        expected = _FORMAT_EXTENSIONS[detected]
        if out.suffix.lower() != expected:
            logger.debug(
                "TTS returned %s, renaming %s → %s",
                detected,
                out.name,
                out.with_suffix(expected).name,
            )
            out = out.with_suffix(expected)
    out.write_bytes(data)
    return str(out)


def _synthesize_qwen(
    resolved: ResolvedSpeech, text: str, voice: str, headers: dict[str, str]
) -> bytes:
    """Synthesize via Alibaba DashScope Qwen-TTS and return the raw audio bytes.

    DashScope does not speak the OpenAI audio shape: text and voice nest under
    ``input``, and the response carries a short-lived URL at ``output.audio.url``
    that we then fetch. See
    https://www.alibabacloud.com/help/en/model-studio/qwen-tts-api.
    """
    import httpx

    payload = {
        "model": resolved.model,
        "input": {
            "text": text[:_TTS_MAX_CHARS],
            "voice": voice.strip() or resolved.voice or "Cherry",
        },
    }
    r = httpx.post(resolved.url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    body = r.json()
    audio = (body.get("output") or {}).get("audio") or {}
    audio_url = str(audio.get("url") or "").strip()
    if not audio_url:
        # DashScope reports quota/validation failures as a JSON envelope
        # (``code`` / ``message``), sometimes with HTTP 200 — surface it verbatim.
        detail = body.get("message") or body.get("code") or body
        raise RuntimeError(f"Qwen TTS returned no audio URL: {detail}")
    got = httpx.get(audio_url, timeout=120)
    got.raise_for_status()
    return got.content
