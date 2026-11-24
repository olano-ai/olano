"""Preflight check: is an agent's model actually usable right now?

A fresh cloud deployment can boot with **no provider API key set** and/or **no
model chosen yet**. The head-less background flows — Pulse curation, ideation,
memory gardening, delegated / scheduled cognition workers — build and invoke a
model with no human watching. Without a preflight, a missing key surfaces as a
raw ``TypeError: Could not resolve authentication method`` deep inside the
provider SDK: a scary stack trace in the logs, an unclassified crash, and no
signal the operator can act on.

:func:`check_model_ready` resolves the model's provider credential the **same
way the model build does** — under the agent's own identity, honouring
``config.env``, per-agent vault overrides, the common vault, live OAuth
connectors, and ``os.environ`` — and returns a structured
:class:`~olano.core.actionable_errors.ActionableError` when the model can't
be reached. Callers turn that into a graceful, human-readable outcome instead of
crashing.

It also returns the *resolved* key (:attr:`ModelReadiness.value`) so a caller
building an ephemeral child agent (which runs under a fresh generated name and
therefore can't resolve a parent-scoped vault override on its own) can pin the
credential into the child's build env.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from olano.core.actionable_errors import (
    CODE_INVALID_KEY,
    ActionableError,
    remediation_for_auth,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# Stable code for "no model configured" (distinct from a missing API key). Kept
# alongside the actionable-error codes so surfaces can key off it if needed.
CODE_MODEL_UNSET = "model_unset"


@dataclass(frozen=True)
class ModelReadiness:
    """Result of :func:`check_model_ready`.

    ``ok`` is ``True`` when the model can be reached (or when readiness can't be
    verified for this provider, in which case we defer to the build/invoke rather
    than blocking). When ``ok`` is ``False``, ``error`` carries a human-facing,
    actionable explanation. ``env_var``/``value`` are set when a static API-key
    provider's key was resolved, so callers can pin it into a child build env.
    """

    ok: bool
    env_var: str | None = None
    value: str | None = None
    error: ActionableError | None = None

    @property
    def credential_env(self) -> dict[str, str] | None:
        """The resolved provider key as an ``env`` overlay, or ``None``."""
        if self.env_var and self.value:
            return {self.env_var: self.value}
        return None


def _resolve_key_under_identity(agent_name: str, env: dict[str, str] | None, env_var: str) -> str:
    """Resolve ``env_var`` exactly as a toolkit would, under ``agent_name``.

    Binds the agent's runtime context so ``get_credential`` honours the full
    precedence chain (config.env overlay → connector broker → vault override →
    common vault → os.environ). Never raises — resolution failure means "absent".
    """
    try:
        from olano.tools._credentials import agent_runtime_context, get_credential

        with agent_runtime_context(agent_name, env):
            return get_credential(env_var)
    except Exception:
        return ""


def check_model_ready(config: AgentConfig, *, model: str | None = None) -> ModelReadiness:
    """Return whether ``config``'s model is usable right now.

    ``model`` overrides ``config.model`` (a worker may run a different model than
    its parent). Only the common, verifiable case is gated: an API-key provider
    whose key resolves to a non-empty value. Providers with no static key env var
    (Ollama, OpenAI-Codex OAuth, LiteLLM, …) can't be verified this way, so we
    return ``ok=True`` and let the build/invoke surface any problem itself.
    """
    from olano.models import PROVIDERS, parse_model_string

    model_str = (model or getattr(config, "model", "") or "").strip()
    if not model_str:
        return ModelReadiness(
            ok=False,
            error=ActionableError(
                CODE_MODEL_UNSET,
                "No model is configured for this agent yet. Choose a model in the "
                "dashboard (Cortex → Model) to enable background reasoning.",
            ),
        )

    provider, _ = parse_model_string(model_str)
    info = PROVIDERS.get(provider or "")
    env_var = getattr(info, "env_var", None)

    if not env_var:
        # No static API key to check (OAuth / local / proxy providers).
        return ModelReadiness(ok=True)

    value = _resolve_key_under_identity(config.name, getattr(config, "env", None), env_var)
    if value:
        return ModelReadiness(ok=True, env_var=env_var, value=value)

    pname = info.name if info else (provider or "the model provider")
    return ModelReadiness(
        ok=False,
        env_var=env_var,
        error=ActionableError(
            CODE_INVALID_KEY,
            f"The API key for {pname} isn't configured yet, so this agent can't "
            f"reach its model. Set {env_var} in the dashboard → Providers (or the "
            "agent's vault) to enable background reasoning.",
            remediation=remediation_for_auth(provider),
        ),
    )
