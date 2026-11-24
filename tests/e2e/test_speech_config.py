"""E2e: Config → Speech model/voice dropdowns and reset-on-provider-change.

These exercise browser-only behavior the in-process API tests can't see:

  * the model/voice fields render as real ``<select>`` dropdowns populated from
    the backend ``speech_catalog`` (not free-text inputs), and
  * changing the provider RESETS the provider-specific refinements
    (model/voice/url/api_key_name) so a stale value from the previous provider
    can't leak onto the new one — e.g. a qwen ``Ethan`` voice must not survive a
    switch to OpenAI (whose voices don't include it), and a custom endpoint URL
    must not carry over onto a built-in provider.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

SPEECH = "section.card:has-text('Speech')"
TTS_FIELD = ".admin-field:has-text('Text-to-speech')"


def _open_config(page, dashboard):
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    speech = page.locator(SPEECH).first
    speech.wait_for(state="visible", timeout=20_000)
    speech.scroll_into_view_if_needed()
    return speech


def _option_values(select):
    return select.locator("option").evaluate_all("els => els.map(o => o.value)")


def test_tts_model_and_voice_are_catalog_dropdowns(page, dashboard) -> None:
    speech = _open_config(page, dashboard)
    speech.locator(TTS_FIELD).locator("select").first.select_option("qwen")

    model = speech.locator("select:has(option[value='qwen3-tts-flash'])").first
    voice = speech.locator("select:has(option[value='Cherry'])").first
    model.wait_for(state="visible", timeout=10_000)
    voice.wait_for(state="visible", timeout=10_000)

    # Real <select> dropdowns, populated from the backend catalog.
    assert model.evaluate("el => el.tagName") == "SELECT"
    assert voice.evaluate("el => el.tagName") == "SELECT"
    assert {"qwen3-tts-flash", "qwen-tts"} <= set(_option_values(model))
    voices = set(_option_values(voice))
    assert {"Cherry", "Ethan", "Dylan"} <= voices


def test_changing_provider_resets_model_and_voice(page, dashboard) -> None:
    speech = _open_config(page, dashboard)
    provider = speech.locator(TTS_FIELD).locator("select").first

    # qwen → pick a non-default model AND voice.
    provider.select_option("qwen")
    q_model = speech.locator("select:has(option[value='qwen3-tts-flash'])").first
    q_voice = speech.locator("select:has(option[value='Cherry'])").first
    q_model.wait_for(state="visible", timeout=10_000)
    q_voice.wait_for(state="visible", timeout=10_000)
    q_model.select_option("qwen-tts")
    q_voice.select_option("Ethan")
    assert q_model.input_value() == "qwen-tts"
    assert q_voice.input_value() == "Ethan"

    # Switch to OpenAI → both refinements reset to "" (Provider default), and the
    # qwen-only "Ethan" voice is gone from the options entirely.
    provider.select_option("openai")
    o_model = speech.locator("select:has(option[value='gpt-4o-mini-tts'])").first
    o_voice = speech.locator("select:has(option[value='nova'])").first
    o_model.wait_for(state="visible", timeout=10_000)
    o_voice.wait_for(state="visible", timeout=10_000)
    assert o_model.input_value() == ""
    assert o_voice.input_value() == ""
    o_voices = set(_option_values(o_voice))
    assert "nova" in o_voices and "Ethan" not in o_voices


def test_switching_to_custom_starts_blank(page, dashboard) -> None:
    """A custom endpoint URL must not inherit a previous provider's state."""
    speech = _open_config(page, dashboard)
    provider = speech.locator(TTS_FIELD).locator("select").first

    provider.select_option("qwen")
    speech.locator("select:has(option[value='Cherry'])").first.wait_for(
        state="visible", timeout=10_000
    )
    provider.select_option("custom")

    # Custom → the endpoint URL field appears and starts empty (placeholder only).
    url_input = speech.locator("input[placeholder*='/v1/audio/speech']").first
    url_input.wait_for(state="visible", timeout=10_000)
    assert url_input.input_value() == ""
