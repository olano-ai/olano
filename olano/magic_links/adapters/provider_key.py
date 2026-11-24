"""LLM provider keys — "connect a model provider" from a chat.

The same form machinery as :mod:`secret_entry`, but driven by
:data:`olano.models.PROVIDERS` so the user picks a provider by name and the
key lands under that provider's canonical env-var (``ANTHROPIC_API_KEY``,
``GROQ_API_KEY``, …) — the exact name ``build_model`` and the credential
resolver look for. Getting that name wrong is the usual reason a hand-added key
appears to do nothing, so it is not left to the person pasting.

Providers that don't take an API key are filtered out of the picker: Ollama
needs none, and Codex authenticates with a ChatGPT account (use the
``codex-device`` flow instead).

Honours the deployment BYOK entitlement, the same gate
``POST /api/vault/secrets`` applies — a plan without ``byok`` (Ultra+) or
without ``model_selection`` can't have provider keys added through a side door.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links.adapters.secret_entry import store_secret
from olano.magic_links.flows import (
    STATUS_DONE,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

# Providers whose credential is not a pasteable API key.
_EXCLUDED = frozenset({"ollama", "openai-codex"})


def _selectable_providers() -> list[tuple[str, str, str]]:
    """``(key, name, env_var)`` for every provider that takes an API key."""
    from olano.models import PROVIDERS

    return [
        (key, info.name, info.env_var)
        for key, info in sorted(PROVIDERS.items())
        if info.env_var and key not in _EXCLUDED
    ]


def _provider_entry(provider_key: str) -> tuple[str, str, str] | None:
    for entry in _selectable_providers():
        if entry[0] == provider_key:
            return entry
    return None


def _assert_byok_allowed() -> None:
    """Refuse when the deployment's plan has BYOK disabled."""
    from olano.deployment import enforcement as dep_enforcement

    try:
        allowed = dep_enforcement.feature_enabled(
            "model_selection"
        ) and dep_enforcement.feature_enabled("byok")
    except Exception:  # pragma: no cover - entitlements unavailable ⇒ allow
        return
    if not allowed:
        raise ValueError(
            "Adding model-provider API keys (BYOK) is not included in this deployment's plan — "
            "it is available on the Ultra plan and above at cloud.olano.ai."
        )


class ProviderKeyFlow(BaseFlow):
    id = "provider-key"
    label = "LLM provider"
    kind = "form"

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        _assert_byok_allowed()
        out: dict[str, Any] = {"service": "provider-key"}
        provider = str(params.get("provider") or "").strip().lower()
        if provider:
            entry = _provider_entry(provider)
            if entry is None:
                known = ", ".join(key for key, _, _ in _selectable_providers())
                raise ValueError(
                    f"Unknown model provider '{provider}'. Ones that take an API key: {known}."
                )
            out["provider"] = provider
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        return out

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        base["service"] = {"id": "provider-key", "name": "the vault"}
        pinned = str(link.params.get("provider") or "")
        agent = link.params.get("agent")
        scope = f" for the agent '{agent}'" if agent else ""

        if pinned:
            entry = _provider_entry(pinned)
            name, env_var = (entry[1], entry[2]) if entry else (pinned, "")
            base["title"] = f"Connect {name}"
            base["instructions"] = (
                f"Paste your {name} API key. It is stored encrypted as {env_var}{scope}."
            )
            fields: tuple[FlowField, ...] = (
                FlowField(
                    name="value",
                    label=f"{name} API key",
                    secret=True,
                    placeholder="Paste the key",
                    help="Stored encrypted. It never appears in the chat or the logs.",
                ),
            )
        else:
            base["title"] = "Connect a model provider"
            base["instructions"] = (
                f"Pick the provider and paste its API key. It is stored encrypted{scope} "
                "under the name that provider expects."
            )
            fields = (
                FlowField(
                    name="provider",
                    label="Provider",
                    options=tuple(
                        (key, f"{name} ({env})") for key, name, env in _selectable_providers()
                    ),
                ),
                FlowField(
                    name="value",
                    label="API key",
                    secret=True,
                    placeholder="Paste the key",
                    help="Stored encrypted. It never appears in the chat or the logs.",
                ),
            )
        base["fields"] = [f.public() for f in fields]
        return base

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        _assert_byok_allowed()
        provider = str(link.params.get("provider") or fields.get("provider") or "").strip().lower()
        entry = _provider_entry(provider)
        if entry is None:
            raise ValueError("Pick a provider before saving.")
        _, name, env_var = entry
        agent = str(link.params.get("agent") or "") or None
        store_secret(
            env_var,
            fields.get("value", ""),
            agent=agent,
            description=f"{name} API key (added via a magic link)",
            category="model_provider",
        )
        scope = f" for {agent}" if agent else ""
        return {
            "status": STATUS_DONE,
            "result": f"{name} connected — key stored as {env_var}{scope}.",
        }


register(ProviderKeyFlow())
