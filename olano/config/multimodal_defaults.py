"""Multimodal default tools — Olano services by default, switchable to BYOK.

Every agent should be able to *read* an image, *make* an image and *listen*
out of the box, without an operator hand-listing five tool names in
``agent.json5 → tools``. On a **managed deployment that is the default and it
needs no configuration at all**: the Olano service provides those capabilities
through the model gateway, billed in olano credits, so ``image_describe`` runs
on ``olano:turbo``, ``image_generate`` / ``image_edit`` on ``olano-image``, and
transcription on ``olano-stt`` with no provider key on the box. *Speaking* is
the deliberate exception: TTS defaults off everywhere (voice replies change
the agent's outward behaviour and cost money per reply), so ``text_to_speech``
appears only after someone picks a TTS provider under Config → Speech — on a
managed box that's one click on ``olano``, billed in credits like the rest.

**That default holds whether or not the box also has provider keys.** A managed
deployment that happens to carry ``OPENAI_API_KEY`` (for a model, for another
toolkit) still draws on credits: a key present for some other purpose is not an
instruction to start billing the customer's own account for every image an agent
makes. Moving a capability to BYOK is a deliberate, standing choice — one knob
each, resolved like any other credential so it can be set globally (env, vault)
or for a single agent (``config.env``):

* ``LIONCLAW_IMAGE_MODEL`` — e.g. ``openai:gpt-image-2.5-flare``, ``gemini:…``
* ``LIONCLAW_VISION_MODEL`` — e.g. ``anthropic:claude-haiku-4-5-20251001``
* ``speech.stt.provider`` / ``speech.tts.provider`` — dashboard Config → Speech

An unmanaged install has no gateway to bill through, so there BYOK is not a
switch but the only route, and the keys are read in provider order as before.

What stays conditional is the case where **neither** exists. Unlike the
universal defaults in ``ops_defaults.UNIVERSAL_AGENT_TOOL_NAMES`` — pure local
plumbing that always works — each of these calls a paid remote service, and
binding one with nothing behind it buys nothing while costing something real: a
tool the model can see, plan around, and call, only to get
``[missing-credentials]`` back. So an unmanaged, keyless box never hears that
image generation exists.

The gate is deliberately NOT a hardcoded key list. Each capability asks the code
that would actually run it, so the grant and the call can never disagree about
what counts:

===================== ==================================================
``image_describe``    :func:`olano.tools.vision.vision_available` — the Olano
                      tier on a managed box, else a credential for whatever
                      ``LIONCLAW_VISION_MODEL`` names (default: Anthropic).
``image_generate``    :func:`olano.tools.image_tools.image_generation_available`
                      — the Olano image service on a managed box, else (or when
                      ``LIONCLAW_IMAGE_MODEL`` pins one) a Gemini / OpenAI key.
``image_edit``        :func:`olano.tools.image_tools.image_edit_available` — the
                      same, minus Gemini: ``/images/edits`` is an OpenAI-shaped
                      endpoint Gemini does not serve.
``text_to_speech``    :func:`olano.speech.tts_available` — the ``speech.tts``
                      provider, which **defaults to** ``disabled`` everywhere
                      (every install opts in under Config → Speech: managed
                      boxes pick ``olano`` to run on credits, and holding an
                      API key is not consent to ship audio off the machine).
``audio_to_text``     :func:`olano.speech.stt_available` — the ``speech.stt``
                      provider, which **defaults to** ``olano`` on a managed
                      box and to ``disabled`` elsewhere.
===================== ==================================================

``pdf_to_text`` / ``document_to_text`` are the fifth capability the operator
asked for and are **not** here: they parse locally, need no credential at all,
and are already unconditional universal defaults (``ops_defaults``).

Resolution runs inside :func:`olano.tools._credentials.agent_runtime_context`
bound to the agent being built, so a key living only in that agent's
``config.env`` or in its per-agent vault override counts — two agents on one box
can legitimately disagree about whether image generation exists.

Two callers, one condition:

* ``core/builder._build_extra_tools`` binds the available names (the imperative
  side — what the model is actually handed),
* ``config/internal_tools`` mirrors the same predicate so the dashboard's
  internal-tools browser reports the true per-agent state, with a ``requires``
  note telling an unmanaged operator what to set.

Keep those two in sync by keeping both on :func:`available_multimodal_tool_names`
/ :func:`capability_available` rather than re-deriving the rule.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# ── The tools ──────────────────────────────────────────────────────────────
#: Order is display/binding order. Kept flat (not a dict) so the sequence the
#: model sees is stable across Python versions and readable in one glance.
MULTIMODAL_TOOL_NAMES: list[str] = [
    "image_describe",  # image → text (vision LLM)
    "image_generate",  # text → image
    "image_edit",  # image + instruction → image
    "text_to_speech",  # TTS
    "audio_to_text",  # STT
]


def _vision_available() -> bool:
    from olano.tools.vision import vision_available

    return vision_available()


def _image_generation_available() -> bool:
    from olano.tools.image_tools import image_generation_available

    return image_generation_available()


def _image_edit_available() -> bool:
    # Nearly the same question as generation, but not identical: /images/edits
    # is an OpenAI-shaped endpoint Gemini does not serve, so a box whose only
    # image key is a Google one can generate but not edit. The Olano gateway
    # serves the shape, so a managed box edits on credits like it generates.
    from olano.tools.image_tools import image_edit_available

    return image_edit_available()


def _tts_available() -> bool:
    from olano.speech import tts_available

    return tts_available()


def _stt_available() -> bool:
    from olano.speech import stt_available

    return stt_available()


#: tool name → "is its provider usable right now, for the bound agent".
#: Every probe is credential-only (no network) and must never raise — a vault
#: hiccup has to read as "not available", not as a failed agent build.
_AVAILABILITY: dict[str, Callable[[], bool]] = {
    "image_describe": _vision_available,
    "image_generate": _image_generation_available,
    "image_edit": _image_edit_available,
    "text_to_speech": _tts_available,
    "audio_to_text": _stt_available,
}

#: One-line "what it does", for the dashboard internal-tools browser.
MULTIMODAL_TOOL_DESCRIPTIONS: dict[str, str] = {
    "image_describe": (
        "Read an image (screenshot, photo, chart, scanned page) and answer "
        "questions about it with a vision model."
    ),
    "image_generate": "Generate an image from a text prompt and save it to the workspace.",
    "image_edit": "Edit an existing image (local path or URL) from a text instruction.",
    "text_to_speech": "Speak text out loud — synthesize an audio file from text.",
    "audio_to_text": "Transcribe a voice note or audio file to text.",
}

#: Human label for the catalog's `requires` note: what an operator has to do to
#: turn the row on. Says the key names out loud — the whole point of reporting
#: an unavailable tool instead of hiding it is that the operator learns how to
#: get it.
_DEFAULTS_TAIL = " + default internal tools, or an explicit tools: entry"
MULTIMODAL_TOOL_REQUIRES: dict[str, str] = {
    "image_describe": (
        "olano credits (the default — connect this deployment to cloud.olano.ai "
        "and image-to-text runs on the Olano service with no key), or your own "
        "key for the vision model. A managed box stays on credits even with a "
        "key set; point it at your own account with LIONCLAW_VISION_MODEL (e.g. "
        "anthropic:claude-haiku-4-5-20251001, which is also the unmanaged "
        "default)" + _DEFAULTS_TAIL
    ),
    "image_generate": (
        "olano credits (the default — connect this deployment to cloud.olano.ai "
        "and image generation runs on the Olano image service with no key), or "
        "your own GOOGLE_API_KEY / GEMINI_API_KEY or OPENAI_API_KEY. A managed "
        "box stays on credits even with a key set; point it at your own account "
        "with LIONCLAW_IMAGE_MODEL (e.g. openai:gpt-image-2.5-flare)" + _DEFAULTS_TAIL
    ),
    "text_to_speech": (
        "a text-to-speech provider chosen in Config → Speech "
        "(speech.tts.provider) — voice replies are off by default on every "
        "install. On a managed deployment pick the Olano speech service to run "
        "on olano credits with no key; elsewhere pick a provider and set its "
        "API key" + _DEFAULTS_TAIL
    ),
    "audio_to_text": (
        "olano credits (the default on a managed deployment — the Olano speech "
        "service, no key needed), or on an unmanaged install: a speech-to-text "
        "provider chosen in Config → Speech (speech.stt.provider) plus its API "
        "key" + _DEFAULTS_TAIL
    ),
}
#: image_edit rides the image_generate providers minus Gemini, which does not
#: serve the /images/edits shape.
MULTIMODAL_TOOL_REQUIRES["image_edit"] = (
    "olano credits (the default — connect this deployment to cloud.olano.ai "
    "and image editing runs on the Olano image service with no key), or your "
    "own OPENAI_API_KEY. A Gemini key alone is not enough here: /images/edits "
    "is an OpenAI-shaped endpoint Gemini does not serve (use "
    "nanobanana_image_edit for Gemini editing)" + _DEFAULTS_TAIL
)


def _probe(name: str) -> bool:
    """Run one probe. Assumes the agent's credential context is already bound."""
    probe = _AVAILABILITY.get(name)
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        return False


