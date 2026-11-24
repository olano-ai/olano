"""Secret entry — hand over an API key without putting it in the chat.

This is the flow that exists because ``secret_set`` cannot be used safely from a
messenger: whatever the user types goes through the platform's servers, lands in
the session JSONL transcript, and enters the model's context. Here the agent
only ever sends a *link*; the value travels browser → HTTPS → vault and is never
echoed back, logged, or included in the completion notice — the chat learns the
credential's **name**, never its value.

Scope follows the vault's own model: no ``agent`` writes the common value every
agent sees, an ``agent`` writes that agent's override (``credential_overrides``),
which is how two agents end up using two different accounts for one service.

The vault must be unlocked to store anything; on a locked vault the flow says so
rather than silently dropping the secret.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from olano.magic_links.flows import (
    STATUS_DONE,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

logger = logging.getLogger(__name__)

# Vault credential names are env-var shaped; anything else is almost certainly a
# mistake that would never resolve at tool-call time.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def unlocked_vault() -> Any:
    """The unlocked vault, or ``None``. Never raises."""
    try:
        from olano.vault.resolver import get_vault

        return get_vault()
    except Exception:  # pragma: no cover - defensive
        return None


def store_secret(
    name: str,
    value: str,
    *,
    agent: str | None,
    description: str,
    category: str = "general",
) -> None:
    """Write one credential to the vault. Raises ``ValueError`` with a message
    that is safe to show the user (it never contains the value)."""
    if not value.strip():
        raise ValueError("The value can't be empty.")
    vault = unlocked_vault()
    if vault is None:
        raise ValueError(
            "The credentials vault is locked, so nothing was saved. Unlock it in "
            "the dashboard (Vault → Unlock) and use a fresh link."
        )
    try:
        vault.set(
            name=name,
            value=value.strip(),
            description=description,
            category=category,
            agent=agent,
        )
    except Exception as exc:  # never surface a stack trace to a public page
        logger.warning("magic-link: vault write failed for %s: %s", name, exc)
        raise ValueError("Could not save to the vault. Ask the owner to check the server.") from exc


class SecretEntryFlow(BaseFlow):
    id = "secret-entry"
    label = "API key"
    kind = "form"
    instructions = "Paste the value here. It goes straight into the encrypted vault."
    fields = (
        FlowField(
            name="value",
            label="Value",
            secret=True,
            placeholder="Paste the key",
            help="Stored encrypted. It is never shown in the chat or the logs.",
        ),
    )

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip().upper()
        if not name:
            raise ValueError("A credential name is required (e.g. STRIPE_API_KEY).")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"'{name}' isn't a valid credential name — use uppercase letters, "
                "digits and underscores, like STRIPE_API_KEY."
            )
        out: dict[str, Any] = {"name": name, "service": name}
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        reason = str(params.get("reason") or "").strip()
        if reason:
            out["reason"] = reason[:200]
        return out

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        name = str(link.params.get("name") or "")
        agent = link.params.get("agent")
        base["service"] = {"id": "vault", "name": "the vault"}
        base["title"] = f"Add {name}"
        scope = f" for the agent '{agent}'" if agent else ""
        reason = str(link.params.get("reason") or "").strip()
        if reason and reason[-1] not in ".!?":
            reason += "."  # the agent's reason is usually a fragment ("for invoices")
        base["instructions"] = (
            f"{reason} " if reason else ""
        ) + f"The value is stored encrypted as {name}{scope} and never appears in the chat."
        return base

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        name = str(link.params.get("name") or "")
        agent = str(link.params.get("agent") or "") or None
        store_secret(
            name,
            fields.get("value", ""),
            agent=agent,
            description=str(link.params.get("reason") or "Added via a magic link"),
        )
        scope = f" for {agent}" if agent else ""
        return {"status": STATUS_DONE, "result": f"{name} saved to the vault{scope}."}


register(SecretEntryFlow())
