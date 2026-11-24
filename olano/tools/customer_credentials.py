"""Per-customer credentials: each end-customer brings their own token.

The missing piece for selling credential-bound agents as one deployment with
many subscribers (each customer's own Google Sheets / HubSpot / API token,
instead of one operator token every customer's data would flow through).

Three identity-injected tools (``customer_service.user_credentials``), plus
the two integration points that make a stored secret actually usable:

- :func:`resolve_customer_credential` — called FIRST by the shared
  ``get_credential`` chain. For a name the agent DECLARED per-customer
  (``customer_service.customer_credential_names``) during a customer-bound
  turn, resolution is the customer's own value or nothing — it never falls
  through to the operator's env/vault token, because that silent fallback is
  exactly the cross-tenant accident this exists to prevent (customer A's
  books mirrored into the operator's spreadsheet). With no customer bound
  (operator chat, classic scheduled runs) the normal chain applies unchanged.
- :func:`customer_secret_env` — the declared names resolved for one customer
  as env vars, injected into their ``allowed_actions``/fan-out action runs
  (cwd = their folder, so the script sees THEIR token next to THEIR data).

Secrets are AES-encrypted in the vault under ``(agent, customer, name)`` — a
namespace fully separate from operator credentials; ``agent_secret_env``
(long-lived stdio-MCP children shared by every customer) reads only the
agent tables and can never see them. ``forget_me`` erases them.

Caveat, stated where operators can read it (knowledge docs): a secret a
customer pastes into chat is stored encrypted, but the chat transcript that
carried it lives in the thread history like any other message.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Per-process registry of which credential names each agent declared
#: per-customer, populated at build time (builder) — the resolver in
#: ``_credentials.get_credential`` is toolkit-level and has no config object.
_DECLARED: dict[str, tuple[str, ...]] = {}


def register_customer_credential_names(agent_name: str, names: list[str] | None) -> None:
    _DECLARED[agent_name] = tuple(dict.fromkeys(n.strip() for n in (names or []) if n.strip()))


def declared_customer_credential_names(agent_name: str) -> tuple[str, ...]:
    return _DECLARED.get(agent_name, ())


def resolve_customer_credential(agent_name: str, name: str) -> tuple[bool, str | None]:
    """``(handled, value)`` for the shared ``get_credential`` chain.

    ``handled=True`` means the name is declared per-customer AND a customer is
    bound: the chain must stop here — ``value`` (possibly None ⇒ unset) is the
    final answer, fail closed. ``handled=False`` means proceed as ever.
    Never raises.
    """
    if name not in declared_customer_credential_names(agent_name):
        return False, None
    try:
        from olano.memory.customer_memory import active_customer_id
        from olano.security.request_context import get_request_context
        from olano.vault.resolver import get_vault

        # Owner contexts (operator chat, CLI, classic scheduled runs — and the
        # fallback context outside any push, which is owner by default) use
        # the normal chain: the operator is entitled to their own token. Only
        # a genuine customer-bound turn (is_owner=False, resolvable identity)
        # switches to the customer's namespace.
        if getattr(get_request_context(), "is_owner", True):
            return False, None
        cid = active_customer_id()
        if not cid:
            return False, None
        vault = get_vault()
        if vault is None:
            return True, None  # declared + customer bound + no vault ⇒ unset, not operator's
        return True, vault.get_customer(name, agent_name, cid)
    except Exception:
        logger.debug("Per-customer credential lookup failed for %s", name, exc_info=True)
        # A broken lookup during a customer turn must still fail closed for a
        # declared name — return handled with no value, never the operator's.
        return True, None


def customer_secret_env(agent_name: str, customer_key: str) -> dict[str, str]:
    """Declared per-customer credentials for one customer, as env vars.

    Injected into that customer's action runs. Missing names are simply
    absent — the script's own missing-credential handling stays in charge.
    Never raises.
    """
    env: dict[str, str] = {}
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is None:
            return env
        for name in declared_customer_credential_names(agent_name):
            value = vault.get_customer(name, agent_name, customer_key)
            if value:
                env[name] = value
    except Exception:
        logger.debug("customer_secret_env failed for %s", customer_key, exc_info=True)
    return env


def erase_customer_credentials(agent_name: str, customer_key: str) -> int:
    """forget_me hook — drop every credential this customer stored."""
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
        return vault.erase_customer(agent_name, customer_key) if vault is not None else 0
    except Exception:
        logger.debug("Credential erasure failed for %s", customer_key, exc_info=True)
        return 0


def make_customer_credential_tools(agent_name: str, config) -> list:
    """set/list/delete_my_credential — bound when user_credentials is on."""
    from langchain_core.tools import tool

    from olano.memory.customer_memory import active_customer_id

    declared = tuple(getattr(config.customer_service, "customer_credential_names", ()) or ())
    register_customer_credential_names(agent_name, list(declared))
    no_customer = (
        "No identified customer in this conversation, so there is nowhere to store a credential."
    )
    accepted = ", ".join(declared) if declared else "(none configured)"

    @tool
    def set_my_credential(name: str, value: str) -> str:
        """Store the CURRENT customer's own token/credential, encrypted, under their account.

        Only the credential names this agent declares are accepted — never
        invent one. The value is used only for THIS customer's own work (their
        turns and their scheduled runs); it never affects anyone else and the
        operator's own keys are never used in its place. Never echo the value
        back in conversation after storing it.
        """
        cid = active_customer_id()
        if not cid:
            return no_customer
        clean_name = (name or "").strip()
        if clean_name not in declared:
            return f"'{clean_name}' is not a credential this agent accepts. Accepted: {accepted}."
        secret = (value or "").strip()
        if not secret:
            return "The credential value is empty — nothing stored."
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is None:
            return "The credential vault is not available right now — nothing stored."
        vault.set_customer(clean_name, agent_name, cid, secret)
        return (
            f"Stored {clean_name} for this customer (encrypted). It applies only to "
            "their own account; delete_my_credential removes it any time. For "
            "safety they may want to delete the chat message that contained it."
        )

    @tool
    def list_my_credentials() -> str:
        """List which credential names the CURRENT customer has stored (never the values)."""
        cid = active_customer_id()
        if not cid:
            return no_customer
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is None:
            return "The credential vault is not available right now."
        rows = vault.list_customer_names(agent_name, cid)
        if not rows:
            return f"No credentials stored for this customer yet. Accepted names: {accepted}."
        return "\n".join(f"- {r['name']} (updated {r['updated_at'][:10]})" for r in rows)

    @tool
    def delete_my_credential(name: str) -> str:
        """Delete one of the CURRENT customer's stored credentials."""
        cid = active_customer_id()
        if not cid:
            return no_customer
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is None:
            return "The credential vault is not available right now."
        if vault.delete_customer((name or "").strip(), agent_name, cid):
            return f"Deleted {name.strip()} for this customer."
        return f"This customer has no stored credential named '{(name or '').strip()}'."

    return [set_my_credential, list_my_credentials, delete_my_credential]
