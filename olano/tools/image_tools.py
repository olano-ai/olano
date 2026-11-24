"""ImageAgent tools — text-to-image generation and image editing.

These tools WRITE images. Nothing here can read one: describing / analyzing /
OCR-ing an existing image is `tools/vision.py` (`image_describe`), and an agent
that reaches for `image_generate` when vision fails is on a dead end — the tool
descriptions say so, because one did.

Backends, in the order an unpinned `image_generate` tries them:
  Olano-managed image service — billed in olano credits, managed deployments
  Google Gemini (Nano Banana) — GOOGLE_API_KEY or GEMINI_API_KEY
  OpenAI GPT Image — OPENAI_API_KEY

A *pinned* model (per-call ``model=`` or ``LIONCLAW_IMAGE_MODEL``) is used as-is
with no fallback, so an operator always learns their pin is broken; an unpinned
call falls through to the next configured backend.

Generated images are saved as PNGs under ``<workspace>/images/`` and the
tools return the absolute local path (bare, so channel adapters deliver the
file natively) plus a markdown image reference (dashboard inline rendering).
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.security.ssrf import assert_safe_outbound_url
from olano.tools._credentials import get_credential
from olano.tools._sandbox_exec import handoff_to_sandbox

logger = logging.getLogger(__name__)

# Operator override for the image model, the mirror of vision's
# LIONCLAW_VISION_MODEL: unset means "let the resolver pick" (Olano credits
# where available), a value pins a provider — `openai:gpt-image-2.5-flare`,
# `gemini:gemini-3.1-flash-image`, `olano:olano-image`. Read through
# get_credential, so it can be set globally (env/vault) or for one agent
# (config.env) like any other credential.
_IMAGE_MODEL_ENV = "LIONCLAW_IMAGE_MODEL"

# The BYOK OpenAI default. One constant, deliberately: it is the unpinned
# fallback, the name every "[missing-credentials]" line suggests, and the entry
# `_available_image_backends` advertises, so the tool an agent is told to retry
# with is always the one an unpinned call would have used.
#
# gpt-image-2 is NOT removed from the catalog when this moves. Someone pinned
# it, and a pin is strict here by design (no fallback), so an old
# LIONCLAW_IMAGE_MODEL or a per-agent config.env keeps working untouched.
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2.5-flare"
# The gateway's image GROUP name (Olano-managed, billed in credits). What
# backs it is the gateway's routing decision, never named on this box.
OLANO_IMAGE_MODEL = "olano-image"


def gateway_media_reachable() -> bool:
    """A box that can bill the media gateway: a managed deployment with a
    gateway credential (a live key, or a deployment token to enroll one), or a
    self-hosted box holding a pasted Olano account key.

    This is the shared-CREDENTIAL half of media availability. Image, vision, tts
    and stt all ride the same gateway key, so the credential check lives in one
    place; each service ANDs its OWN per-service entitlement flag on top
    (`enforcement.service_entitled`), so a plan can withhold one without killing
    the others.
    """
    try:
        from olano.deployment import resolver as dep_resolver
        from olano.deployment.gateway_key import account_key_if_unmanaged, gateway_key
        from olano.deployment.sources import deployment_token

        if dep_resolver.deployment_status().posture != "open":
            return bool(gateway_key() or deployment_token())
        # A self-hosted box reaches the same gateway with a pasted account key
        # (the second key family); nothing else makes it reachable there.
        return bool(account_key_if_unmanaged())
    except Exception:  # pragma: no cover - defensive
        return False


def _olano_image_available() -> bool:
    """Managed box entitled to bill IMAGE generation/edit through the gateway.

    Credential reachable AND the `olano_image` entitlement is on (which covers
    both generation and edit — they share the olano-image gateway entry).
    """
    from olano.deployment import enforcement

    return gateway_media_reachable() and enforcement.service_entitled("image")


def olano_managed_media_available() -> bool:
    """Public image-media availability (the `image_gen` capability + the avatar
    image backend). Image-entitled; other media services apply their own flag."""
    return _olano_image_available()


def image_edit_available() -> bool:
    """True when ``image_edit`` can actually run.

    Not the same answer as :func:`image_generation_available`: ``/images/edits``
    is an OpenAI-shaped endpoint that **Gemini does not serve**, so a box whose
    only image key is a Google one can generate but not edit. Everything else
    matches — the Olano gateway serves this shape too (it routes ``olano-image``
    edit requests to an edit-capable OpenAI model), so a managed deployment
    edits on credits with no provider key, exactly like it generates.
    """
    try:
        return isinstance(openai_images_backend(""), tuple)
    except Exception:
        return False


def openai_images_backend(model: str = "") -> tuple[str, str, str, str] | str:
    """Resolve an image request to a backend that speaks the OpenAI *images* API.

    Returns ``(provider, model_name, api_key, base_url)`` for the two providers
    that serve ``/images/generations`` + ``/images/edits`` in that shape — the
    Olano gateway and OpenAI itself — or an actionable error string.

    This is the seam the avatar / character generators use, so their default
    matches the agent tools': Olano credits first on a managed box, the
    customer's own account when ``LIONCLAW_IMAGE_MODEL`` says so or when there
    is no gateway. Gemini and Ollama serve image generation through entirely
    different request shapes, so a default that lands there falls through to
    the OpenAI-shaped options rather than failing.
    """
    resolved = _resolve_image_model(model)
    if isinstance(resolved, tuple) and resolved[0] in ("olano", "openai"):
        provider, name = resolved
    elif model.strip():
        # An explicit pick this API cannot drive — say so rather than silently
        # substituting a provider the caller did not ask for.
        return (
            f"[tool-error] {model!r} does not serve the OpenAI images API this "
            f"generator uses. Pick olano:{OLANO_IMAGE_MODEL} or "
            f"openai:{DEFAULT_OPENAI_IMAGE_MODEL}."
        )
    elif _olano_image_available():
        provider, name = "olano", OLANO_IMAGE_MODEL
    elif get_credential("OPENAI_API_KEY", ""):
        provider, name = "openai", DEFAULT_OPENAI_IMAGE_MODEL
    elif isinstance(resolved, str):
        return resolved
    else:
        return (
            "[missing-credentials] No image backend available. Connect this "
            "deployment to cloud.olano.ai to generate on olano credits, or set "
            "OPENAI_API_KEY."
        )

    if provider == "olano":
        from olano.deployment.gateway_key import ensure_gateway_key, openai_base_url

        key = ensure_gateway_key() or ""
        if not key:
            return (
                "[missing-credentials] The Olano image service needs a deployment "
                "connected to cloud.olano.ai."
            )
        return ("olano", name, key, openai_base_url())

    key = get_credential("OPENAI_API_KEY", "")
    if not key:
        return "[missing-credentials] OPENAI_API_KEY not set."
    return ("openai", name, key, "https://api.openai.com/v1")


def image_generation_available() -> bool:
    """True when *some* image provider is usable for the agent bound to this turn.

    The gate ``config/multimodal_defaults`` uses to decide whether
    ``image_generate`` / ``image_edit`` are worth binding at all. Delegates to
    the same resolver the tools themselves call with no explicit model, so the
    default grant and the default call can never disagree about which keys
    count. Credential-only — no network, no provider import.
    """
    try:
        return isinstance(_resolve_image_model(""), tuple)
    except Exception:
        return False


DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"  # Nano Banana 2
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# The three sizes every gpt-image model accepts, which is what we send; map the
# friendlier aspect-ratio param onto them. gpt-image-2.5-* additionally take an
# `aspect_ratio` param and arbitrary WxH, which would let 16:9 and 21:9 stop
# collapsing onto one 3:2 box — deliberately not wired up here, because it is a
# behaviour change for every caller rather than part of swapping the default.
_OPENAI_SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "4:3": "1536x1024",
    "21:9": "1536x1024",
    "9:16": "1024x1536",
    "3:4": "1024x1536",
}


def _workspace_root() -> Path:
    """Resolve the running agent's workspace root from the request context."""
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _images_dir() -> Path:
    d = _workspace_root() / "images"
    existed = d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if not existed:
        handoff_to_sandbox(d)
    return d


