"""Tests for the server-side media/web-search source catalog.

`media_catalog()` feeds the dashboard Config → "Media & search" dropdowns. It is
assembled from three inputs — the synced model catalog's capability rows, live
BYOK key presence, and the managed-service availability predicates — so the
tests drive those three inputs directly (monkeypatching the module's own
helpers) rather than depend on whatever keys the machine running pytest exports.
"""

from __future__ import annotations

from typing import Any

import pytest

import olano.deployment.services_client as services_client
import olano.media_catalog as mc
import olano.tools.image_tools as image_tools
import olano.tools.vision as vision

# A deterministic stand-in for the synced catalog. It deliberately contains the
# tricky rows: an image-gen model that ALSO carries a vision flag (it can take an
# image as an edit input), a deprecated vision model, and an olano row — none of
# which may leak into the wrong list.
_FAKE_MODELS: list[dict[str, Any]] = [
    {
        "provider": "openai",
        "model": "gpt-4o",
        "id": "openai:gpt-4o",
        "displayName": "GPT-4o",
        "vision": True,
        "modality": "chat",
    },
    {
        "provider": "openai",
        "model": "gpt-image-2.5-flare",
        "id": "openai:gpt-image-2.5-flare",
        "displayName": "GPT Image 2",
        "vision": True,
        "imageGen": True,
        "modality": "image_gen",
    },
    {
        "provider": "openai",
        "model": "dall-e-3",
        "id": "openai:dall-e-3",
        "displayName": "DALL·E 3",
        "imageGen": True,
        "modality": "image_gen",
    },
    {
        "provider": "google_genai",
        "model": "gemini-2.5-flash",
        "id": "google_genai:gemini-2.5-flash",
        "displayName": "Gemini 2.5 Flash",
        "vision": True,
        "modality": "chat",
    },
    {
        "provider": "google_genai",
        "model": "imagen-4.0-generate-001",
        "id": "google_genai:imagen-4.0-generate-001",
        "displayName": "Imagen 4",
        "imageGen": True,
        "modality": "image_gen",
    },
    {
        "provider": "anthropic",
        "model": "claude-old",
        "id": "anthropic:claude-old",
        "displayName": "Old",
        "vision": True,
        "modality": "chat",
        "deprecated": True,
    },
    {
        "provider": "olano",
        "model": "vision",
        "id": "olano:vision",
        "displayName": "Olano Vision",
        "vision": True,
        "modality": "chat",
    },
]


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the catalog + turn every managed predicate off, key OpenAI+Gemini on."""
    monkeypatch.setattr(mc, "_catalog_models", lambda: list(_FAKE_MODELS))
    # BYOK: OpenAI and Google keyed; Anthropic not (so its rows never appear).
    monkeypatch.setattr(
        mc,
        "_provider_key_present",
        lambda p: p in ("openai", "google_genai", "google", "gemini"),
    )
    # Managed services all off by default; individual tests turn one on.
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: False)
    monkeypatch.setattr(image_tools, "_olano_image_available", lambda: False)
    monkeypatch.setattr(services_client, "services_available", lambda: False)


def test_media_catalog_shape_never_raises(fake_catalog: None) -> None:
    cat = mc.media_catalog()
    assert set(cat) == {"vision", "image", "web_search"}
    assert all(isinstance(v, list) for v in cat.values())


def test_vision_excludes_image_gen_models(fake_catalog: None) -> None:
    """An image-gen model carrying a vision flag must NOT appear as a vision
    (image→text) option — vision requires modality == 'chat'."""
    values = {o["value"] for o in mc.media_catalog()["vision"]}
    assert "openai:gpt-4o" in values
    assert "google_genai:gemini-2.5-flash" in values
    # The image-gen models — even the one with a stray vision flag — are excluded.
    assert "openai:gpt-image-2.5-flare" not in values
    assert "openai:dall-e-3" not in values
    assert "google_genai:imagen-4.0-generate-001" not in values


def test_vision_excludes_deprecated_and_unkeyed_providers(fake_catalog: None) -> None:
    values = {o["value"] for o in mc.media_catalog()["vision"]}
    assert "anthropic:claude-old" not in values  # deprecated AND unkeyed


def test_image_options_use_resolver_provider_prefix(fake_catalog: None) -> None:
    """Image values carry the prefix the image resolver expects: openai: as-is,
    google_genai → gemini:."""
    values = {o["value"] for o in mc.media_catalog()["image"]}
    assert "openai:dall-e-3" in values
    assert "openai:gpt-image-2.5-flare" in values
    assert "gemini:imagen-4.0-generate-001" in values
    # No chat models in the image list.
    assert "openai:gpt-4o" not in values


def test_web_search_always_offers_disabled(fake_catalog: None) -> None:
    values = [o["value"] for o in mc.media_catalog()["web_search"]]
    assert values[-1] == "disabled"  # always the last resort


def test_olano_option_appears_only_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc, "_catalog_models", list)
    monkeypatch.setattr(mc, "_provider_key_present", lambda p: False)
    monkeypatch.setattr(vision, "_olano_vision_available", lambda: True)
    monkeypatch.setattr(image_tools, "_olano_image_available", lambda: True)
    cat = mc.media_catalog()
    assert cat["vision"] == [
        {"value": "olano", "label": "Olano Vision (olano credits)", "olano": True}
    ]
    assert cat["image"][0]["value"] == "olano"
    assert cat["image"][0]["olano"] is True


def test_olano_option_absent_when_unavailable(fake_catalog: None) -> None:
    # fake_catalog pins all managed predicates off.
    assert all(not o.get("olano") for o in mc.media_catalog()["vision"])
    assert all(not o.get("olano") for o in mc.media_catalog()["image"])
