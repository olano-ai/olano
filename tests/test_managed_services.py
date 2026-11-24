"""Olano-metered services on the instance (P5): speech via the gateway,
managed web search/fetch via the cloud service APIs, image gen via the
gateway, vision through the olano turbo tier.

The property under test throughout: a MANAGED box gets these services with no
provider key on disk (billed in olano credits) and an UNMANAGED box keeps its
BYOK/local paths.

Who wins when a managed box ALSO holds a provider key differs by service, on
purpose. For the agent's own multimodal defaults — image generation, vision,
speech — the managed path is the *default*: a DO deployment is meant to work
with zero configuration on credits, and a key set for something else must not
silently move that billing onto the customer's account. Switching those to BYOK
is a deliberate act (`LIONCLAW_IMAGE_MODEL`, `LIONCLAW_VISION_MODEL`, Config →
Speech). Web search/fetch keep the older rule — a present key wins outright —
because there the key IS the configuration.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from olano.deployment import services_client


def _managed(
    monkeypatch,
    managed: bool = True,
    token: str | None = "olano_dep_tok",
    features: dict[str, bool] | None = None,
) -> None:
    """Patch deployment status to a managed/unmanaged posture.

    Includes a `.settings.feature_enabled` stub (default: every flag on) so the
    per-service entitlement probes resolve — pass `features={"olano_tts": False}`
    to withhold one. The stub accepts `now=` because `get_effective_settings`
    calls `deployment_status(now=…)`.
    """
    from olano.deployment import resolver as dep_resolver
    from olano.deployment import sources

    posture = "enforced" if managed else "open"
    feats = features or {}
    settings = SimpleNamespace(feature_enabled=lambda name: feats.get(name, True))
    status = SimpleNamespace(posture=posture, settings=settings)
    monkeypatch.setattr(dep_resolver, "deployment_status", lambda now=None: status)
    monkeypatch.setattr(sources, "deployment_token", lambda: token)


# ── services_client ──────────────────────────────────────────────────────────


def test_services_available_needs_managed_posture_and_token(monkeypatch) -> None:
    _managed(monkeypatch, managed=True, token="tok")
    assert services_client.services_available() is True
    _managed(monkeypatch, managed=False, token="tok")
    assert services_client.services_available() is False
    _managed(monkeypatch, managed=True, token=None)
    assert services_client.services_available() is False


def _fake_post(monkeypatch, status: int, body: dict | None = None):
    calls: list[dict] = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(status, json=body or {}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_service_search_posts_with_deployment_token(monkeypatch) -> None:
    from olano.deployment import sources

    _managed(monkeypatch)
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    calls = _fake_post(
        monkeypatch,
        200,
        {"results": [{"url": "https://a", "title": "A", "excerpts": ["x"]}], "credits_charged": 2},
    )
    data = services_client.service_search(query="weather lisbon", max_results=3)
    assert data["credits_charged"] == 2
    assert calls[0]["url"] == "https://cloud.example.test/api/v1/services/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer olano_dep_tok"
    assert calls[0]["json"]["query"] == "weather lisbon"


@pytest.mark.parametrize(
    "status,code",
    [(402, "credits_exhausted"), (403, "disabled"), (503, "disabled"), (500, "error")],
)
def test_service_errors_classify_by_status(monkeypatch, status, code) -> None:
    from olano.deployment import sources

    _managed(monkeypatch)
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    _fake_post(monkeypatch, status)
    with pytest.raises(services_client.ServiceUnavailable) as err:
        services_client.service_fetch("https://example.com")
    assert err.value.code == code


# ── web_search tool integration ──────────────────────────────────────────────


def test_web_search_uses_managed_service_when_keyless(monkeypatch) -> None:
    from olano.tools import web_search as ws

    _managed(monkeypatch)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        services_client,
        "service_search",
        lambda **kw: {
            "results": [{"url": "https://a.test", "title": "Answer", "excerpts": ["found it"]}],
            "credits_charged": 2,
        },
    )
    out = ws._web_search_impl("anything", max_results=3)
    assert "Answer" in out and "https://a.test" in out


def test_web_search_byok_key_wins_over_managed(monkeypatch) -> None:
    """A customer's own Tavily key is an explicit choice — the managed path
    must not intercept it (they'd be double-paying)."""
    from olano.tools import web_search as ws

    _managed(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-their-own")

    def boom(**kw):  # pragma: no cover - the assertion
        raise AssertionError("managed service must not be called with a BYOK key set")

    monkeypatch.setattr(services_client, "service_search", boom)
    # Tavily client itself isn't reachable in tests; the point is the managed
    # branch was skipped and the Tavily path was entered.
    out = ws._web_search_impl("anything")
    assert "managed service" not in out


def test_web_search_credits_exhausted_is_actionable_not_silent(monkeypatch) -> None:
    from olano.tools import web_search as ws

    _managed(monkeypatch)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def raise_402(**kw):
        raise services_client.ServiceUnavailable("credits_exhausted", "out of olano credits")

    monkeypatch.setattr(services_client, "service_search", raise_402)
    out = ws._web_search_impl("anything")
    assert "out of olano credits" in out


def test_web_search_service_outage_falls_back_to_tavily_error(monkeypatch) -> None:
    """A disabled/down service degrades to the normal keyless message — the
    turn must not hard-fail on infrastructure the customer can't see."""
    from olano.tools import web_search as ws

    _managed(monkeypatch)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def raise_503(**kw):
        raise services_client.ServiceUnavailable("disabled", "service disabled")

    monkeypatch.setattr(services_client, "service_search", raise_503)
    out = ws._web_search_impl("anything")
    assert "TAVILY_API_KEY" in out or "tavily" in out.lower()


# ── web_fetch tool integration ───────────────────────────────────────────────


def test_web_fetch_prefers_managed_extract(monkeypatch) -> None:
    from olano.tools import web_fetch as wf

    _managed(monkeypatch)
    monkeypatch.setattr(
        services_client,
        "service_fetch",
        lambda url, **kw: {
            "pages": [{"url": url, "title": "Doc", "content": "# Heading\n\nBody text."}],
            "credits_charged": 0.5,
        },
    )
    wf._FETCH_CACHE.clear()
    out = wf._web_fetch_impl("https://docs.example.test/page")
    assert '"extractor": "olano-web-service"' in out
    assert "Body text." in out


def test_web_fetch_falls_back_to_local_reader(monkeypatch) -> None:
    from olano.tools import web_fetch as wf

    _managed(monkeypatch)

    def raise_402(url, **kw):
        raise services_client.ServiceUnavailable("credits_exhausted", "empty")

    monkeypatch.setattr(services_client, "service_fetch", raise_402)

    def fake_get(self, url):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>local content</p></body></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    wf._FETCH_CACHE.clear()
    out = wf._web_fetch_impl("https://example.test/page")
    assert "local content" in out
    assert '"olano-web-service"' not in out


# ── speech: the olano provider ───────────────────────────────────────────────


def test_speech_resolves_olano_through_the_gateway(monkeypatch, tmp_path) -> None:
    from olano import speech
    from olano.deployment import gateway_key as gk

    monkeypatch.setenv("LIONCLAW_OLANO_GATEWAY_URL", "https://gw.example.test")
    monkeypatch.setattr(gk, "ensure_gateway_key", lambda: "sk-olano-dep")
    settings = SimpleNamespace(
        provider="olano", model="", url="", api_key_name="", voice="", audio_format=""
    )

    stt = speech._resolve("stt", settings, speech._STT_DEFAULTS, None)
    assert stt.url == "https://gw.example.test/v1/audio/transcriptions"
    assert stt.model == "olano-stt"
    assert stt.api_key == "sk-olano-dep"

    tts = speech._resolve("tts", settings, None, speech._TTS_DEFAULTS)
    assert tts.url == "https://gw.example.test/v1/audio/speech"
    assert tts.model == "olano-tts"
    assert tts.voice == "nova"


def test_speech_olano_without_gateway_is_actionable(monkeypatch) -> None:
    from olano import speech
    from olano.deployment import gateway_key as gk

    monkeypatch.setattr(gk, "ensure_gateway_key", lambda: None)
    settings = SimpleNamespace(provider="olano", model="", url="", api_key_name="", voice="")
    with pytest.raises(speech.SpeechNotConfiguredError, match="cloud.olano.ai"):
        speech._resolve("stt", settings, speech._STT_DEFAULTS, None)


def test_managed_speech_default_is_olano_for_stt_only(monkeypatch, tmp_path) -> None:
    """A managed box with NO speech block gets STT on the olano service but
    keeps TTS off (voice replies are a deliberate opt-in); an explicit choice
    in either direction — and any unmanaged box — is honored."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
    config = tmp_path / "config.yaml"
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))

    settings = ss.load_speech_settings(tmp_path)
    assert settings.stt.provider == "olano"
    assert settings.tts.provider == "disabled"  # TTS never defaults on

    config.write_text("speech:\n  stt:\n    provider: disabled\n", encoding="utf-8")
    settings = ss.load_speech_settings(tmp_path)
    assert settings.stt.provider == "disabled"  # explicit choice wins
    assert settings.tts.provider == "disabled"

    config.write_text("speech:\n  tts:\n    provider: olano\n", encoding="utf-8")
    settings = ss.load_speech_settings(tmp_path)
    assert settings.stt.provider == "olano"  # unset direction still defaults
    assert settings.tts.provider == "olano"  # explicit TTS opt-in works

    monkeypatch.setattr(ss, "managed_olano_default", lambda: False)
    config.unlink()
    settings = ss.load_speech_settings(tmp_path)
    assert settings.stt.provider == "disabled"
    assert settings.tts.provider == "disabled"


# ── image generation: the olano provider ─────────────────────────────────────


def test_image_model_defaults_to_olano_when_managed_and_keyless(monkeypatch) -> None:
    from olano.tools import image_tools as it

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: True)
    assert it._resolve_image_model("") == ("olano", "olano-image")
    # Explicit forms resolve too.
    assert it._resolve_image_model("olano:olano-image") == ("olano", "olano-image")
    assert it._resolve_image_model("olano-image") == ("olano", "olano-image")


def test_managed_default_is_olano_even_with_a_byok_key_present(monkeypatch) -> None:
    """A DO box holding an OpenAI key still generates on credits by default.

    The key may be there for anything else (a model, another toolkit); it is
    not consent to bill this customer's OpenAI account for every image an agent
    draws. Moving off credits is the explicit act below.
    """
    from olano.tools import image_tools as it

    monkeypatch.setenv("OPENAI_API_KEY", "sk-their-own")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: True)
    assert it._resolve_image_model("") == ("olano", "olano-image")


def test_operator_can_move_image_generation_to_byok(monkeypatch) -> None:
    """LIONCLAW_IMAGE_MODEL is the manual switch — the mirror of
    LIONCLAW_VISION_MODEL. Set globally (env/vault) or per agent (config.env)."""
    from olano.tools import image_tools as it

    monkeypatch.setenv("OPENAI_API_KEY", "sk-their-own")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: True)

    monkeypatch.setenv("LIONCLAW_IMAGE_MODEL", "openai:gpt-image-2.5-flare")
    assert it._resolve_image_model("") == ("openai", "gpt-image-2.5-flare")
    # A per-call model still outranks the standing choice.
    assert it._resolve_image_model("olano:olano-image") == ("olano", "olano-image")


def test_image_model_override_reports_its_own_missing_key(monkeypatch) -> None:
    """Pinning a provider you have no key for fails on THAT provider — it must
    not silently fall back to credits, or the operator never learns the pin
    is broken."""
    from olano.tools import image_tools as it

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: True)
    monkeypatch.setenv("LIONCLAW_IMAGE_MODEL", "openai:gpt-image-2.5-flare")
    resolved = it._resolve_image_model("")
    assert isinstance(resolved, str) and "OPENAI_API_KEY" in resolved


def test_unmanaged_box_still_uses_its_byok_key(monkeypatch) -> None:
    """No gateway to bill through ⇒ the BYOK order is the only route, unchanged."""
    from olano.tools import image_tools as it

    monkeypatch.setenv("OPENAI_API_KEY", "sk-their-own")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_IMAGE_MODEL", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: False)
    assert it._resolve_image_model("") == ("openai", it.DEFAULT_OPENAI_IMAGE_MODEL)


def test_image_model_unmanaged_keyless_stays_actionable(monkeypatch) -> None:
    from olano.tools import image_tools as it

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: False)
    resolved = it._resolve_image_model("")
    assert isinstance(resolved, str) and "[missing-credentials]" in resolved


# ── vision: the managed group ────────────────────────────────────────────────


def _clear_vision_env(monkeypatch) -> None:
    for var in (
        "LIONCLAW_VISION_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_vision_default_is_the_olano_group_on_managed(monkeypatch) -> None:
    from olano.tools import vision

    _clear_vision_env(monkeypatch)
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: True)
    assert vision._default_vision_model() == "olano:vision"
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: False)
    assert vision._default_vision_model() == vision._DEFAULT_VISION_MODEL


def test_vision_chain_leads_with_olano_and_keeps_byok_behind_it(monkeypatch) -> None:
    from olano.tools import vision

    _clear_vision_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: True)
    assert vision._vision_chain() == ["olano:vision", vision._DEFAULT_VISION_MODEL]

    monkeypatch.setattr(vision, "_olano_vision_available", lambda: False)
    assert vision._vision_chain() == [vision._DEFAULT_VISION_MODEL]


def test_operator_pin_collapses_the_vision_chain(monkeypatch) -> None:
    """LIONCLAW_VISION_MODEL is the manual switch off credits — the mirror of
    LIONCLAW_IMAGE_MODEL. A pin must not be padded with fallbacks, or the
    operator never learns it is broken."""
    from olano.tools import vision

    _clear_vision_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("LIONCLAW_VISION_MODEL", "openai:gpt-5.4-mini")
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: True)
    assert vision._vision_chain() == ["openai:gpt-5.4-mini"]
    # A per-call model still outranks the standing choice.
    assert vision._vision_chain("anthropic:claude-haiku-4-5-20251001") == [
        "anthropic:claude-haiku-4-5-20251001"
    ]


def test_vision_available_gate(monkeypatch) -> None:
    """The agent-build gate (config/multimodal_defaults): credential-only, and
    a pin with no key behind it answers False so the tool isn't bound."""
    from olano.tools import vision

    _clear_vision_env(monkeypatch)
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: False)
    assert vision.vision_available() is False

    monkeypatch.setattr(vision, "_olano_vision_available", lambda: True)
    assert vision.vision_available() is True

    monkeypatch.setattr(vision, "_olano_vision_available", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    assert vision.vision_available() is True

    monkeypatch.setenv("LIONCLAW_VISION_MODEL", "anthropic:claude-haiku-4-5-20251001")
    assert vision.vision_available() is False


def test_olano_vision_is_a_group_not_a_selectable_tier() -> None:
    """The guarantee this exists for: vision must not ride on the chat tiers,
    which are re-routed on price/latency and have landed on text-only backends.
    Keeping it out of the tier list is also what keeps it out of every picker.
    """
    from olano.models import PROVIDERS
    from olano.models.olano_gateway import (
        OLANO_TIER_INFO,
        OLANO_TIERS,
        OLANO_VISION_MODEL,
        is_olano_model,
        is_olano_utility_model,
        normalize_tier,
    )

    assert OLANO_VISION_MODEL == "olano-vision"
    assert "vision" not in OLANO_TIERS
    assert "vision" not in OLANO_TIER_INFO
    assert normalize_tier("vision") is None
    # Not a tier ⇒ not a brain any dropdown, /model or agent config offers.
    assert "vision" not in (PROVIDERS["olano"].default_models or [])
    # ...and not a tier ⇒ the per-tier streamed-cost estimator ignores it (the
    # gateway's response-cost header bills these non-streamed calls instead).
    assert is_olano_model("olano:vision") is False
    # A cloud catalog row (needed to price it) can't leak it into a picker
    # either — the dashboard's chat list filters utility groups out.
    assert is_olano_utility_model("olano:vision") is True
    assert is_olano_utility_model("olano:olano-image") is True
    assert is_olano_utility_model("olano:pro") is False


def test_agent_config_rejects_a_utility_model_as_a_brain() -> None:
    """A hand-edited agent.json5 (or the raw-config editor / CLI load) that names
    a utility group as a model/brain is refused at parse time — the one
    AgentConfig validator covers every config-parse surface."""
    import pytest as _pytest

    from olano.config.agent_config import AgentConfig, CognitionConfig, SubAgentConfig

    for bad in ("olano:vision", "olano:olano-image"):
        with _pytest.raises(ValueError, match="utility service"):
            AgentConfig(name="x", model=bad)
        with _pytest.raises(ValueError, match="utility service"):
            SubAgentConfig(name="s", description="d", system_prompt="p", model=bad)
        with _pytest.raises(ValueError, match="utility service"):
            CognitionConfig(fast_model=bad)
        with _pytest.raises(ValueError, match="utility service"):
            CognitionConfig(deep_model=bad)

    # A real tier is accepted, and None (inherit) is fine.
    assert AgentConfig(name="x", model="olano:pro").model == "olano:pro"
    assert CognitionConfig(fast_model=None).fast_model is None


def test_per_service_entitlement_gates_each_probe(monkeypatch) -> None:
    """A managed box with a service WITHHELD (its per-service flag off) must
    report that service unavailable, so the tool falls through instead of
    leading with an olano rung that 402s. Each flag is independent."""
    from olano.tools import image_tools as it
    from olano.tools import vision

    # Every managed service on by default (missing flag ⇒ enabled).
    _managed(monkeypatch, managed=True, token="tok")
    assert it._olano_image_available() is True
    assert vision._olano_vision_available() is True
    assert services_client.services_available() is True

    # Withhold image → image off, vision unaffected (independent flags).
    _managed(monkeypatch, managed=True, token="tok", features={"olano_image": False})
    assert it._olano_image_available() is False
    assert vision._olano_vision_available() is True

    # Withhold vision → vision off, image unaffected.
    _managed(monkeypatch, managed=True, token="tok", features={"olano_vision": False})
    assert vision._olano_vision_available() is False
    assert it._olano_image_available() is True

    # Withhold web search → the cloud services API reads unavailable.
    _managed(monkeypatch, managed=True, token="tok", features={"olano_web_search": False})
    assert services_client.services_available() is False


def test_olano_speech_respects_the_stt_tts_entitlement(monkeypatch) -> None:
    """The Olano speech path refuses (falls back) when its per-service flag is
    off, even with a gateway key — checked before the credential."""
    import olano.deployment.gateway_key as gk
    from olano import speech

    settings = SimpleNamespace(provider="olano", model="", url="", api_key_name="", voice="")

    # Withheld: refuses regardless of a present gateway key.
    monkeypatch.setattr(gk, "ensure_gateway_key", lambda: "sk-olano-dep")
    _managed(monkeypatch, managed=True, token="tok", features={"olano_stt": False})
    with pytest.raises(speech.SpeechNotConfiguredError, match="does not include"):
        speech._resolve("stt", settings, speech._STT_DEFAULTS, None)

    # Flag on: resolves to the olano gateway endpoint.
    monkeypatch.setattr(gk, "openai_base_url", lambda: "https://gateway.olano.ai/v1")
    _managed(monkeypatch, managed=True, token="tok")
    resolved = speech._resolve("stt", settings, speech._STT_DEFAULTS, None)
    assert resolved.provider == "olano"


def test_vision_availability_needs_a_billable_deployment(monkeypatch) -> None:
    """Posture alone is not enough: without a gateway key or deployment token
    the managed call would 401, so the chain must skip it."""
    from olano.deployment import gateway_key as gk
    from olano.tools import vision

    _managed(monkeypatch, managed=True, token=None)
    monkeypatch.setattr(gk, "gateway_key", lambda: None)
    assert vision._olano_vision_available() is False

    monkeypatch.setattr(gk, "gateway_key", lambda: "sk-olano-dep-key")
    assert vision._olano_vision_available() is True

    _managed(monkeypatch, managed=False, token="olano_dep_tok")
    assert vision._olano_vision_available() is False


# ── cognition models: the managed tiers ──────────────────────────────────────


def test_cognition_models_default_to_olano_tiers_when_managed(monkeypatch, tmp_path) -> None:
    """A DO deployment runs cognition on credits with no provider key. `turbo`
    is the fast tier (it exists to be the fast, no-knobs one — `flash` is its
    legacy alias); `pro` backs the deep runs."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    settings = ss.load_system_settings(tmp_path)

    assert settings.fast_model == "olano:turbo"
    assert settings.deep_model == "olano:pro"


def test_cognition_models_stay_byok_when_unmanaged(monkeypatch, tmp_path) -> None:
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: False)
    settings = ss.load_system_settings(tmp_path)

    assert settings.fast_model == ss.SystemSettings().fast_model
    assert settings.deep_model == ss.SystemSettings().deep_model


def test_an_explicit_cognition_model_beats_the_managed_default(monkeypatch, tmp_path) -> None:
    """Each direction is independent: pinning `fast` to a BYOK model must not
    drag `deep` off credits with it."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
    (tmp_path / "config.yaml").write_text(
        "cognition:\n  fast_model: openai:gpt-5.4-mini\n", encoding="utf-8"
    )
    settings = ss.load_system_settings(tmp_path)

    assert settings.fast_model == "openai:gpt-5.4-mini"
    assert settings.deep_model == "olano:pro"


def test_an_explicit_null_deep_model_is_honored(monkeypatch, tmp_path) -> None:
    """`deep_model: null` means 'inherit the agent's own model' — a deliberate
    choice, not an absent key, so the managed default must not overwrite it."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
    (tmp_path / "config.yaml").write_text("cognition:\n  deep_model: null\n", encoding="utf-8")
    settings = ss.load_system_settings(tmp_path)

    assert settings.deep_model is None
    assert settings.fast_model == "olano:turbo"


def test_clearing_the_fast_model_resets_to_the_boxs_own_default(monkeypatch, tmp_path) -> None:
    """Clearing the dashboard field must not quietly move a managed box off
    credits by resetting to the BYOK literal."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
    ss.set_cognition_model("fast_model", "openai:gpt-5.4-mini", tmp_path)
    ss.set_cognition_model("fast_model", "", tmp_path)

    assert ss.load_system_settings(tmp_path).fast_model == "olano:turbo"
