"""Agent-facing tools for auth magic links.

These are how an agent gets a user through an authentication it cannot perform
itself. The host has no browser (``xdg-open`` fails on a server), and the user
is on Telegram or WhatsApp, so the agent mints a short-lived link, sends it into
the chat, and a background watcher reports the outcome back to that same chat.

Every tool here is **non-blocking**: it returns a URL + PIN immediately and the
turn ends. Do not poll — the watcher posts a ✅/⚠️ notice on its own.

Design rules carried over from ``olano_authorize_mcp_server``:

* **Identity is a runtime fact, not an argument.** Who asked, on which channel,
  and in which chat all come from the live request context — never from the
  model. A prompt-injected "send the link to this other chat" has no parameter
  to hijack.
* **Owner-gated.** Minting a link grants its holder the power to connect an
  account or write a vault secret, so the sender must be on the channel's
  ``allowed_users`` allowlist, and an open bot (no allowlist at all) is refused
  with setup guidance — the same gate ``/olano`` uses.
* **Group chats are refused** by default: a link and its PIN posted in a group
  are visible to everyone in it.

Every agent gets these tools by default under the ``magic_links`` autonomy
capability (``config/autonomy_defaults.py``) — a link is the default way to
connect a service, collect a key or pair a messenger, on every agent, not an
AgentFather privilege. The capability is re-checked on every call, so a
dashboard switch applies to the next call; minting stays HITL-gated, so an
owner approves before a link exists; and ``magic_links.enabled`` in
``config.yaml`` remains the hard off switch below all of this.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False, default=str)


def _sending_agent() -> str:
    """The agent executing this turn (bound by the runner for every turn)."""
    from olano.tools.talk_to import current_agent

    name = current_agent.get()
    if not name:
        raise RuntimeError("no executing-agent context for a magic link")
    return name


def _request_origin() -> tuple[dict[str, Any], dict[str, Any]]:
    """``(requested_by, notify)`` captured from the live request context.

    Must be read while the tool call is running — the background watcher runs
    long after the context is gone, so the chat to report back to has to be
    frozen here.
    """
    from olano.security.request_context import get_request_context

    ctx = get_request_context()
    agent = _sending_agent()
    meta = getattr(ctx, "metadata", {}) or {}
    chat_id = str(meta.get("chat_id") or ctx.session_id or "owner")
    requested_by = {
        "channel": ctx.channel or "",
        "user_id": str(ctx.user_id or ""),
        "username": str(meta.get("username") or meta.get("name") or ""),
        "agent": agent,
        "is_group": bool(getattr(ctx, "is_group", False)),
    }
    notify = {"channel_type": ctx.channel or "", "target": chat_id, "agent_name": agent}
    return requested_by, notify


def _owner_gate() -> str | None:
    """``None`` when the caller may mint, else an actionable refusal.

    Mirrors ``platform_control.commands._owner_gate``: an unauthenticated
    sender on a bot with no allowlist could otherwise talk an agent into
    handing them a vault-writing form.
    """
    from olano.security.request_context import get_request_context

    ctx = get_request_context()
    # Local operator surfaces (CLI, dashboard) are already authenticated by
    # the time a turn runs; only messenger senders need the allowlist check.
    if ctx.channel in ("", "local", "api", "cli"):
        return None
    if not getattr(ctx, "is_owner", False):
        return (
            "Magic links are owner-only. If you are the owner of this "
            "deployment, add your user id to this channel's `allowed_users` "
            "(dashboard → agent → Channels) and try again."
        )
    return None


def _capability_gate() -> str | None:
    """Refusal when the ``magic_links`` autonomy capability is off, else ``None``.

    Mirrors ``self_config_tools._gate``: the sending agent's config is re-read
    on every call so a dashboard switch applies immediately, and a config that
    fails to load counts as *global default* (the load error is the agent's
    real problem and surfaces elsewhere). Runs **before** the owner gate, so
    "this deployment has links turned off" is the same answer for everybody
    and a non-owner never learns the capability state from the refusal order.
    """
    from olano.config.system_settings import autonomy_allowed

    try:
        agent = _sending_agent()
    except RuntimeError:
        return None  # no agent context: the owner gate reports that case
    config = None
    try:
        from olano.config.loader import load_agent_config

        config = load_agent_config(agent)
    except Exception:
        logger.debug("magic_link: could not load config for %s", agent, exc_info=True)
    if autonomy_allowed("magic_links", config):
        return None
    return _err(
        "magic links are turned off for this agent",
        how_to_enable=(
            "An admin can turn them on in the dashboard: this agent's Autonomy "
            "settings → Send magic links (per agent), or Config → Agent "
            "self-configuration (whole deployment). Config → Magic links must "
            "also be enabled."
        ),
    )


def _mint(flow: str, params: dict[str, Any]) -> str:
    """Shared mint path: gate → mint → watch → return the link for the reply."""
    from olano.magic_links import service
    from olano.magic_links import watch as watch_mod

    refusal = _capability_gate()
    if refusal:
        return refusal
    refusal = _owner_gate()
    if refusal:
        return _err(refusal)
    try:
        requested_by, notify = _request_origin()
    except RuntimeError as exc:
        return _err(str(exc))
    try:
        minted = service.mint_link(
            flow,
            params=params,
            requested_by=requested_by,
            notify=notify,
            minted_by=f"agent:{notify.get('agent_name', '')}",
        )
    except service.MagicLinkError as exc:
        return _err(
            str(exc),
            agent_guidance=(
                "Relay this to the user as-is. Do NOT try to work around it: "
                "never ask them to paste a secret into the chat, and never run "
                "an interactive auth CLI on the server — it has no browser and "
                "a localhost callback is unreachable from the user's phone."
            ),
        )
    watch_mod.spawn(minted.id)
    if _dashboard_chat_turn() and flow in service.INLINE_FORM_FLOWS:
        guidance = (
            "The dashboard chat and the mobile app render this as a secure "
            "form card right under your reply (no link to open, no PIN to "
            "type): do NOT paste the url or the pin into your reply; tell the "
            "user to paste the key into the card below. Then stop: this is "
            "non-blocking and you must NOT poll. I will post the result to "
            "this chat myself when they finish."
        )
    elif _dashboard_chat_turn():
        guidance = (
            "The dashboard chat renders this link as a Connect button card "
            "(with the PIN) on its own — do NOT paste the raw url into your "
            "reply; tell the user to click the button and mention the PIN. "
            "Then stop — this is non-blocking and you must NOT poll. I will "
            "post the result to this chat myself when they finish."
        )
    else:
        guidance = (
            "Send the user the url AND the pin, then stop — this is "
            "non-blocking and you must NOT poll. I will post the result to "
            "this chat myself when they finish."
        )
    return _ok({**minted.public(), "agent_guidance": guidance})


def _dashboard_chat_turn() -> bool:
    """Whether this turn arrived from the dashboard chat (``channel == "api"``).

    The dashboard renders magic-link tool results as a Connect button card, so
    the guidance above flips between "point at the button" and "send the raw
    URL". Best-effort: no context reads as a non-dashboard surface.
    """
    try:
        from olano.security.request_context import get_request_context

        return get_request_context().channel == "api"
    except Exception:
        return False


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_connect_service(
    service_name: Annotated[
        str,
        "What to connect: a connector id ('github', 'microsoft365', 'shopify', "
        "'salesforce', 'hubspot', 'linkedin', 'reddit', 'quickbooks', 'docusign', "
        "'slack', 'notion', …), 'google', 'openai-codex', or an MCP server URL. "
        "self_find_integration tells you which.",
    ],
    agent: Annotated[
        str,
        "Agent the connection belongs to. Empty = deployment-wide. MCP servers always need one.",
    ] = "",
    method: Annotated[
        str,
        "How to sign in: 'official_oauth' (Olano's app), 'byo_oauth' (the operator's "
        "own OAuth app), 'device' (a code typed on a phone — no app needed), or 'key' "
        "(a pasted key, last resort). Empty = the best one available on this "
        "deployment, which self_find_integration reports as auth.best.",
    ] = "",
) -> str:
    """Send the user a secure link to sign in to a service, from any chat.

    Use this whenever a tool is missing credentials or the user asks to connect
    an account. The server has no browser, so THIS is how sign-in happens — never
    ask the user to run a command, never run an interactive auth CLI yourself,
    and never ask them to paste a token into the chat. The connection lands in
    the one credential store, so the service's built-in tools, MCP servers and
    the git tools all use it on their next call — nothing to restart.

    Non-blocking: returns a `url` + `pin` to send the user, then your turn ends.
    Do NOT poll — a background watcher posts the result back to this chat.
    """
    try:
        from olano.magic_links.routing import resolve_connect_flow

        try:
            flow, params = resolve_connect_flow(
                service_name, agent=(agent or "").strip(), method=method or None
            )
        except ValueError as exc:
            return _err(str(exc))
        return _mint(flow, params)
    except Exception as exc:  # a tool must never raise into the turn
        logger.exception("magic_link_connect_service failed")
        return _err(f"Could not create the magic link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_request_secret(
    name: Annotated[
        str,
        "Credential name to store, e.g. STRIPE_API_KEY. Leave empty when using "
        "`provider` to connect a model provider.",
    ] = "",
    reason: Annotated[str, "One short sentence on why you need it (shown on the form)."] = "",
    provider: Annotated[
        str,
        "Model-provider key instead of a named credential: 'anthropic', 'openai', "
        "'groq', 'deepseek', … Stored under that provider's own env-var name.",
    ] = "",
    agent: Annotated[str, "Store it for this agent only. Empty = shared by every agent."] = "",
) -> str:
    """Send the user a secure form to hand over an API key — never ask for it in chat.

    A key pasted into a conversation is stored in the transcript, sent through
    the messaging platform, and put in your context. Use this instead: the user
    types the value into a page served over HTTPS and it goes straight into the
    encrypted vault. You never see it — you only learn the name.

    Non-blocking: returns a `url` + `pin` to send the user. Do NOT poll.
    """
    try:
        agent_name = (agent or "").strip()
        if (provider or "").strip():
            return _mint("provider-key", {"provider": provider.strip(), "agent": agent_name})
        if not (name or "").strip():
            return _err("Give either a credential `name` or a model `provider`.")
        return _mint(
            "secret-entry",
            {"name": name.strip(), "reason": (reason or "").strip(), "agent": agent_name},
        )
    except Exception as exc:
        logger.exception("magic_link_request_secret failed")
        return _err(f"Could not create the secure form link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_edit_instructions(
    agent: Annotated[
        str, "Agent whose instructions to edit. Defaults to you (the agent in this chat)."
    ] = "",
) -> str:
    """Send the owner a secure one-page editor for an agent's standing instructions.

    Use this when the owner wants to change your system prompt / persona rules
    and a chat message isn't enough (long rewrites, careful wording). The page
    shows the current instructions in an editable text box; saving validates and
    applies them to the running agent — no dashboard, no restart.

    Non-blocking: returns a `url` + `pin` to send the owner, then your turn
    ends. Do NOT poll — a background watcher posts the result back to this chat.
    """
    try:
        agent_name = (agent or "").strip()
        if not agent_name:
            try:
                agent_name = _sending_agent()
            except RuntimeError:
                return _err("Say which agent's instructions to edit.")
        return _mint("edit-prompt", {"agent": agent_name})
    except Exception as exc:
        logger.exception("magic_link_edit_instructions failed")
        return _err(f"Could not create the editor link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_request_file_edit(
    path: Annotated[
        str,
        "File to edit. A path inside the agent's workspace (e.g. 'config/settings.yaml'), "
        "or an absolute path — absolute paths are admin-only.",
    ],
    agent: Annotated[
        str, "Agent whose workspace the path is in. Defaults to you (the agent in this chat)."
    ] = "",
) -> str:
    """Send the owner a secure one-page editor for a file.

    Use this when a human needs to change a file you cannot decide for them —
    a config file, a `.env`, a checklist — and they have chat but no server
    access. The page shows the file's current contents in an editable box;
    saving replaces the file with what they submit.

    The file's contents are shown to whoever opens the link, so only send it to
    the person who is meant to see that file.

    Non-blocking: returns a `url` + `pin` to send the owner, then your turn
    ends. Do NOT poll — a background watcher posts the result back to this chat.
    """
    try:
        target = (path or "").strip()
        if not target:
            return _err("Say which file to edit.")
        agent_name = (agent or "").strip()
        if not agent_name:
            try:
                agent_name = _sending_agent()
            except RuntimeError:
                agent_name = ""
        return _mint("edit-file", {"agent": agent_name, "path": target})
    except Exception as exc:
        logger.exception("magic_link_request_file_edit failed")
        return _err(f"Could not create the editor link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_connect_channels(
    platforms: Annotated[
        str,
        "Which messengers the setup page should offer, comma-separated: "
        "'whatsapp', 'wechat' (the personal messenger; WeCom / Enterprise WeChat is "
        "not a QR platform, wire it with self_connect_platform), 'telegram', "
        "'discord', 'slack'. e.g. 'whatsapp, telegram'.",
    ],
    agent: Annotated[
        str, "Agent to connect the channels to. Defaults to you (the agent in this chat)."
    ] = "",
) -> str:
    """Send the owner a magic link to connect messengers to an agent — QR + tokens.

    Use this to onboard an agent onto WhatsApp / WeChat / Telegram / Discord /
    Slack without the owner touching the dashboard. The one page shows a button
    per platform: WhatsApp and WeChat pair by scanning a QR (or a phone-number
    code); Telegram/Discord/Slack take a bot token that is validated live and
    stored in the vault. Each connection is written to the agent's config and
    the agent restarts to go live when the owner taps Finish.

    Non-blocking: returns a `url` + `pin` to send the owner, then your turn ends.
    Do NOT poll — a background watcher posts the result back to this chat.
    """
    try:
        agent_name = (agent or "").strip()
        if not agent_name:
            try:
                agent_name = _sending_agent()
            except RuntimeError:
                return _err("Say which agent to connect the channels to.")
        picked = [
            p.strip().lower() for p in str(platforms or "").replace(",", " ").split() if p.strip()
        ]
        if not picked:
            return _err(
                "Say which messengers to offer, e.g. 'whatsapp, telegram'. "
                "Available: whatsapp, wechat, telegram, discord, slack."
            )
        return _mint("channel-connect", {"agent": agent_name, "platforms": picked})
    except Exception as exc:
        logger.exception("magic_link_connect_channels failed")
        return _err(f"Could not create the channel setup link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_open_dashboard(
    section: Annotated[
        str,
        "Where to land: 'prompt' (this agent's system prompt), 'vault', 'connections', "
        "'providers', 'config', 'files', or 'overview'. Default overview.",
    ] = "overview",
    agent: Annotated[
        str, "Agent whose config to open when section='prompt'. Defaults to you."
    ] = "",
) -> str:
    """Send the owner a one-time link that signs them into the dashboard on their phone.

    Use this when the owner wants to change something that lives in the dashboard —
    the system prompt, model providers, vault keys, connections — and they're in
    chat with no browser session. The link opens the real dashboard, focused on
    the section you name; the first time they'll enter their dashboard password
    once, after that it's a single tap.

    Non-blocking: returns a `url` + `pin` to send the owner. Do NOT poll.
    Requires the owner-login capability to be enabled by the operator.
    """
    try:
        sec = (section or "overview").strip().lower()
        params: dict[str, Any] = {"section": sec}
        agent_name = (agent or "").strip()
        # "prompt" opens an agent's config — default to the agent sending the
        # link (the one the owner is chatting with) when none is named.
        if not agent_name and sec in ("prompt", "agent", "identity"):
            try:
                agent_name = _sending_agent()
            except RuntimeError:
                agent_name = ""
        if agent_name:
            params["agent"] = agent_name
        return _mint("owner-login", params)
    except Exception as exc:
        logger.exception("magic_link_open_dashboard failed")
        return _err(f"Could not create the dashboard magic link: {exc}")


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
def magic_link_mint_access_token(
    label: Annotated[
        str,
        "Which integration the token is for, e.g. 'Claude desktop — Ana' or 'Zapier orders'. "
        "Prefilled on the page; the admin can change it.",
    ] = "",
    agent: Annotated[
        str,
        "Agent the token may reach. Empty = the admin picks on the page (any agent published "
        "for external access, or every exposed agent).",
    ] = "",
) -> str:
    """Send an ADMIN a secure page that mints a bearer token for outside callers.

    The token lets an external platform (Claude, ChatGPT, Cursor, another agent)
    send messages to an agent over MCP or A2A. It is shown once, on the page,
    and never in this chat — you only learn its label and id when the admin
    finishes. Refused unless the person on this turn is an administrator.

    Non-blocking: returns a `url` + `pin` to send the admin. Do NOT poll.
    """
    from olano.tools._human_gate import admin_human_denial

    denial = admin_human_denial()
    if denial:
        return denial
    try:
        return _mint(
            "access-token", {"label": (label or "").strip(), "agent": (agent or "").strip()}
        )
    except Exception as exc:
        logger.exception("magic_link_mint_access_token failed")
        return _err(f"Could not create the token-minting link: {exc}")


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def magic_link_status(
    link_id: Annotated[str, "The `id` returned when the link was created."] = "",
) -> str:
    """Check a magic link you sent (optional — completion is reported to you).

    Only useful if the user says they finished and you want to confirm early.
    Without a `link_id` it lists the links still waiting.
    """
    refusal = _capability_gate()
    if refusal:
        return refusal
    try:
        from olano.magic_links import service, store

        if not (link_id or "").strip():
            return _ok({"pending": service.list_links(include_terminal=False)})
        record = store.get(link_id.strip())
        if record is None:
            return _err(f"No magic link with id '{link_id}'.")
        return _ok(record.public())
    except Exception as exc:
        logger.exception("magic_link_status failed")
        return _err(f"Could not read the link status: {exc}")


_TOOLS = [
    magic_link_connect_service,
    magic_link_request_secret,
    magic_link_edit_instructions,
    magic_link_request_file_edit,
    magic_link_connect_channels,
    magic_link_open_dashboard,
    magic_link_mint_access_token,
    magic_link_status,
]


def get_tools(names: list[str] | None = None) -> list[Any]:
    if names is None:
        return list(_TOOLS)
    wanted = set(names)
    return [t for t in _TOOLS if t.name in wanted]


def get_tool(name: str) -> Any | None:
    return next((t for t in _TOOLS if t.name == name), None)
