"""Mint an access token for outside callers — on a page, never in the chat.

An operator who is chatting with an agent from a phone has no dashboard open,
but a bearer token for the MCP / A2A endpoints must never travel through a
messenger and must never enter the model's context. So the agent sends a
PIN-protected link (``magic_link_mint_access_token``, admin humans only); the
page mints the token in the browser session and shows it **exactly once**: the
raw value is returned to the page under ``reveal``, which ``service.act_on_link``
passes through to that one response and never persists — the stored result, the
completion notice in the chat and the admin listing carry only the token's label
and id.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.magic_links.flows import STATUS_DONE, BaseFlow, FlowField, register
from olano.magic_links.store import LinkRecord

logger = logging.getLogger(__name__)

_TTL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("default", "Deployment default"),
    ("30", "30 days"),
    ("90", "90 days"),
    ("365", "1 year"),
    ("0", "Never expires"),
)


def _exposed_agents() -> list[str]:
    """Agents flagged for external access on disk — what a token can reach."""
    try:
        from olano.config.loader import list_agents, load_agent_config
    except Exception:  # pragma: no cover
        return []
    out: list[str] = []
    for name in list_agents():
        try:
            ext = load_agent_config(name).external_access
        except Exception:
            continue
        if ext.mcp or ext.a2a:
            out.append(name)
    return sorted(out)


class AccessTokenFlow(BaseFlow):
    id = "access-token"
    label = "Access token"
    kind = "form"
    applies = "none"
    instructions = (
        "Mint a bearer token that lets an outside platform (Claude, ChatGPT, Cursor, another "
        "agent) send messages to the chosen agent over MCP or A2A. It is shown once."
    )

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        label = str(params.get("label") or "").strip()[:120]
        agent = str(params.get("agent") or "").strip()
        return {"label": label, "agent": agent}

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        base["service"] = {"id": "access-token", "name": "external access"}
        base["title"] = "Mint an access token"
        pinned = str(link.params.get("agent") or "")
        exposed = _exposed_agents()
        options: list[tuple[str, str]] = []
        if pinned:
            options.append((pinned, pinned))
        options.extend((a, a) for a in exposed if a != pinned)
        options.append(("*", "Every exposed agent (now and later)"))
        if not exposed and not pinned:
            base["instructions"] = (
                "No agent is published for external access yet. Turn on MCP or A2A on an agent "
                "(dashboard → agent → External access) before minting a token, or mint one for "
                "every future exposed agent."
            )
        base["fields"] = [
            f.public()
            for f in (
                FlowField(
                    name="label",
                    label="Label",
                    placeholder="e.g. Claude desktop — Ana",
                    help=(
                        "Which integration this token is for. Shown in the token list and as "
                        "the caller's name in the Agent to Agent view; how a leaked token is "
                        "traced and revoked on its own."
                    ),
                    value=str(link.params.get("label") or ""),
                ),
                FlowField(
                    name="agents",
                    label="Agent the token may reach",
                    options=tuple(options),
                    help=(
                        "Only agents published for external access are reachable, whatever the "
                        "token names."
                    ),
                ),
                FlowField(
                    name="ttl",
                    label="Lifetime",
                    options=_TTL_OPTIONS,
                    help="How long the token works. Revoke it any time from the dashboard.",
                ),
            )
        ]
        return base

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        from olano.config.system_settings import load_external_access_settings
        from olano.mcp_server.tokens import get_token_store

        label = str(fields.get("label") or link.params.get("label") or "").strip()
        if not label:
            raise ValueError("Give the token a label (which integration it is for).")
        agent = str(fields.get("agents") or link.params.get("agent") or "").strip()
        if not agent:
            raise ValueError("Pick the agent the token may reach.")
        ttl_raw = str(fields.get("ttl") or "default")
        settings = load_external_access_settings()
        ttl = settings.token_ttl_days if ttl_raw == "default" else int(ttl_raw)
        raw, record = get_token_store().mint(
            label=label,
            agents=[agent],
            ttl_days=ttl,
            created_by=f"magic-link:{link.id}",
        )
        logger.info("magic-link %s minted access token %s (%s)", link.id, record.id, label)
        target = "every exposed agent" if agent == "*" else agent
        return {
            "status": STATUS_DONE,
            "result": (
                f"Access token '{label}' (id {record.id}) minted for {target}. It was shown once "
                "on the page; revoke it from the dashboard's External access tab if needed."
            ),
            # One-time display: passed through to this response only, never stored.
            "reveal": raw,
        }


register(AccessTokenFlow())