def _google_api_key() -> str:
    return get_credential("GOOGLE_API_KEY", "") or get_credential("GEMINI_API_KEY", "")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "image"


def _save_images(images: list[bytes], prompt: str) -> list[Path]:
    out_dir = _images_dir()
    stem = f"{_slug(prompt)}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    paths: list[Path] = []
    for i, data in enumerate(images, 1):
        path = out_dir / f"{stem}-{i}.png"
        path.write_bytes(data)
        # Written by the runtime process; without this the sandboxed agent that
        # asked for the image can read it and cannot edit or delete it.
        handoff_to_sandbox(path)
        paths.append(path)
    return paths


def _record_image_service(label: str, count: int, credits: float | None = None) -> None:
    """Mirror generated images into the analytics service ledger.

    Called from :func:`_format_result` — the one point every image tool passes
    through on success, carrying the ``provider:model`` label that says who was
    billed. Image generation spends olano credits (or the operator's own
    provider key) without producing a token, so the LLM usage ledger never sees
    it and Insights would otherwise under-report a credits account's burn.
    Managed calls are plain (non-streamed) HTTP, so the gateway's per-call cost
    header is on the response — call sites pass it through ``credits`` so the
    row carries the real charge.
    """
    try:
        from olano.analytics.service_ledger import record_service_event

        provider = label.partition(":")[0] or "unknown"
        record_service_event(
            "image",
            provider=provider,
            managed=provider == "olano",
            units=max(1, count),
            credits=credits,
        )
    except Exception:  # pragma: no cover - accounting must never break a tool
        logger.debug("service ledger: image call not recorded", exc_info=True)


