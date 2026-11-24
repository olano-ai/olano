"""Source-option catalog for the dashboard Config → "Media & search" section.

The counterpart to :func:`olano.speech.speech_catalog`: it builds the grouped
option lists the dashboard renders for the customer's per-feature SOURCE choice
(vision, image generation, web search). Each feature returns a list of
``{value, label, olano?}`` option dicts computed live from:

- the synced model catalog's capability flags (``vision`` for vision models,
  ``imageGen`` for image-generation models — the latter are excluded from
  ``/api/catalog/providers`` because they are not chat-modality, which is why
  this option list is assembled server-side rather than on the client);
- which BYOK provider keys are actually present (``get_credential``); and
- whether the managed Olano service is available/entitled for that feature.

``"olano"`` is a SINGLE option — the customer picks the olano service, never the
backend model inside it (audience split). ``value`` is what the resolvers read:
a ``provider:model`` string, the ``"olano"`` sentinel, or (web search)
``"olano"``/``"tavily"``. Blank (not in any list) means "Auto".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Providers whose key can arrive under more than one name (mirrors vision.py).
_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
}


def _provider_key_present(provider_key: str) -> bool:
    """True when a BYOK key for this provider resolves for the box."""
    from olano.tools._credentials import get_credential

    names = _PROVIDER_ENV.get(provider_key)
    if names is None:
        try:
            from olano.models import PROVIDERS

            info = PROVIDERS.get(provider_key)
            names = (info.env_var,) if info and info.env_var else ()
        except Exception:  # pragma: no cover - defensive
            names = ()
    return any(get_credential(n, "") for n in names)


def _provider_name(provider_key: str) -> str:
    try:
        from olano.models import PROVIDERS

        info = PROVIDERS.get(provider_key)
        if info and info.name:
            return info.name
    except Exception:  # pragma: no cover - defensive
        pass
    return provider_key


def _image_value_provider(provider_key: str) -> str:
    """The provider prefix ``image_tools._resolve_image_model`` expects.

    The catalog labels Google image models ``google_genai``/``google`` but the
    image resolver only knows ``gemini:`` / ``openai:`` / ``olano:``.
    """
    if provider_key in ("google_genai", "google", "gemini"):
        return "gemini"
    return provider_key


def _catalog_models() -> list[dict[str, Any]]:
    try:
        from olano.catalog.models_registry import get_model_catalog

        return list(get_model_catalog().get("models") or [])
    except Exception:  # pragma: no cover - defensive
        logger.debug("media_catalog: catalog read failed", exc_info=True)
        return []


def _capable_options(
    capability: str, *, image_value: bool, require_modality: str | None = None
) -> list[dict[str, Any]]:
    """BYOK options for models carrying ``capability`` (``vision``/``imageGen``),
    grouped by provider whose key is present, non-deprecated, deduped.

    ``require_modality`` filters to a primary modality — vision passes ``"chat"``
    so an image-generation model that happens to carry a ``vision`` flag (it can
    take an image as *input* for editing) is not offered as an image→text model.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _catalog_models():
        if not m.get(capability) or m.get("deprecated"):
            continue
        if require_modality is not None and str(m.get("modality") or "chat") != require_modality:
            continue
        provider = str(m.get("provider") or "")
        model = str(m.get("model") or "")
        if not provider or not model or provider == "olano":
            continue
        if not _provider_key_present(provider):
            continue
        value = (
            f"{_image_value_provider(provider)}:{model}"
            if image_value
            else str(m.get("id") or f"{provider}:{model}")
        )
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        label = f"{_provider_name(provider)} · {m.get('displayName') or model}"
        out.append({"value": value, "label": label, "olano": False})
    out.sort(key=lambda o: o["label"].lower())
    return out


def _vision_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    try:
        from olano.tools.vision import _olano_vision_available

        if _olano_vision_available():
            options.append(
                {"value": "olano", "label": "Olano Vision (olano credits)", "olano": True}
            )
    except Exception:  # pragma: no cover - defensive
        logger.debug("media_catalog: vision availability check failed", exc_info=True)
    options.extend(_capable_options("vision", image_value=False, require_modality="chat"))
    return options


def _image_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    try:
        from olano.tools.image_tools import _olano_image_available

        if _olano_image_available():
            options.append(
                {"value": "olano", "label": "Olano Image (olano credits)", "olano": True}
            )
    except Exception:  # pragma: no cover - defensive
        logger.debug("media_catalog: image availability check failed", exc_info=True)
    options.extend(_capable_options("imageGen", image_value=True))
    return options


def _web_search_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    try:
        from olano.deployment.services_client import services_available

        if services_available():
            options.append(
                {"value": "olano", "label": "Olano web search (olano credits)", "olano": True}
            )
    except Exception:  # pragma: no cover - defensive
        logger.debug("media_catalog: web-search availability check failed", exc_info=True)
    try:
        from olano.tools._credentials import get_credential

        if get_credential("TAVILY_API_KEY", ""):
            options.append(
                {"value": "tavily", "label": "Tavily (your TAVILY_API_KEY)", "olano": False}
            )
    except Exception:  # pragma: no cover - defensive
        pass
    options.append({"value": "disabled", "label": "Disabled", "olano": False})
    return options


def media_catalog() -> dict[str, list[dict[str, Any]]]:
    """The grouped source options for vision, image, and web search.

    Never raises — any probe failure yields a shorter list (at minimum the
    web-search ``disabled`` option), never an error. Runs on the
    ``/api/system/config`` read path (not the hot agent-build path).
    """
    return {
        "vision": _vision_options(),
        "image": _image_options(),
        "web_search": _web_search_options(),
    }
