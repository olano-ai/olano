"""Tests for the credential-gated multimodal defaults.

Every agent should be able to see, draw, speak and listen without an operator
hand-listing five tool names — but only where the key that would run the tool
actually resolves. A tool the model can see, plan around and then never
successfully call is worse than no tool at all, so the whole feature is the
gate: keys present ⇒ bound; keys absent ⇒ the model is never told it exists.

`tests/conftest.py` pins the availability probes off for the suite (they would
otherwise read the developer machine's own API keys); these tests put back
exactly the probes they mean to exercise.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from olano.config import multimodal_defaults as md
from olano.config.agent_config import AgentConfig
from olano.config.internal_tools import effective_internal_tools
from olano.config.multimodal_defaults import (
    MULTIMODAL_TOOL_NAMES,
    available_multimodal_tool_names,
)
from olano.config.ops_defaults import resolve_effective_tool_names
from olano.core.builder import _build_extra_tools

AGENT = "seebot"

#: The real probe table, snapshotted at import — `conftest.lionclaw_home` swaps
#: it for an empty dict per test, so the tests below that want the genuine
#: credential wiring (not a stub) put this back.
REAL_AVAILABILITY = dict(md._AVAILABILITY)

_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DASHSCOPE_API_KEY",
    "LIONCLAW_VISION_MODEL",
    "LIONCLAW_IMAGE_MODEL",
)


def _names(tools) -> set[str]:
    return {str(getattr(t, "name", "")) for t in tools}


@pytest.fixture
def all_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every multimodal provider resolves."""
    monkeypatch.setattr(md, "_AVAILABILITY", dict.fromkeys(MULTIMODAL_TOOL_NAMES, lambda: True))


@pytest.fixture
def only_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box with a vision-capable model key and nothing else."""
    monkeypatch.setattr(
        md,
        "_AVAILABILITY",
        {name: (lambda n=name: n == "image_describe") for name in MULTIMODAL_TOOL_NAMES},
    )


# ─────────────────────────────── the gate itself ──────────────────────────────


def test_no_keys_grants_nothing():
    # conftest pins every probe off — the no-credentials box.
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == []


def test_all_keys_grants_every_multimodal_tool(all_available: None):
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == MULTIMODAL_TOOL_NAMES


def test_partial_keys_grant_only_what_works(only_vision: None):
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == ["image_describe"]


def test_probe_failure_reads_as_unavailable(monkeypatch: pytest.MonkeyPatch):
    def _boom() -> bool:
        raise RuntimeError("vault is locked")

    monkeypatch.setattr(md, "_AVAILABILITY", dict.fromkeys(MULTIMODAL_TOOL_NAMES, _boom))
    # A vault hiccup must never break an agent build; it just means "no grant".
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == []


def test_default_internal_tools_off_grants_nothing(all_available: None):
    cfg = AgentConfig(name=AGENT, default_internal_tools=False)
    assert available_multimodal_tool_names(cfg) == []


def test_disabled_tools_wins_over_an_available_key(all_available: None):
    cfg = AgentConfig(name=AGENT, disabled_tools=["image_generate", "audio_to_text"])
    granted = available_multimodal_tool_names(cfg)
    assert "image_generate" not in granted
    assert "audio_to_text" not in granted
    assert "image_describe" in granted


def test_explicit_tools_entry_is_not_double_granted(all_available: None):
    cfg = AgentConfig(name=AGENT, tools=["image_generate"])
    # Already in `tools:`, so the implicit grant skips it — it binds either way.
    assert "image_generate" not in available_multimodal_tool_names(cfg)


# ────────────────────────────── what gets bound ───────────────────────────────


def test_builder_binds_the_available_tools(
    base_config: AgentConfig, tmp_workspace: Path, all_available: None
):
    base_config.tools = []
    tools = _build_extra_tools(base_config, tmp_workspace)
    assert set(MULTIMODAL_TOOL_NAMES) <= _names(tools)


def test_builder_binds_nothing_without_keys(base_config: AgentConfig, tmp_workspace: Path):
    base_config.tools = []
    tools = _build_extra_tools(base_config, tmp_workspace)
    assert not (set(MULTIMODAL_TOOL_NAMES) & _names(tools))


def test_builder_honors_disabled_tools(
    base_config: AgentConfig, tmp_workspace: Path, all_available: None
):
    base_config.tools = []
    base_config.disabled_tools = ["text_to_speech"]
    assert "text_to_speech" not in _names(_build_extra_tools(base_config, tmp_workspace))


def test_explicit_tools_entry_binds_without_a_key(base_config: AgentConfig, tmp_workspace: Path):
    """The operator named it, so they get it — and its [missing-credentials]
    reply — rather than a tool that silently vanished."""
    base_config.tools = ["image_generate"]
    assert "image_generate" in _names(_build_extra_tools(base_config, tmp_workspace))


# ───────────────────────────── reporting surfaces ─────────────────────────────


def test_catalog_reports_available_tools_as_on(all_available: None):
    rows = {r["name"]: r for r in effective_internal_tools(AgentConfig(name=AGENT))}
    for name in MULTIMODAL_TOOL_NAMES:
        assert rows[name]["on"] is True, name
        assert rows[name]["category"] == "Multimodal"
        assert rows[name]["description"]


def test_catalog_reports_missing_keys_as_off_and_locked():
    rows = {r["name"]: r for r in effective_internal_tools(AgentConfig(name=AGENT))}
    for name in MULTIMODAL_TOOL_NAMES:
        assert rows[name]["on"] is False, name
        assert rows[name]["toggleable"] is False
        note = rows[name]["note"]
        assert note.startswith("requires ")
        # An off row is the only place an operator learns the capability
        # exists, so the note must lead with the default route (olano credits)
        # AND name something concrete to set for the BYOK alternative.
        assert "olano credits" in note
        assert any(
            token in note
            for token in ("_API_KEY", "LIONCLAW_IMAGE_MODEL", "LIONCLAW_VISION_MODEL", "Speech")
        ), note


def test_effective_tool_names_include_the_grant_when_given_a_config(all_available: None):
    cfg = AgentConfig(name=AGENT)
    without = resolve_effective_tool_names(cfg.tools, cfg.disabled_tools)
    with_cfg = resolve_effective_tool_names(cfg.tools, cfg.disabled_tools, cfg)
    assert not (set(MULTIMODAL_TOOL_NAMES) & set(without))
    assert set(MULTIMODAL_TOOL_NAMES) <= set(with_cfg)


# ───────────────────── the real probes, against real keys ─────────────────────
# Everything above stubs `_AVAILABILITY` to test the grant logic. These test the
# wiring underneath it: that a key in the environment (or in one agent's own
# `config.env`) is what actually decides.


@pytest.fixture
def real_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the genuine credential probes on a keyless environment."""
    monkeypatch.setattr(md, "_AVAILABILITY", REAL_AVAILABILITY)
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_vision_key_decides_image_describe(real_probes: None, monkeypatch: pytest.MonkeyPatch):
    cfg = AgentConfig(name=AGENT)
    assert "image_describe" not in available_multimodal_tool_names(cfg)
    # The default vision model is an Anthropic one, so that is the key it wants.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "image_describe" in available_multimodal_tool_names(cfg)