def _format_result(
    paths: list[Path], label: str, notes: str = "", credits: float | None = None
) -> str:
    _record_image_service(label, len(paths), credits)
    # Bare absolute paths (no backticks) so PlatformAdapter.extract_local_files
    # picks them up for native media delivery; the markdown image lets the
    # dashboard render inline.
    lines = [f"Generated {len(paths)} image(s) with {label}:"]
    for p in paths:
        lines.append(f"Saved: {p}")
        lines.append(f"![{p.stem}]({p})")
    if notes:
        lines.append(f"\n{notes}")
    return "\n".join(lines)


def _load_image_bytes(src: str) -> tuple[bytes, str]:
    """Load an image from a local path (absolute, ~, or workspace-relative) or URL."""
    src = src.strip()
    if src.startswith(("http://", "https://")):
        from olano.security.request_context import get_request_context
        from olano.security.ssrf import redirect_guard

        security = dict(get_request_context().metadata.get("security", {}) or {})
        allowlist = list(security.get("ssrf_allowlist", []) or [])
        blocklist = list(security.get("ssrf_blocklist", []) or [])
        assert_safe_outbound_url(src, allowlist=allowlist, blocklist=blocklist)
        # Every redirect hop gets the same decision as the first URL.
        hooks = {"request": [redirect_guard(allowlist=allowlist, blocklist=blocklist)]}
        with httpx.Client(timeout=30, follow_redirects=True, event_hooks=hooks) as client:
            r = client.get(src)
        r.raise_for_status()
        mime = r.headers.get("content-type", "").split(";")[0].strip()
        if not mime.startswith("image/"):
            ext = Path(src.split("?")[0]).suffix.lower()
            mime = _MIME_TYPES.get(ext, "image/png")
        return r.content, mime
    p = Path(src).expanduser()
    if not p.is_file():
        p = _workspace_root() / src
    if not p.is_file():
        raise ValueError(f"image not found: {src}")
    return p.read_bytes(), _MIME_TYPES.get(p.suffix.lower(), "image/png")


