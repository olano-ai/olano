"""`default:*` model aliases — the deployment-level indirection layer.

`default:turbo` / `default:pro` / `default:max` may appear anywhere a model
string is accepted (agent.json5 `model`, subagent models, cognition fast/deep
models, `/model`, the dashboard/chat pickers). They are never built directly:
:func:`resolve_model_alias` swaps them for the concrete target this box is
configured with — the `default_models:` block of config.yaml when the operator
set one, the box's built-in target otherwise (Olano tiers on a managed box,
Codex literals on BYOK; see `config/system_settings.py`).

Resolution happens at the consumers, deliberately not at config-parse time:
`agent.json5` keeps the alias verbatim, so retargeting the fleet is one
config.yaml edit applied on the next build — never a file rewrite. The callers
that must see a concrete model are `models.build_model` (the one chokepoint
every brain surface funnels through), the runner's usage/credits recording,
and the context-window report; everything else may keep the alias.

`resolve_model_alias` is idempotent for non-alias strings, so calling it at
multiple layers is safe. Imports of `system_settings` stay lazy: this module
is imported from `build_model`, and config parsing must never pull the model
stack (nor vice versa at import time).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The pseudo-provider prefix. Deliberately NOT in `models.PROVIDERS` — it has
#: no package, no key, no health probe; pickers learn about it through the
#: `model_aliases` field of the providers payload instead.
ALIAS_PROVIDER = "default"

ALIAS_TIERS = ("turbo", "pro", "max")


def is_model_alias(model_str: str | None) -> bool:
    """True for a ``default:<anything>`` model string (tier validity aside)."""
    if not model_str or ":" not in model_str:
        return False
    provider, _, _ = model_str.partition(":")
    return provider.strip().lower() == ALIAS_PROVIDER


def alias_targets() -> dict[str, str]:
    """``{"turbo": "<provider:model>", ...}`` — every tier's current target.

    For API/display surfaces (the providers payload, the Config card) that
    need the whole mapping at once. Reads through the cached home-config doc,
    so it is cheap enough for request paths.
    """
    from olano.config.system_settings import load_default_models

    return load_default_models()


def resolve_model_alias(model_str: str) -> str:
    """Swap a ``default:<tier>`` alias for its configured concrete target.

    Anything that is not an alias returns verbatim (idempotent — safe to call
    at multiple layers). An unknown tier raises an actionable ``ValueError``:
    a typo must fail at selection/build time, not dissolve into an
    unknown-provider error deep in langchain. A configured target that is
    itself an alias (a hand-edited config.yaml — the write path refuses it)
    falls back to the box's built-in target for that tier with a warning:
    resolution never recurses, and an agent must boot.
    """
    if not is_model_alias(model_str):
        return model_str

    _, _, tier_raw = model_str.partition(":")
    tier = tier_raw.strip().lower()
    if tier not in ALIAS_TIERS:
        raise ValueError(
            f"Unknown default-model tier '{tier_raw.strip()}'. "
            "Use default:turbo, default:pro or default:max."
        )

    from olano.config.system_settings import (
        builtin_default_model_targets,
        load_default_models,
    )

    target = load_default_models().get(tier, "")
    if not target or is_model_alias(target):
        builtin = builtin_default_model_targets()[tier]
        if target:
            logger.warning(
                "default_models.%s points at another alias (%r); using this box's "
                "built-in target %r instead — aliases may not chain.",
                tier,
                target,
                builtin,
            )
        target = builtin
    return target
