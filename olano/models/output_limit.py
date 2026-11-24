"""One number, ``max_output_tokens``, translated to whatever the provider calls it.

``agent.json5 → message_policy.max_output_tokens`` is a provider-agnostic cap on
one reply. LangChain does not normalise it: the wire parameter is ``max_tokens``
for most integrations, ``max_output_tokens`` for the Google clients, and
``num_predict`` for Ollama (which reads ``max_tokens`` as nothing at all, so an
unmapped value is silently ignored rather than rejected — the worst kind of
failure for a cap someone is relying on).

This is the sibling of :mod:`olano.models.reasoning`'s ``gate_reasoning_kwargs``
and sits at the same chokepoint, ``build_model``, so every construction path —
agent build, per-chat ``/model`` override, cognition worker — maps the same way.

**An explicit cap always wins over a stale ``model_kwargs`` entry.** An operator
could have hand-written ``model_kwargs: {max_tokens: 4000}`` before this field
existed; if both are present the policy field is the one with a dashboard
control and a description, so it overwrites, and the shadowed entry is logged.

Two things this deliberately does **not** do. It does not translate a cap for a
provider not listed here — an unknown integration gets the ``max_tokens``
default, because every OpenAI-compatible endpoint (openrouter, together,
litellm, vercel, the olano gateway) accepts exactly that, and guessing a novel
spelling would be worse than a parameter the endpoint ignores. And it does not
lower a cap to fit a reasoning budget: on models that bill thinking against the
same ceiling a small value can produce an empty reply, which the config's help
text warns about — silently raising an operator's number would break the cap
they asked for, so it is reported at build time instead (see :func:`warn_if_thin`).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Providers whose integration class does not take ``max_tokens``. Everything
#: absent here gets ``max_tokens``, which is both the LangChain convention and
#: what every OpenAI-compatible relay accepts.
_PARAM_BY_PROVIDER: dict[str, str] = {
    "google_genai": "max_output_tokens",
    "google_vertexai": "max_output_tokens",
    # ChatOllama maps this onto the Ollama `options.num_predict` field; it has
    # no `max_tokens` at all, so the default spelling would vanish silently.
    "ollama": "num_predict",
}

#: Below this, a model that bills its own reasoning against the output ceiling
#: can spend the whole budget thinking and return an empty reply. Anthropic's
#: extended thinking and the OpenAI o-series both behave this way. The number is
#: the smallest ceiling that leaves room for a short answer after a minimal
#: thinking budget; it only ever produces a log line, never a changed value.
REASONING_HEADROOM_TOKENS = 1024


def output_limit_param(provider: str) -> str:
    """The kwarg name ``provider`` accepts for a per-reply token ceiling."""
    return _PARAM_BY_PROVIDER.get((provider or "").strip().lower(), "max_tokens")


def apply_output_limit(
    provider: str, kwargs: dict[str, Any], max_output_tokens: int | None
) -> dict[str, Any]:
    """Write ``max_output_tokens`` into ``kwargs`` under ``provider``'s name.

    Mutates and returns ``kwargs`` (same contract as ``gate_reasoning_kwargs``).
    A ``None`` or non-positive cap is a no-op, so an unconfigured agent reaches
    the provider with exactly the kwargs it always had.
    """
    if not max_output_tokens or max_output_tokens <= 0:
        return kwargs
    param = output_limit_param(provider)
    # Clear every spelling before writing ours: a config carrying `max_tokens`
    # from a previous provider would otherwise ride along beside `num_predict`
    # and reach, say, Ollama as an unknown field.
    for stale in set(_PARAM_BY_PROVIDER.values()) | {"max_tokens"}:
        if stale != param and stale in kwargs:
            logger.debug(
                "message_policy.max_output_tokens: dropping stale %s=%r for provider %r",
                stale,
                kwargs.pop(stale),
                provider,
            )
    if param in kwargs and kwargs[param] != max_output_tokens:
        logger.info(
            "message_policy.max_output_tokens=%d overrides model_kwargs %s=%r",
            max_output_tokens,
            param,
            kwargs[param],
        )
    kwargs[param] = int(max_output_tokens)
    return kwargs


def warn_if_thin(
    model_str: str, max_output_tokens: int | None, reasoning_effort: str | None
) -> None:
    """Log once when a cap is small enough to be eaten by thinking tokens.

    Advisory only. The alternative — quietly raising the number — would mean an
    operator who typed 300 gets something else, which is exactly the surprise a
    hard cap exists to remove.
    """
    if not max_output_tokens or max_output_tokens >= REASONING_HEADROOM_TOKENS:
        return
    effort = (reasoning_effort or "").strip().lower()
    if not effort or effort == "none":
        return
    logger.warning(
        "Agent model %s has message_policy.max_output_tokens=%d with "
        "reasoning_effort=%s. On models that count reasoning against the output "
        "ceiling this can consume the whole budget and return an empty reply — "
        "raise the cap above %d or set reasoning_effort to none/low.",
        model_str,
        max_output_tokens,
        effort,
        REASONING_HEADROOM_TOKENS,
    )