def _pinned_image_model() -> str:
    """The operator's standing image-source choice, or "" for none.

    Precedence: the dashboard **media.image** selection (Config → Media &
    search) first, then the legacy ``LIONCLAW_IMAGE_MODEL`` env/vault pin as the
    fallback. The dashboard's ``"olano"`` sentinel resolves to the managed image
    group. A pin is strict (used as-is, no fallback), like ``LIONCLAW_IMAGE_MODEL``.
    """
    try:
        from olano.config.system_settings import load_media_settings

        choice = (load_media_settings().image or "").strip()
    except Exception:  # pragma: no cover - defensive
        choice = ""
    if choice:
        return f"olano:{OLANO_IMAGE_MODEL}" if choice.lower() == "olano" else choice
    return get_credential(_IMAGE_MODEL_ENV, "").strip()


def _resolve_image_model(model: str) -> tuple[str, str] | str:
    """Resolve a ``provider:model`` string to ``(provider, model_name)``.

    Returns an error string (``[missing-credentials]`` / ``[tool-error]``)
    when the request cannot be satisfied.
    """
    # A per-call `model` wins; otherwise the operator's standing choice
    # (dashboard media.image → LIONCLAW_IMAGE_MODEL, the mirror of
    # LIONCLAW_VISION_MODEL) decides.
    model = model.strip() or _pinned_image_model()
    if not model:
        # Nothing chosen ⇒ the Olano image service, whenever this box can bill
        # it. Managed deployments are meant to work with zero configuration and
        # everything on credits, so credits are the DEFAULT rather than the
        # fallback — a customer who set an image key for something else does
        # not thereby opt their agents' image generation onto their own bill.
        # Moving to BYOK is a deliberate act: set LIONCLAW_IMAGE_MODEL (or pass
        # `model=` per call). Unmanaged boxes have no gateway, so they keep the
        # BYOK order below as their only route.
        if _olano_image_available():
            return ("olano", OLANO_IMAGE_MODEL)
        if _google_api_key():
            return ("gemini", DEFAULT_GEMINI_IMAGE_MODEL)
        if get_credential("OPENAI_API_KEY", ""):
            return ("openai", DEFAULT_OPENAI_IMAGE_MODEL)
        return (
            "[missing-credentials] No image provider available. Connect this "
            "deployment to cloud.olano.ai to generate images on olano credits, "
            "or set GOOGLE_API_KEY / GEMINI_API_KEY (Gemini Nano Banana) or "
            f"OPENAI_API_KEY ({DEFAULT_OPENAI_IMAGE_MODEL})."
        )

    provider, _, name = model.partition(":")
    if not name:
        # Bare model name — infer the provider from its prefix.
        name = provider
        low = name.lower()
        if low.startswith(("gpt-", "dall-")):
            provider = "openai"
        elif low.startswith(("gemini", "imagen")):
            provider = "gemini"
        elif low == OLANO_IMAGE_MODEL:
            provider = "olano"
        else:
            provider = ""

    if provider == "olano":
        if not _olano_image_available():
            return (
                "[missing-credentials] The Olano image service needs a deployment "
                "connected to cloud.olano.ai."
            )
        return ("olano", OLANO_IMAGE_MODEL)
    if provider == "openai":
        if not get_credential("OPENAI_API_KEY", ""):
            return "[missing-credentials] OPENAI_API_KEY not set."
        return ("openai", name or DEFAULT_OPENAI_IMAGE_MODEL)
    if provider == "gemini":
        if not _google_api_key():
            return "[missing-credentials] GOOGLE_API_KEY / GEMINI_API_KEY not set."
        return ("gemini", name or DEFAULT_GEMINI_IMAGE_MODEL)
    return (
        f"[tool-error] Unknown image provider {provider or model!r}. "
        "Use openai:<model>, gemini:<model> or olano:olano-image."
    )