def capability_available(name: str, config: AgentConfig) -> bool:
    """True when ``name``'s provider is usable for ``config``'s agent.

    Credential-only: no network call, no model instantiation. Bound to the
    agent's own identity + ``config.env`` overlay so per-agent keys count.
    Never raises — an unreadable vault, a malformed ``speech`` block or a
    toolkit that fails to import all read as "not available" rather than
    breaking the agent build.
    """
    try:
        from olano.tools._credentials import agent_runtime_context

        with agent_runtime_context(config.name, config.env):
            return _probe(name)
    except Exception:
        return False


def available_multimodal_tool_names(config: AgentConfig) -> list[str]:
    """The multimodal names to grant ``config`` implicitly, keys permitting.

    Empty when ``default_internal_tools`` is off — that knob turns off every
    implicit grant, and an agent that wants one of these back lists it in
    ``tools:`` (where it binds unconditionally, exactly like any other explicit
    tool: the operator named it, so they get it and its ``[missing-credentials]``
    message if the key is absent).
    """
    if not config.default_internal_tools:
        return []
    explicit = set(config.tools or [])
    disabled = set(config.disabled_tools or [])
    candidates = [n for n in MULTIMODAL_TOOL_NAMES if n not in explicit and n not in disabled]
    if not candidates:
        return []
    try:
        from olano.tools._credentials import agent_runtime_context

        # One context for the whole sweep, not one per probe: the five
        # capabilities share a vault memo, so a per-agent override is decrypted
        # once instead of five times per agent build.
        with agent_runtime_context(config.name, config.env):
            return [name for name in candidates if _probe(name)]
    except Exception:
        return []