def test_vision_model_override_moves_the_key_it_needs(
    real_probes: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LIONCLAW_VISION_MODEL", "openai:gpt-5.4-mini")
    cfg = AgentConfig(name=AGENT)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "image_describe" not in available_multimodal_tool_names(cfg)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "image_describe" in available_multimodal_tool_names(cfg)


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"])
def test_any_image_provider_key_enables_generation(
    real_probes: None, monkeypatch: pytest.MonkeyPatch, key: str
):
    cfg = AgentConfig(name=AGENT)
    assert "image_generate" not in available_multimodal_tool_names(cfg)
    monkeypatch.setenv(key, "test-key")
    assert "image_generate" in available_multimodal_tool_names(cfg)


def test_a_gemini_key_alone_does_not_enable_editing(
    real_probes: None, monkeypatch: pytest.MonkeyPatch
):
    """/images/edits is an OpenAI-shaped endpoint Gemini does not serve, so
    editing is the one capability a Google-only box cannot have. (Gemini
    editing lives in nanobanana_image_edit, a separate tool.)"""
    cfg = AgentConfig(name=AGENT)
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    granted = available_multimodal_tool_names(cfg)
    assert "image_generate" in granted  # Gemini can generate…
    assert "image_edit" not in granted  # …but cannot serve this endpoint.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "image_edit" in available_multimodal_tool_names(cfg)


def test_speech_is_off_until_a_provider_is_configured(
    real_probes: None, lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = AgentConfig(name=AGENT)
    # Speech defaults to `disabled` on an unmanaged box: having the key is not
    # consent to ship audio off the machine.
    assert "audio_to_text" not in available_multimodal_tool_names(cfg)
    assert "text_to_speech" not in available_multimodal_tool_names(cfg)

    (lionclaw_home / "config.yaml").write_text(
        "speech:\n  stt:\n    provider: openai\n  tts:\n    provider: openai\n",
        encoding="utf-8",
    )
    granted = available_multimodal_tool_names(cfg)
    assert "audio_to_text" in granted
    assert "text_to_speech" in granted


def test_speech_enabled_without_its_key_stays_off(
    real_probes: None, lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
):
    (lionclaw_home / "config.yaml").write_text(
        "speech:\n  stt:\n    provider: groq\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # wrong provider's key
    assert "audio_to_text" not in available_multimodal_tool_names(AgentConfig(name=AGENT))
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert "audio_to_text" in available_multimodal_tool_names(AgentConfig(name=AGENT))


def test_a_key_in_one_agents_config_env_does_not_leak_to_another(real_probes: None):
    """Per-agent credentials are the whole reason the probe binds an agent
    context: two agents on one box can legitimately disagree about whether
    image generation exists."""
    with_key = AgentConfig(name="painter", env={"OPENAI_API_KEY": "sk-test"})
    without = AgentConfig(name="plain")
    assert "image_generate" in available_multimodal_tool_names(with_key)
    assert "image_generate" not in available_multimodal_tool_names(without)


# ──────────────── the olano-credits default (managed deployment) ──────────────
# The headline behaviour: a managed box needs NO provider key at all. The Olano
# service covers vision, image generation/editing and transcription through the
# model gateway, billed in credits, so those tools are on out of the box. TTS
# is the deliberate exception — voice replies stay a Config → Speech opt-in on
# every install (on olano credits once picked on a managed box).


@pytest.fixture
def managed_box(real_probes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment connected to cloud.olano.ai, holding zero provider keys."""
    from dataclasses import replace

    from olano.deployment import gateway_key as gw
    from olano.deployment import resolver as dep_resolver
    from olano.deployment import sources

    # Keep the real settings object (the cortex/feature gates read it) and move
    # only the posture, so nothing else silently falls over into "disabled".
    real = dep_resolver.deployment_status()
    monkeypatch.setattr(
        dep_resolver,
        "deployment_status",
        lambda *a, **k: replace(real, posture="enforced"),
    )
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    # ensure_gateway_key() would otherwise try to enroll over the network.
    monkeypatch.setattr(gw, "gateway_key", lambda: "sk-olano-virtual")
    monkeypatch.setattr(gw, "ensure_gateway_key", lambda: "sk-olano-virtual")


def test_managed_box_grants_everything_but_tts_with_no_provider_key(managed_box: None):
    """Every capability except TTS, editing included: the gateway serves the
    OpenAI images shape, so a DO deployment needs no provider key for any of
    them. text_to_speech alone waits for the Config → Speech opt-in — voice
    replies are never on by themselves."""
    expected = [n for n in MULTIMODAL_TOOL_NAMES if n != "text_to_speech"]
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == expected


def test_managed_box_grants_tts_once_opted_in(managed_box: None):
    """Picking the olano TTS provider under Config → Speech is all it takes —
    the tool then binds like the rest, still with no provider key."""
    (Path(os.environ["LIONCLAW_HOME"]) / "config.yaml").write_text(
        "speech:\n  tts:\n    provider: olano\n", encoding="utf-8"
    )
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == MULTIMODAL_TOOL_NAMES


def test_managed_box_routes_every_capability_to_olano(managed_box: None):
    """Not just "on" — on *via credits*. Each capability must actually resolve
    to the Olano service, or the grant above would be billing someone's key.
    TTS resolves to olano too once opted in (and only then)."""
    import olano.speech as speech
    from olano.tools.image_tools import _resolve_image_model
    from olano.tools.vision import _vision_model_string

    assert _vision_model_string() == "olano:vision"
    assert _resolve_image_model("") == ("olano", "olano-image")
    assert speech.resolve_stt().provider == "olano"
    with pytest.raises(speech.SpeechDisabledError):
        speech.resolve_tts()  # off until someone picks a provider
    (Path(os.environ["LIONCLAW_HOME"]) / "config.yaml").write_text(
        "speech:\n  tts:\n    provider: olano\n", encoding="utf-8"
    )
    assert speech.resolve_tts().provider == "olano"
    # Editing rides the same gateway, not a customer's OpenAI account.
    from olano.tools.image_tools import openai_images_backend

    assert openai_images_backend("")[:2] == ("olano", "olano-image")


def test_a_byok_key_does_not_move_a_managed_box_off_credits(
    managed_box: None, monkeypatch: pytest.MonkeyPatch
):
    """The initial default on a DO deployment is the Olano service, whether or
    not the customer also holds provider keys. A key present for some other
    purpose is not an instruction to bill their account for every image."""
    from olano.tools.image_tools import _resolve_image_model
    from olano.tools.vision import _vision_model_string

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert _resolve_image_model("") == ("olano", "olano-image")
    assert _vision_model_string() == "olano:vision"
    granted = set(available_multimodal_tool_names(AgentConfig(name=AGENT)))
    assert set(MULTIMODAL_TOOL_NAMES) - {"text_to_speech"} <= granted
    # …and an idle OpenAI key is NOT consent to start voicing replies either.
    assert "text_to_speech" not in granted


def test_the_operator_can_switch_each_capability_to_byok(
    managed_box: None, monkeypatch: pytest.MonkeyPatch
):
    """…and then change it manually. One standing knob per capability, each
    readable from env, vault, or a single agent's config.env."""
    from olano.tools.image_tools import _resolve_image_model
    from olano.tools.vision import _vision_model_string

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LIONCLAW_IMAGE_MODEL", "openai:gpt-image-2.5-flare")
    monkeypatch.setenv("LIONCLAW_VISION_MODEL", "anthropic:claude-haiku-4-5-20251001")
    assert _resolve_image_model("") == ("openai", "gpt-image-2.5-flare")
    assert _vision_model_string() == "anthropic:claude-haiku-4-5-20251001"
    # Speech's switch is the Config → Speech provider; an explicit value wins
    # over the managed olano default there in the same way.
    import olano.speech as speech

    (Path(os.environ["LIONCLAW_HOME"]) / "config.yaml").write_text(
        "speech:\n  tts:\n    provider: openai\n", encoding="utf-8"
    )
    assert speech.resolve_tts().provider == "openai"


def test_one_agent_can_be_pinned_to_byok_without_moving_the_others(managed_box: None):
    """The knobs resolve like any other credential, so the switch is per agent
    when it needs to be."""
    from olano.config.multimodal_defaults import capability_available

    pinned = AgentConfig(name="ownkey", env={"LIONCLAW_IMAGE_MODEL": "openai:gpt-image-2.5-flare"})
    # No OPENAI_API_KEY anywhere ⇒ that agent's pin is unsatisfiable and it
    # loses the tool, while every other agent keeps generating on credits.
    assert capability_available("image_generate", pinned) is False
    assert capability_available("image_generate", AgentConfig(name=AGENT)) is True


def test_unmanaged_box_without_keys_gets_nothing(real_probes: None):
    # The other half of the contract: no gateway, no keys ⇒ the model is never
    # told these capabilities exist.
    assert available_multimodal_tool_names(AgentConfig(name=AGENT)) == []


# ─────────────── the avatar / character generator's backend pick ──────────────
# Same seam, so the dashboard's "Generate cartoon" and the agents' own image
# tools can never disagree about which backend a box uses.


def test_avatar_backend_defaults_to_the_olano_gateway(managed_box: None):
    from olano.deployment.gateway_key import openai_base_url
    from olano.tools.image_tools import openai_images_backend

    provider, model, key, base_url = openai_images_backend("")
    assert (provider, model) == ("olano", "olano-image")
    assert key == "sk-olano-virtual"
    assert base_url == openai_base_url()


def test_avatar_backend_honors_an_explicit_openai_pick(managed_box: None, monkeypatch):
    from olano.tools.image_tools import openai_images_backend

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider, model, key, base_url = openai_images_backend("openai:gpt-image-2.5-flare")
    assert (provider, model, key) == ("openai", "gpt-image-2.5-flare", "sk-test")
    assert base_url == "https://api.openai.com/v1"


def test_avatar_backend_falls_back_to_openai_when_unmanaged(real_probes: None, monkeypatch):
    from olano.tools.image_tools import openai_images_backend

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert openai_images_backend("")[:2] == ("openai", "gpt-image-2.5-flare")


def test_avatar_backend_skips_a_gemini_default_it_cannot_drive(real_probes: None, monkeypatch):
    """Gemini serves image generation through a different request shape, so a
    default landing there must fall through to an OpenAI-shaped option rather
    than handing the SDK a model it cannot call."""
    from olano.tools.image_tools import _resolve_image_model, openai_images_backend

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _resolve_image_model("")[0] == "gemini"  # the agent tool's pick…
    assert openai_images_backend("")[:2] == ("openai", "gpt-image-2.5-flare")  # …not this one's


def test_avatar_backend_rejects_a_provider_this_api_cannot_serve(real_probes: None, monkeypatch):
    from olano.tools.image_tools import openai_images_backend

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    resolved = openai_images_backend("gemini:imagen-3.0-generate-002")
    assert isinstance(resolved, str) and "does not serve the OpenAI images API" in resolved


def test_avatar_backend_is_actionable_on_a_bare_box(real_probes: None):
    from olano.tools.image_tools import openai_images_backend

    resolved = openai_images_backend("")
    assert isinstance(resolved, str)
    # Both routes named, so the operator can pick one instead of guessing.
    assert "cloud.olano.ai" in resolved
    assert "OPENAI_API_KEY" in resolved