def _gemini_generate_images(
    model: str, parts: list[dict[str, Any]], aspect_ratio: str, key: str
) -> tuple[list[bytes], str]:
    """Call Gemini generateContent and return (image_bytes_list, text_notes).

    Raises ValueError when the response contains no image parts.
    """
    generation_config: dict[str, Any] = {"responseModalities": ["IMAGE"]}
    if aspect_ratio:
        generation_config["imageConfig"] = {"aspectRatio": aspect_ratio}
    r = httpx.post(
        GEMINI_ENDPOINT.format(model=model),
        headers={"x-goog-api-key": key},
        json={
            "contents": [{"parts": parts}],
            "generationConfig": generation_config,
        },
        timeout=120,
    )
    r.raise_for_status()
    candidates = r.json().get("candidates") or []
    out_parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    images: list[bytes] = []
    notes: list[str] = []
    for part in out_parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            images.append(base64.b64decode(inline["data"]))
        elif part.get("text"):
            notes.append(part["text"])
    if not images:
        raise ValueError(f"Gemini returned no image (response text: {' '.join(notes)[:300]})")
    return images, "\n".join(notes)


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    return f"{exc.response.status_code}: {exc.response.text[:300]}"


def _backend_failure(exc: Exception, provider: str) -> str:
    """One-line, user-meaningful reason an image backend failed.

    Routed through the shared classifier so a managed-gateway refusal reads the
    same here as it does in chat. Without this the tool output carried the raw
    body — an unrouted `olano-image` group landed in front of the model as a
    clipped gateway JSON blob, an unreadable error dump.

    The classifier is given the response BODY for HTTP errors: `str(exc)` is
    only "Client error '400 Bad Request' for url …", which carries none of the
    detail the rules match on.
    """
    text = _http_error_detail(exc) if isinstance(exc, httpx.HTTPStatusError) else None
    try:
        from olano.core.actionable_errors import classify_exception

        classified = classify_exception(exc, provider, text=text)
    except Exception:  # pragma: no cover - defensive
        classified = None
    if classified is not None:
        return classified.message
    return text if text is not None else (str(exc)[:300] or type(exc).__name__)


def _available_image_backends() -> list[tuple[str, str]]:
    """Every backend this box can actually bill, in preference order."""
    backends: list[tuple[str, str]] = []
    if _olano_image_available():
        backends.append(("olano", OLANO_IMAGE_MODEL))
    if _google_api_key():
        backends.append(("gemini", DEFAULT_GEMINI_IMAGE_MODEL))
    if get_credential("OPENAI_API_KEY", ""):
        backends.append(("openai", DEFAULT_OPENAI_IMAGE_MODEL))
    return backends


def _describe_backends(backends: list[tuple[str, str]]) -> str:
    labels = {
        "olano": "Olano-managed, billed in olano credits",
        "gemini": "Google key configured",
        "openai": "OpenAI key configured",
    }
    return ", ".join(f"{p}:{n} ({labels.get(p, p)})" for p, n in backends)


def _image_candidates(model: str) -> list[tuple[str, str]] | str:
    """Ordered backends for one ``image_generate`` call.

    A **pinned** model — per-call ``model=`` or the operator's
    ``LIONCLAW_IMAGE_MODEL`` — resolves to exactly itself: no substitution, so
    a broken pin reports itself instead of quietly billing somewhere else.
    Only an **unpinned** call fans out, so a backend that is out of credits or
    rate-limited doesn't sink the whole request.
    """
    resolved = _resolve_image_model(model)
    if isinstance(resolved, str):
        # Name the backends that WOULD work, so the agent can retry with one
        # instead of concluding image generation is unavailable. (The shared
        # resolver's message stays as-is — it also serves the avatar
        # generators, which have no `model=` retry to offer.)
        alts = _available_image_backends()
        if alts and resolved.startswith("[missing-credentials]"):
            return (
                f"{resolved} Image backends available here: {_describe_backends(alts)}. "
                'Retry image_generate with model="<one of those>".'
            )
        return resolved
    if model.strip() or _pinned_image_model():
        return [resolved]
    return [resolved] + [b for b in _available_image_backends() if b[0] != resolved[0]]


