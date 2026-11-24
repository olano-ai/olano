"""Image description tool using a vision-capable LLM.

Model selection and fallback
----------------------------
`image_describe` is the ONE image→text tool. It picks its own model unless the
caller names one:

    model= argument                (per call, wins)
    → LIONCLAW_VISION_MODEL        (the operator's standing choice, resolved per
                                    agent through config.env / vault / env)
    → olano:vision                 (managed boxes — a gateway group guaranteed
                                    to be multimodal, billed in olano credits,
                                    no provider key needed)
    → the BYOK providers whose key is actually configured, in
      :data:`_BYOK_VISION_OPTIONS` order

The split that matters is **pinned vs unpinned**, and it is the same rule the
image tools use:

- **Pinned** — a `model=` argument or `LIONCLAW_VISION_MODEL` — is honoured
  exactly, with no substitution. A missing key fails before any network call;
  a failed call reports why. Silently falling back would mean the operator
  never learns their pin is broken (and, on a managed box, would move billing
  they chose to place on their own account back onto credits).
- **Unpinned** — the whole chain is tried in order. A provider that is out of
  credits, rate-limited, or holding a stale key falls through to the next one,
  because "this box has a working OpenAI key but the vision default is
  Anthropic" must never reach the user as "I can't look at images".

Either way, a failure names every *other* vision model configured here, so the
agent can retry deliberately with `model="…"` — every other model that is
actually *working*, which is not the same question as which are configured. See
the failure memo below: a model that just hard-failed is still named, but never
recommended.

Note for whoever extends this: there is no image *generation* fallback for
vision. `image_generate` cannot read an image, and an agent that tries it as a
vision workaround is following a dead end — the tool descriptions in
`image_tools.py` say so explicitly, because one did.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_VISION_MODEL_ENV = "LIONCLAW_VISION_MODEL"
_DEFAULT_VISION_MODEL = "anthropic:claude-haiku-4-5-20251001"

# `olano:vision`, NOT a chat tier. The tiers are routed on price/latency — the
# 2026-08 re-route put a text-only backend behind turbo — so "the managed tier
# probably takes images" is an assumption the cloud can invalidate at any time
# without anyone noticing until a user sends a photo. `olano-vision` is a
# gateway group whose membership condition is "accepts image parts", so asking
# for it is the guarantee (models/olano_gateway.py).
_OLANO_VISION_MODEL = "olano:vision"

_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass(frozen=True)
class _VisionOption:
    """A BYOK vision model plus the credential names that unlock it."""

    model: str
    env_vars: tuple[str, ...]
    label: str


# Cheap, vision-capable model per BYOK provider, in fallback order. Anthropic
# leads because it is the documented unmanaged default; the rest follow so a
# box with any one key configured can still see.
_BYOK_VISION_OPTIONS: tuple[_VisionOption, ...] = (
    _VisionOption(_DEFAULT_VISION_MODEL, ("ANTHROPIC_API_KEY",), "Anthropic"),
    _VisionOption("openai:gpt-5.4-mini", ("OPENAI_API_KEY",), "OpenAI"),
    _VisionOption(
        "google_genai:gemini-2.5-flash",
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "Google Gemini",
    ),
)

# Providers whose key can arrive under more than one name.
_PROVIDER_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


# ── Observed-failure memo ────────────────────────────────────────────────────
# Availability in this module is a CREDENTIAL question (`_olano_vision_available`
# and friends), and a credential cannot tell you whether the model on the other
# end actually works. When it doesn't, every surface built on that answer keeps
# offering the model anyway — including the retry hint printed by the failure
# itself. That is not hypothetical: a managed box whose gateway served no
# `olano-vision` group told the agent "Vision models that ARE available here:
# olano:vision — retry with one of those" immediately after `olano:vision` had
# hard-failed, so the agent bounced between the same two dead ends and gave up.
#
# A hard failure is therefore remembered briefly, and the model is annotated as
# failing wherever it is listed. Three deliberate limits:
#
# - The chain still TRIES it. This memo only words messages; it never removes a
#   rung. A cloud-side fix then heals the box on the next call, with no restart
#   and no cache to invalidate.
# - Only NON-RETRYABLE classified failures are remembered. A rate limit or a
#   timeout says nothing about the next call, and marking those "failing" would
#   turn a blip into a lie that outlives it.
# - Process-local and short-lived. This is a hint for phrasing, never a source of
#   truth about entitlement or availability.
_FAILURE_TTL_ENV = "LIONCLAW_VISION_FAILURE_TTL"
_DEFAULT_FAILURE_TTL = 600.0

# model string (lowercased) → (monotonic deadline, short reason)
_failure_memo: dict[str, tuple[float, str]] = {}


def _failure_ttl() -> float:
    """Memo window in seconds. ``0`` disables the memo entirely."""
    raw = (os.environ.get(_FAILURE_TTL_ENV) or "").strip()
    if not raw:
        return _DEFAULT_FAILURE_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_FAILURE_TTL


def _brief(text: str, limit: int = 90) -> str:
    """First sentence of *text*, clipped — enough to tell failures apart inline.

    Clipped on a word boundary: this is rendered mid-sentence inside a list of
    models, where a break mid-word ("…is not available at the model gateway
    right now (it ser…") reads as corruption rather than as a summary.
    """
    first = text.strip().split(". ")[0].strip()
    if len(first) <= limit:
        return first
    cut = first[:limit]
    spaced = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return spaced.rstrip(" ,;:(") + "…"


def _note_failure(model_str: str, reason: str) -> None:
    ttl = _failure_ttl()
    if ttl > 0:
        _failure_memo[model_str.strip().lower()] = (time.monotonic() + ttl, _brief(reason))


def _clear_failure(model_str: str) -> None:
    """Forget a model's failure — it just answered."""
    _failure_memo.pop(model_str.strip().lower(), None)


def reset_failure_memo() -> None:
    """Forget every remembered failure.

    Public because the memo is process-global: the test suite resets it between
    tests (``tests/conftest.py``) so one test's simulated outage cannot reword
    another's output.
    """
    _failure_memo.clear()


def _recent_failure(model_str: str) -> str | None:
    """Why *model_str* recently hard-failed, or ``None`` if it hasn't (lately)."""
    key = model_str.strip().lower()
    entry = _failure_memo.get(key)
    if entry is None:
        return None
    expires, reason = entry
    if time.monotonic() >= expires:
        _failure_memo.pop(key, None)
        return None
    return reason


def _olano_vision_available() -> bool:
    """Managed box entitled to bill image→text through the model gateway.

    Shares the gateway CREDENTIAL check with the image toolkit
    (``gateway_media_reachable``) but applies the VISION entitlement, not the
    image one: a plan can withhold ``olano_vision`` while keeping image
    generation, and vice-versa.

    Credentials and entitlement are necessary, not sufficient: both said yes
    throughout the outage where the gateway served no ``olano-vision`` group at
    all. So a recent probe that actually read the gateway's model list and did
    not find the group also counts — the chain then falls straight to BYOK
    instead of spending a call to be told "Invalid model name". Advisory in the
    safe direction only: no probe, a failed probe, or a stale one all leave the
    rung in place (see ``gateway_probe.known_missing``), and a group published
    in the cloud starts working here within the probe's window, no restart.
    """
    try:
        from olano.deployment import enforcement, gateway_probe
        from olano.models.olano_gateway import OLANO_VISION_MODEL
        from olano.tools.image_tools import gateway_media_reachable

        if not (gateway_media_reachable() and enforcement.service_entitled("vision")):
            return False
        return not gateway_probe.known_missing(OLANO_VISION_MODEL)
    except Exception:  # pragma: no cover - defensive
        return False


def _pinned_vision_model() -> str:
    """The operator's standing choice, or "" when they made none.

    Precedence: the dashboard **media.vision** selection (Config → Media &
    search) first, then the legacy ``LIONCLAW_VISION_MODEL`` env/vault pin as the
    fallback — so an existing install keeps working while the dashboard can
    override it. The dashboard's ``"olano"`` sentinel resolves to the managed
    vision group. Read through ``get_credential`` so the env fallback stays
    per-agent (config.env / vault), like every other credential in olano.
    """
    try:
        from olano.config.system_settings import load_media_settings

        choice = (load_media_settings().vision or "").strip()
    except Exception:  # pragma: no cover - defensive
        choice = ""
    if choice:
        return _OLANO_VISION_MODEL if choice.lower() == "olano" else choice
    return (get_credential(_VISION_MODEL_ENV, "") or "").strip()


def _default_vision_model() -> str:
    """The first rung of the unpinned chain."""
    return _OLANO_VISION_MODEL if _olano_vision_available() else _DEFAULT_VISION_MODEL


def _vision_model_string() -> str:
    """The model image→text will actually run on for this turn's agent.

    The head of the chain: the operator's pin when there is one, else the first
    rung that is actually reachable. Kept as its own name because callers like
    ``config/multimodal_defaults`` ask "what does this box use?" — a question
    with exactly one answer even when fallback rungs sit behind it.
    """
    chain = _vision_chain()
    return chain[0] if chain else _default_vision_model()


def _configured_options() -> list[_VisionOption]:
    """BYOK vision options whose API key resolves for the current agent."""
    return [o for o in _BYOK_VISION_OPTIONS if any(get_credential(v, "") for v in o.env_vars)]


def _vision_chain(model: str = "") -> list[str]:
    """Ordered model candidates. A pinned model is the only candidate."""
    pinned = (model or "").strip() or _pinned_vision_model()
    if pinned:
        return [pinned]

    chain: list[str] = []
    if _olano_vision_available():
        chain.append(_OLANO_VISION_MODEL)
    chain.extend(o.model for o in _configured_options())

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in chain:
        if candidate.lower() not in seen:
            seen.add(candidate.lower())
            ordered.append(candidate)
    return ordered


def _missing_credential(model_str: str) -> str | None:
    """Credential name(s) *model_str* needs and doesn't have, else ``None``.

    ``None`` also covers "we can't tell" (unknown provider, keyless provider
    like ollama, the offline ``fake`` model) — the call then runs and fails
    with the provider's own error, which beats refusing on a guess.
    """
    provider = model_str.partition(":")[0] if ":" in model_str else ""
    if not provider or provider == "fake":
        return None
    if provider == "olano":
        if _olano_vision_available():
            return None
        return "a deployment connected to cloud.olano.ai (olano credits)"

    names = _PROVIDER_ENV_ALIASES.get(provider)
    if names is None:
        try:
            from olano.models import PROVIDERS

            info = PROVIDERS.get(provider)
        except Exception:  # pragma: no cover - defensive
            info = None
        if info is None or not info.env_var:
            return None
        names = (info.env_var,)

    if any(get_credential(n, "") for n in names):
        return None
    return " / ".join(names)


def vision_available() -> bool:
    """True when image→text has a usable credential for this turn's agent.

    Credential-only — it never builds the model or touches the network, because
    it runs on the agent-build path (``config/multimodal_defaults``) to decide
    whether ``image_describe`` is worth binding at all. A pinned model answers
    for itself; an unpinned one counts if ANY rung of the chain is reachable.
    """
    try:
        chain = _vision_chain()
        return bool(chain) and _missing_credential(chain[0]) is None
    except Exception:
        return False


def _alternatives(exclude: Iterable[str]) -> tuple[list[str], list[str]]:
    """Vision models configured here, minus *exclude*, split ``(healthy, failing)``.

    "Failing" means a hard failure was observed inside the memo window. Those are
    still named — a model this box is configured for is worth knowing about, and
    hiding it would make a fleet-wide outage look like "nothing is configured" —
    but they are kept out of the "retry with one of these" recommendation, which
    is the loop this split exists to break.
    """
    skip = {m.strip().lower() for m in exclude}
    healthy: list[str] = []
    failing: list[str] = []

    def add(model: str, label: str) -> None:
        if model.strip().lower() in skip:
            return
        reason = _recent_failure(model)
        if reason is None:
            healthy.append(f"{model} ({label})")
        else:
            failing.append(f"{model} ({label}; failing: {reason})")

    if _olano_vision_available():
        add(_OLANO_VISION_MODEL, "Olano-managed, billed in olano credits")
    for option in _configured_options():
        add(option.model, f"{option.label} key configured")
    return healthy, failing


def _retry_hint(healthy: list[str], failing: list[str]) -> str:
    """The "what to do instead" sentence, honest about what is actually usable."""
    if healthy:
        hint = (
            f" Vision models that ARE available here: {', '.join(healthy)}. "
            'Retry image_describe with model="<one of those>" to use one.'
        )
        if failing:
            hint += f" Also configured but failing right now: {', '.join(failing)}."
        return hint
    if failing:
        return (
            " Every other vision model configured here is failing right now: "
            f"{', '.join(failing)}. Retrying with one of them is unlikely to help."
        )
    return " No other vision-capable model is configured either."


def _build_vision_model(model_str: str) -> BaseChatModel:
    # build_model (not init_chat_model): it resolves olano:* groups through the
    # gateway and falls through to init_chat_model for provider:model strings.
    # allow_utility=True: this is the product's own vision call, the one caller
    # permitted to build the olano:vision utility group (build_model rejects it
    # as an agent brain everywhere else).
    from olano.models import build_model

    return build_model(model_str, allow_utility=True)


@dataclass(frozen=True)
class _Attempt:
    """One candidate's outcome.

    ``text`` is the description on success and the user-meaningful failure
    reason otherwise. ``code`` / ``retryable`` carry the
    :mod:`olano.core.actionable_errors` classification when there was one, which
    is what lets the caller tell "this model is broken" (worth remembering) from
    "the API was busy" (emphatically not).
    """

    model: str
    ok: bool
    text: str
    code: str = ""
    retryable: bool = False


def _failed_attempt(exc: Exception, model_str: str) -> _Attempt:
    """Classify a raw provider exception into a failed :class:`_Attempt`.

    An unclassified error keeps the raw text (clipped) — worse to read, but it
    is all we know, and inventing a category would be worse still.
    """
    provider = model_str.partition(":")[0] if ":" in model_str else None
    try:
        from olano.core.actionable_errors import classify_exception

        classified = classify_exception(exc, provider)
    except Exception:  # pragma: no cover - defensive
        classified = None
    if classified is not None:
        return _Attempt(model_str, False, classified.message, classified.code, classified.retryable)
    return _Attempt(model_str, False, str(exc)[:300] or type(exc).__name__)


def _load_image_as_base64(path: str) -> tuple[str, str]:
    """Load a local image file and return (base64_data, media_type)."""
    p = Path(path)
    ext = p.suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    media_type = media_type_map.get(ext, "image/png")
    data = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
    return data, media_type


def _build_content(image: str, question: str) -> list[dict[str, Any]] | str:
    """Message content for the vision call, or an error string.

    Runs before any model is built so a bad path fails instantly instead of
    after a provider round-trip.
    """
    if image.startswith("http://") or image.startswith("https://"):
        return [
            {"type": "image_url", "image_url": {"url": image}},
            {"type": "text", "text": question},
        ]

    p = Path(image)
    if not p.exists():
        return f"Error: file not found: {image}"
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return (
            f"Error: unsupported file type '{p.suffix}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )
    try:
        b64, media_type = _load_image_as_base64(image)
    except Exception as e:
        return f"Error reading image file: {e}"

    return [
        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
        {"type": "text", "text": question},
    ]


def _record_vision_service(model_str: str, response: Any) -> None:
    """Ledger a successful vision call as a service event (the dashboard's
    "Service calls", matching the cloud's own per-service accounting).

    Deliberately the SERVICE ledger, not the usage ledger: ``olano:vision`` is
    a gateway utility group, not a chat tier, so a ``UsageEvent`` would derive
    ``billing_source="customer"`` and estimate 0 credits for it. And unlike its
    image/speech siblings this call is a non-streamed chat completion, so the
    gateway's cost header is present and the row can carry the real charge.
    """
    try:
        managed = model_str.startswith("olano:")
        credits: float | None = None
        if managed:
            from olano.analytics.usage_recorder import credits_from_headers

            meta = getattr(response, "response_metadata", None)
            headers = meta.get("headers") if isinstance(meta, dict) else None
            if headers is not None:
                credits = credits_from_headers(headers, managed=True)
        from olano.analytics.service_ledger import record_service_event

        record_service_event(
            "vision",
            provider=model_str.partition(":")[0] or "unknown",
            managed=managed,
            credits=credits,
        )
    except Exception:  # pragma: no cover - accounting must not break the tool
        logger.debug("vision: service ledger record failed", exc_info=True)


def _invoke_vision(model_str: str, content: list[dict[str, Any]]) -> _Attempt:
    """Run one candidate, recording what the outcome says about that model.

    A model that answers is forgiven any past failure; one that fails in a way
    that will not fix itself is remembered (see the failure memo).
    """
    from olano.core.stream_isolation import detached_runnable_config, nostream_config
    from olano.security.egress_guard import exempt as egress_exempt

    try:
        model = _build_vision_model(model_str)
        # This call runs INSIDE the agent's graph, so it inherits the turn's
        # streaming callbacks unless detached — and a vision transcription is
        # never the agent's answer. Left un-isolated, one photo burst (two
        # concurrent image_describe calls) interleaved two transcriptions
        # chunk-by-chunk into a single unreadable Telegram reply. See
        # olano.core.stream_isolation.
        # And exempt from the egress lists: this call runs inside a governed
        # tool, but the destination is the deployment's own vision model, not
        # somewhere the agent chose to go. The lists govern where the agent
        # reaches, never which model answers it. See security/egress_guard.
        # `nested_config` re-attaches only the turn's Langfuse handler (when
        # the turn is traced), so the call shows under the turn without
        # rejoining its stream.
        from olano.observability.tracing import nested_config

        with detached_runnable_config(), egress_exempt():
            response = model.invoke(
                [{"role": "user", "content": content}], config=nested_config(nostream_config())
            )
    except Exception as exc:
        attempt = _failed_attempt(exc, model_str)
        if attempt.code and not attempt.retryable:
            _note_failure(model_str, attempt.text)
        return attempt

    _clear_failure(model_str)
    _record_vision_service(model_str, response)
    text = getattr(response, "content", None)
    if text is None:
        return _Attempt(model_str, True, str(response))
    return _Attempt(model_str, True, text if isinstance(text, str) else str(text))


def _unrouted_failure(attempts: list[_Attempt]) -> bool:
    """True when a managed group failed because the gateway doesn't serve it."""
    try:
        from olano.core.actionable_errors import CODE_OLANO_UNROUTED
    except Exception:  # pragma: no cover - defensive
        return False
    return any(a.code == CODE_OLANO_UNROUTED for a in attempts)


def _exhausted_advice(attempts: list[_Attempt]) -> list[str]:
    """Closing lines for "every candidate failed".

    The case worth getting right is a MANAGED, keyless box. The old text told
    the reader to set ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY — on a
    deployment that runs on credits precisely so it needs no provider key, that
    is advice for a problem they do not have, about a product they did not buy,
    while the actual fault (the gateway serving no managed vision group) went
    unnamed. Lead with what is actually broken, and say plainly that it is not
    fixable from here.
    """
    healthy, failing = _alternatives([a.model for a in attempts])
    lines: list[str] = []
    if healthy:
        lines.append(
            f"Vision models still available here: {', '.join(healthy)}. "
            'Retry image_describe with model="<one of those>".'
        )
        # Name the failing ones too. They are configured here, and staying quiet
        # about them is how a fleet-wide outage reads as "nothing to see".
        if failing:
            lines.append(f"Also configured but failing right now: {', '.join(failing)}.")
        return lines

    if failing:
        lines.append(f"Also configured, but failing right now: {', '.join(failing)}.")
    if _unrouted_failure(attempts):
        lines.append(
            "The Olano-managed vision service is not reachable from this deployment. "
            "That is an Olano-side routing problem: no API key or setting here can fix "
            "it, and retrying will not help until it is resolved. Whoever operates this "
            "deployment can run `olano models gateway --probe` to see which managed "
            "services it can reach, and check the deployment card at cloud.olano.ai."
        )
        lines.append(
            "Setting a provider key (ANTHROPIC_API_KEY, OPENAI_API_KEY or "
            "GOOGLE_API_KEY / GEMINI_API_KEY) would give this box a fallback path "
            "in the meantime."
        )
    else:
        lines.append(
            "No other vision-capable model is configured. Add ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY or GOOGLE_API_KEY / GEMINI_API_KEY (or top up the "
            "failing provider) and try again."
        )
    lines.append(
        "Image generation tools (image_generate, nanobanana_*) cannot read images — "
        "don't use them as a fallback."
    )
    return lines


@tool
def image_describe(
    image: Annotated[
        str,
        "Path to a local image file OR a URL to an image (png, jpg, gif, webp, bmp)",
    ],
    question: Annotated[
        str,
        "What to ask about the image (e.g., 'What is in this image?', 'Read all text visible')",
    ] = "What is in this image? Describe it in detail.",
    model: Annotated[
        str,
        "Optional vision model as 'provider:model' (e.g. 'openai:gpt-5.4-mini', "
        "'anthropic:claude-haiku-4-5-20251001', 'google_genai:gemini-2.5-flash', "
        "'olano:vision'). LEAVE EMPTY unless you must pin one: empty auto-selects "
        "and automatically retries every other configured vision provider if the "
        "first is out of credits or unauthorized. A model you name here is used "
        "as-is, with no automatic fallback.",
    ] = "",
) -> str:
    """
    Describe or analyze an image using a vision-capable AI model.
    Accepts local file paths or image URLs. Use for screenshots, photos, charts, diagrams, etc.

    This is the ONLY tool that can read/see an image. With no `model` it picks a
    configured provider and falls back automatically across the others; if every
    one fails it reports why and lists the models still available to retry with.
    """
    content = _build_content(image, question)
    if isinstance(content, str):
        return content

    pinned = bool((model or "").strip() or _pinned_vision_model())
    chain = _vision_chain(model)
    if not chain:
        return (
            "[missing-credentials] image_describe: no vision-capable model is "
            "configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY or GOOGLE_API_KEY / "
            "GEMINI_API_KEY, connect this deployment to cloud.olano.ai to run on "
            "olano credits, or point LIONCLAW_VISION_MODEL at a model you can reach."
        )

    if pinned:
        missing = _missing_credential(chain[0])
        if missing:
            healthy, failing = _alternatives(chain)
            return (
                f"[missing-credentials] image_describe: {chain[0]} needs {missing}, "
                f"which is not configured.{_retry_hint(healthy, failing)}"
            )

    attempts: list[_Attempt] = []
    for candidate in chain:
        attempt = _invoke_vision(candidate, content)
        if attempt.ok:
            if attempts:
                # Name EVERY rung that failed before this one, not just the
                # first — two providers can fall through before one answers, and
                # hiding the middle failures makes a recurring outage invisible.
                failed = "; ".join(f"{a.model} ({a.text})" for a in attempts)
                return (
                    f"{attempt.text}\n\n[vision-fallback] fell through: {failed}; "
                    f"described with {candidate} instead."
                )
            return attempt.text
        attempts.append(attempt)

    lines = ["[tool-error] image_describe: could not analyze the image."]
    lines += [f"  - {a.model}: {a.text}" for a in attempts]
    lines.extend(_exhausted_advice(attempts))
    return "\n".join(lines)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
