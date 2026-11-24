"""Identity-injected connection tools for multi-user agents.

Six tools, all resolving the CALLER from the authenticated request context —
no tool takes a platform id anywhere. The recipient space of
``send_to_connection`` is only the caller's own consented connections, every
delivery goes through ``deliver_text`` (which re-checks the peer's mute/block
flags), and each direction of each connection has a daily send cap.

Peer platform ids are never shown to the other side: each side sees only the
display labels chosen at invite/accept time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2000
MAX_INVITE_USES = 500
_MAX_LABEL = 60


def _clean_label(text: str, fallback: str = "") -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:_MAX_LABEL] or fallback


def _agent_contact_hint(agent_name: str) -> str:
    """Best-effort 'how to reach this agent' line from live channel adapters.

    Only states what an adapter actually knows about itself; anything else
    returns "" and the invite tells the inviter to add contact details when
    forwarding. Never raises.
    """
    hints: list[str] = []
    try:
        from olano.platforms import get_channel_registry

        registry = get_channel_registry()
        adapters: dict[str, Any] = getattr(registry, "adapters_for", lambda _n: {})(agent_name)
        for channel_type, adapter in adapters.items():
            try:
                if channel_type == "telegram":
                    username = getattr(getattr(adapter, "_bot", None), "username", None)
                    if username:
                        hints.append(f"Telegram: @{str(username).lstrip('@')}")
                elif channel_type == "whatsapp":
                    jid = getattr(adapter, "_paired_jid", None)
                    if jid:
                        number = str(jid).partition("@")[0]
                        if number:
                            hints.append(f"WhatsApp: +{number.lstrip('+')}")
            except Exception:
                continue
    except Exception:
        return ""
    return " · ".join(hints)


def build_invite_message(
    *,
    agent_label: str,
    introduce_as: str,
    code: str,
    expires_at: str,
    max_uses: int,
    contact: str,
) -> str:
    """The ready-to-forward text the inviter passes on, ending in the literal
    ``connect <CODE>`` line the recipient sends back to the agent."""
    reach = f" ({contact})" if contact else ""
    uses = "It can be used once." if max_uses == 1 else f"It can be used {max_uses} times."
    return (
        f"👋 {introduce_as} would like to connect with you through {agent_label}.\n"
        f"\n"
        f"To accept, open a chat with {agent_label}{reach} and send exactly this line:\n"
        f"\n"
        f"connect {code}\n"
        f"\n"
        f"This invite expires {expires_at[:10]}. {uses} "
        f"If you'd rather not connect, just ignore this message."
    )


def _deliver_now(agent_name: str, platform: str, user_id: str, text: str) -> bool:
    """Run the async platform delivery from a (typically thread-pooled) sync
    tool. Best-effort: False on any failure, never raises."""
    from olano.scheduler.multi_user import deliver_text

    coro = deliver_text(agent_name, platform, user_id, text)
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        registry_loop = None
        try:
            from olano.platforms import get_channel_registry

            registry_loop = get_channel_registry().loop
        except Exception:
            pass
        if registry_loop is not None and registry_loop is not running:
            future = asyncio.run_coroutine_threadsafe(coro, registry_loop)
            return bool(future.result(timeout=30))
        if running is None:
            return bool(asyncio.run(coro))
        # We are ON the delivery loop inside a sync tool: cannot block on it.
        running.create_task(coro)
        return True
    except Exception:
        logger.warning("Connection delivery failed for agent '%s'", agent_name, exc_info=True)
        try:
            coro.close()
        except Exception:
            pass
        return False


def make_customer_connection_tools(agent_name: str, config: Any) -> list:
    """Build the six connection tools for one agent (customer_service.connections)."""
    from langchain_core.tools import tool

    from olano.connections.store import ConnectionsStore
    from olano.memory.customer_memory import (
        active_customer_id,
        customers_base,
        load_customer_identity,
    )

    store = ConnectionsStore(agent_name)
    cs = config.customer_service
    agent_label = str(getattr(config, "label", "") or agent_name)
    no_customer = (
        "No identified customer in this conversation, so there are no connections to act on."
    )

    def _peer_side(row, me: str) -> tuple[str, str, str]:
        """(peer_key, peer's-name-as-I-see-it, my-name-as-peer-sees-it)."""
        if row["inviter_key"] == me:
            return row["peer_key"], row["inviter_label"], row["peer_label"]
        return row["inviter_key"], row["peer_label"], row["inviter_label"]

    @tool
    def connection_invite(introduce_as: str, label: str = "", uses: int = 1) -> str:
        """Create an invite another person can accept to CONNECT with the current customer.

        Returns a ready-to-forward message the customer copies and sends to
        the other person themselves (WhatsApp, email, anywhere); the other
        person accepts by sending its `connect <CODE>` line to this agent.
        `introduce_as` is how the recipient will see the inviter (e.g. "Sarah,
        your landlord"); `label` is the inviter's own name for the recipient
        ("tenant unit 3") — used to address messages later; `uses` above 1
        makes one standing code many people can redeem (e.g. a business
        inviting all its customers). Never ask for anyone's phone number or
        chat id — the code is the whole mechanism.
        """
        me = active_customer_id()
        if not me:
            return no_customer
        introduce_as = _clean_label(introduce_as, "A customer")
        label = _clean_label(label)
        uses = max(1, min(int(uses), MAX_INVITE_USES))
        code, expires_at = store.create_invite(
            me,
            label=label,
            introduce_as=introduce_as,
            max_uses=uses,
            ttl_hours=int(getattr(cs, "connection_invite_ttl_hours", 168)),
        )
        contact = str(getattr(cs, "connection_contact_info", "") or "") or _agent_contact_hint(
            agent_name
        )
        message = build_invite_message(
            agent_label=agent_label,
            introduce_as=introduce_as,
            code=code,
            expires_at=expires_at,
            max_uses=uses,
            contact=contact,
        )
        note = (
            ""
            if contact
            else (
                "\n\nNote for you (not part of the invite): I don't know my own "
                "public address on this platform, so tell the recipient how to "
                "reach me when you forward this — e.g. my bot handle or number."
            )
        )
        return (
            "Here is the invite — forward the block below to the person you want "
            f"to connect with. Their side of it is labeled '{label or code}' for you.\n\n"
            f"{message}{note}"
        )

    @tool
    def connection_accept(code: str, your_name: str = "") -> str:
        """Accept a connection invite the current customer received (they sent 'connect <CODE>').

        Call when a customer pastes an invite or sends a line like
        `connect ABCD-2345`. `your_name` is optional — how the inviter will
        see them (never their chat id; ids are never shared). Accepting lets
        the two exchange messages THROUGH this agent; either side can
        disconnect at any time.
        """
        me = active_customer_id()
        if not me:
            return no_customer
        status, row = store.redeem(
            code,
            me,
            your_name=_clean_label(your_name),
            max_connections=int(getattr(cs, "max_connections_per_user", 25)),
        )
        if status == "expired":
            return "That invite has expired — ask the person who sent it for a fresh one."
        if status == "self":
            return "That is this customer's own invite; it is meant for someone else to accept."
        if status == "already_connected":
            return "These two are already connected — nothing to do."
        if status == "cap":
            return "Connection limit reached — disconnect an old connection first."
        if status != "ok" or row is None:
            return (
                "That code does not match any open invite (it may be mistyped, "
                "already used, or withdrawn)."
            )
        # Tell the inviter their invite was accepted — through the platform
        # router, so their mute/block preferences still apply.
        inviter_folder = customers_base(agent_name) / row["inviter_key"]
        identity = load_customer_identity(inviter_folder)
        if identity is not None:
            _deliver_now(
                agent_name,
                identity[0],
                identity[1],
                f"✅ '{row['inviter_label']}' accepted your {agent_label} invite. "
                f"You can now send them messages — just tell me what to send to "
                f"'{row['inviter_label']}'.",
            )
        return (
            f"Connected with {row['peer_label']}. They can now exchange messages "
            f"with this customer through me — either side can say 'disconnect "
            f"{row['peer_label']}' at any time to stop."
        )

    @tool
    def my_connections() -> str:
        """List the current customer's connections and open invites (theirs only)."""
        me = active_customer_id()
        if not me:
            return no_customer
        lines: list[str] = []
        for row in store.connections_for(me):
            _, their_name, _ = _peer_side(row, me)
            lines.append(f"- #{row['id']} {their_name} (connected {row['created_at'][:10]})")
        for inv in store.invites_for(me):
            left = inv["max_uses"] - inv["uses"]
            lines.append(
                f"- open invite {inv['code']}"
                + (f" '{inv['label']}'" if inv["label"] else "")
                + f" — {left} use(s) left, expires {inv['expires_at'][:10]}"
            )
        return "\n".join(lines) if lines else "No connections or open invites yet."

    @tool
    def send_to_connection(connection: str, message: str) -> str:
        """Send a short message to ONE of the current customer's own connections.

        `connection` is the label or #id from my_connections — never a chat id
        or phone number; only consented connections are reachable. The text is
        relayed with clear sender attribution, counts against a daily
        per-connection cap, and is refused if the two are no longer connected.
        """
        me = active_customer_id()
        if not me:
            return no_customer
        text = (message or "").strip()
        if not text:
            return "There is no message text to send."
        if len(text) > MAX_MESSAGE_CHARS:
            return f"Message too long ({len(text)} chars; the limit is {MAX_MESSAGE_CHARS})."
        row = store.find_connection(me, connection)
        if row is None:
            return (
                f"No connection matches {connection!r} — my_connections lists the "
                "valid labels and ids. If several share a label, use the #id."
            )
        peer_key, their_name, my_name = _peer_side(row, me)
        cap = int(getattr(cs, "max_connection_sends_per_day", 20))
        if store.sends_today(row["id"], me) >= cap:
            return f"Daily limit reached for '{their_name}' ({cap} messages) — try again tomorrow."
        identity = load_customer_identity(customers_base(agent_name) / peer_key)
        if identity is None:
            return (
                f"'{their_name}' is no longer reachable (their account was removed). "
                "You may want to disconnect this connection."
            )
        framed = (
            f"💬 Message from {my_name} (via {agent_label}):\n\n{text}\n\n"
            f"— Reply here and I can relay an answer back; say "
            f"'disconnect {my_name}' to stop messages from them."
        )
        if not _deliver_now(agent_name, identity[0], identity[1], framed):
            return f"Could not deliver to '{their_name}' right now."
        store.record_send(row["id"], me)
        return f"Delivered to '{their_name}'."

    @tool
    def disconnect_connection(connection: str) -> str:
        """Sever one of the current customer's connections (label or #id from my_connections).

        Either side may disconnect at any time; after that, neither can send
        to the other unless a new invite is accepted.
        """
        me = active_customer_id()
        if not me:
            return no_customer
        row = store.find_connection(me, connection)
        if row is None:
            return f"No connection matches {connection!r} — see my_connections."
        _, their_name, _ = _peer_side(row, me)
        store.disconnect(me, row["id"])
        return f"Disconnected from '{their_name}'. No further messages can flow either way."

    @tool
    def revoke_connection_invite(code: str) -> str:
        """Withdraw one of the current customer's own open invites so its code stops working.

        Use when a code was shared too widely or sent to the wrong person.
        Existing connections are unaffected — disconnect those individually.
        """
        me = active_customer_id()
        if not me:
            return no_customer
        if store.revoke_invite(me, code):
            return "Invite withdrawn — the code no longer works."
        return "No open invite of theirs matches that code."

    return [
        connection_invite,
        connection_accept,
        my_connections,
        send_to_connection,
        disconnect_connection,
        revoke_connection_invite,
    ]