def _generate_once(
    provider: str,
    name: str,
    prompt: str,
    aspect_ratio: str,
    quality: str,
    n: int,
) -> tuple[list[bytes], str, float | None]:
    """Run one backend; returns ``(images, notes, credits)`` where ``credits``
    is the gateway's per-call charge (managed calls only, from the response
    headers). Raises on any failure so the caller can fall through."""
    if provider == "gemini":
        images, notes = _gemini_generate_images(
            name, [{"text": prompt}], aspect_ratio, _google_api_key()
        )
        return images, notes, None

    if provider == "olano":
        # OpenAI-images-compatible surface of the Olano model gateway: same
        # request shape, per-deployment key, charged in credits.
        from olano.deployment.gateway_key import ensure_gateway_key, openai_base_url

        api_key = ensure_gateway_key() or ""
        endpoint = f"{openai_base_url()}/images/generations"
    else:
        api_key = get_credential("OPENAI_API_KEY", "")
        endpoint = "https://api.openai.com/v1/images/generations"

    r = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": name,
            "prompt": prompt,
            "n": max(1, min(n, 4)),
            "size": _OPENAI_SIZE_BY_RATIO.get(aspect_ratio, "1024x1024"),
            "quality": quality,
        },
        timeout=180,
    )
    r.raise_for_status()
    from olano.analytics.usage_recorder import credits_from_headers

    credits = credits_from_headers(r.headers, managed=provider == "olano")
    data = r.json().get("data") or []
    images = []
    notes = ""
    for item in data:
        if item.get("b64_json"):
            images.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            images.append(httpx.get(item["url"], timeout=60).content)
    if data and data[0].get("revised_prompt", prompt) != prompt:
        notes = f"Revised prompt: {data[0]['revised_prompt']}"
    if not images:
        raise ValueError(f"{provider} returned no image data")
    return images, notes, credits


@tool
def image_generate(
    prompt: Annotated[str, "Text description of the image to generate"],
    model: Annotated[
        str,
        "provider:model, e.g. 'gemini:gemini-3.1-flash-image' (Nano Banana 2), "
        "'gemini:gemini-3-pro-image', 'gemini:gemini-2.5-flash-image', "
        "'openai:gpt-image-2.5-flare', 'olano:olano-image'. Empty = auto: the Olano "
        "image service on a managed deployment, else Gemini Nano Banana / "
        "OpenAI by key — and a backend that fails falls through to the next "
        "one. A model you name here is used as-is, with no fallback.",
    ] = "",
    aspect_ratio: Annotated[str, "Aspect ratio: 1:1, 16:9, 9:16, 4:3, 3:4, 21:9"] = "1:1",
    quality: Annotated[str, "OpenAI models only: low, medium, or high"] = "medium",
    n: Annotated[int, "Number of images, 1-4 (OpenAI only; Gemini returns 1)"] = 1,
) -> str:
    """Create NEW image(s) from a text prompt and save them to the workspace.

    This tool only *generates* pictures. It CANNOT read, describe, analyze or
    answer questions about an existing image, and passing one an instruction
    like "identify what is in the attached photo" does nothing useful — use
    `image_describe` to look at an image, `image_edit` / `nanobanana_image_edit`
    to modify one.

    Runs on the Olano image service (olano credits) on a managed deployment,
    otherwise Gemini Nano Banana or OpenAI GPT Image depending on which key is
    configured; with no `model` a failing backend falls through to the next.
    """
    resolved = _image_candidates(model)
    if isinstance(resolved, str):
        return resolved

    attempts: list[tuple[str, str]] = []
    for provider, name in resolved:
        try:
            images, notes, credits = _generate_once(
                provider, name, prompt, aspect_ratio, quality, n
            )
        except Exception as exc:
            attempts.append((f"{provider}:{name}", _backend_failure(exc, provider)))
            continue
        paths = _save_images(images, prompt)
        result = _format_result(paths, f"{provider}:{name}", notes, credits)
        if attempts:
            failed = "\n".join(f"  - {label}: {why}" for label, why in attempts)
            result += f"\n\n[image-fallback] Earlier backend(s) failed:\n{failed}"
        return result

    if len(attempts) == 1:
        label, why = attempts[0]
        lines = [f"[tool-error] image_generate: {label} failed — {why}"]
    else:
        lines = ["[tool-error] image_generate: every configured image backend failed."]
        lines += [f"  - {label}: {why}" for label, why in attempts]
    tried = {label.partition(":")[0] for label, _ in attempts}
    alts = [b for b in _available_image_backends() if b[0] not in tried]
    if alts:
        lines.append(
            f'Still available here: {_describe_backends(alts)}. Retry with model="<one of those>".'
        )
    return "\n".join(lines)


@tool
def image_edit(
    image: Annotated[str, "Image to edit: local file path (workspace) or http(s) URL"],
    prompt: Annotated[str, "Description of the desired edit"],
    mask: Annotated[
        str, "Optional mask image (path or URL); transparent areas mark the region to change"
    ] = "",
    model: Annotated[
        str,
        "Empty = auto (the Olano image service on a managed deployment, else "
        "OpenAI). Or pin one: 'openai:gpt-image-2.5-flare', 'olano:olano-image'.",
    ] = "",
) -> str:
    """Edit an image with mask-based inpainting, saving the result to the workspace.

    Runs on the **Olano image service by default** on a managed deployment —
    billed in credits, no provider key — and on the customer's OpenAI account
    otherwise, the same resolution ``image_generate`` uses. Gemini does not
    serve this request shape; for conversational / reference-based editing with
    Nano Banana use ``nanobanana_image_edit`` instead.
    """
    backend = openai_images_backend(model)
    if isinstance(backend, str):
        return backend
    provider, model_name, api_key, base_url = backend

    try:
        image_bytes, mime = _load_image_bytes(image)
        files: dict[str, Any] = {
            "image": ("image.png", image_bytes, mime),
            "model": (None, model_name),
            "prompt": (None, prompt),
            "n": (None, "1"),
        }
        if mask:
            mask_bytes, mask_mime = _load_image_bytes(mask)
            files["mask"] = ("mask.png", mask_bytes, mask_mime)

        r = httpx.post(
            f"{base_url}/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            timeout=180,
        )
        r.raise_for_status()
        from olano.analytics.usage_recorder import credits_from_headers

        credits = credits_from_headers(r.headers, managed=provider == "olano")
        data = r.json().get("data") or []
        images: list[bytes] = []
        for item in data:
            if item.get("b64_json"):
                images.append(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                images.append(httpx.get(item["url"], timeout=60).content)
        if not images:
            return f"[tool-error] image_edit: {provider} returned no image data."
        paths = _save_images(images, prompt)
        return _format_result(paths, f"{provider}:{model_name}", credits=credits)
    except Exception as exc:
        # image_edit rides the SAME olano-image gateway entry as generation, so
        # an unrouted group surfaces here too — classify it rather than dumping
        # the provider's raw body.
        return f"[tool-error] image_edit: {_backend_failure(exc, provider)}"


@tool
def nanobanana_create_image(
    prompt: Annotated[str, "Text description of the image to generate"],
    model: Annotated[
        str,
        "Gemini image model: gemini-3.1-flash-image (Nano Banana 2, default), "
        "gemini-3-pro-image, gemini-2.5-flash-image",
    ] = DEFAULT_GEMINI_IMAGE_MODEL,
    aspect_ratio: Annotated[str, "Aspect ratio: 1:1, 16:9, 9:16, 4:3, 3:4, 21:9"] = "1:1",
) -> str:
    """Create NEW image(s) from a text prompt with Gemini Nano Banana.

    Generation only — it cannot read or describe an existing image (use
    `image_describe` for that). The result is saved to the workspace. To edit
    or combine existing images, use nanobanana_image_edit; for OpenAI GPT Image
    or automatic provider selection, use image_generate.
    """
    key = _google_api_key()
    if not key:
        return (
            "[missing-credentials] GOOGLE_API_KEY / GEMINI_API_KEY not set. "
            "Nano Banana generation needs a Google AI key; for OpenAI-only "
            "setups use image_generate instead."
        )

    try:
        images, notes = _gemini_generate_images(
            model or DEFAULT_GEMINI_IMAGE_MODEL, [{"text": prompt}], aspect_ratio, key
        )
        paths = _save_images(images, prompt)
        return _format_result(
            paths, f"gemini:{model or DEFAULT_GEMINI_IMAGE_MODEL} (Nano Banana)", notes
        )
    except httpx.HTTPStatusError as exc:
        return f"[tool-error] nanobanana_create_image: {_http_error_detail(exc)}"
    except Exception as exc:
        return f"[tool-error] nanobanana_create_image: {exc}"


@tool
def nanobanana_image_edit(
    instruction: Annotated[
        str,
        "Natural-language edit instruction, e.g. 'make the sky a sunset' or "
        "'combine these two products into one lifestyle shot'",
    ],
    images: Annotated[
        str,
        "Input/reference image(s): local file paths or http(s) URLs, "
        "comma- or newline-separated (1-8 images)",
    ],
    model: Annotated[
        str,
        "Gemini image model: gemini-3.1-flash-image (Nano Banana 2, default), "
        "gemini-3-pro-image, gemini-2.5-flash-image",
    ] = DEFAULT_GEMINI_IMAGE_MODEL,
    aspect_ratio: Annotated[
        str, "Optional output aspect ratio (1:1, 16:9, ...); empty keeps the input's ratio"
    ] = "",
) -> str:
    """Edit or combine image(s) conversationally with Gemini Nano Banana.

    Takes one or more input/reference images plus a text instruction — no
    mask needed. The result is saved to the workspace.
    """
    key = _google_api_key()
    if not key:
        return (
            "[missing-credentials] GOOGLE_API_KEY / GEMINI_API_KEY not set. "
            "Nano Banana editing needs a Google AI key; for OpenAI-only setups "
            "use image_edit instead."
        )

    sources = [s.strip() for s in re.split(r"[,\n]", images) if s.strip()][:8]
    if not sources:
        return "[tool-error] nanobanana_image_edit: no input images provided."

    try:
        parts: list[dict[str, Any]] = [{"text": instruction}]
        for src in sources:
            try:
                data, mime = _load_image_bytes(src)
            except Exception as exc:
                return f"[tool-error] nanobanana_image_edit: could not load {src!r}: {exc}"
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )
        out_images, notes = _gemini_generate_images(
            model or DEFAULT_GEMINI_IMAGE_MODEL, parts, aspect_ratio, key
        )
        paths = _save_images(out_images, instruction)
        return _format_result(
            paths, f"gemini:{model or DEFAULT_GEMINI_IMAGE_MODEL} (Nano Banana)", notes
        )
    except httpx.HTTPStatusError as exc:
        return f"[tool-error] nanobanana_image_edit: {_http_error_detail(exc)}"
    except Exception as exc:
        return f"[tool-error] nanobanana_image_edit: {exc}"


@tool
def screenshot_url(
    url: Annotated[str, "URL of the webpage to screenshot"],
    width: Annotated[int, "Viewport width in pixels"] = 1280,
    height: Annotated[int, "Viewport height in pixels"] = 800,
) -> str:
    """Take a screenshot of a webpage. Returns image URL or base64."""
    api_key = get_credential("SCREENSHOTONE_API_KEY", "")
    if not api_key:
        return (
            f"[missing-credentials] No SCREENSHOTONE_API_KEY configured. "
            f"Use browser_navigate + browser_screenshot for in-session screenshots of {url}"
        )
    try:
        r = httpx.get(
            "https://api.screenshotone.com/take",
            params={
                "access_key": api_key,
                "url": url,
                "viewport_width": width,
                "viewport_height": height,
                "format": "jpg",
                "block_ads": "true",
            },
            timeout=30,
        )
        r.raise_for_status()
        return (
            f"Screenshot taken for {url} ({len(r.content)} bytes). "
            "Use browser_screenshot for in-session screenshots."
        )
    except Exception as exc:
        return f"[tool-error] screenshot_url: {exc}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
