"""What the runtime tells phones, and whose phones it tells.

Every function here is a producer hook: called from the place the event happens,
never raises, and returns at once (the dispatcher sends on its own thread). Each
is a no-op unless the runtime server started the dispatcher, so the same code
runs harmlessly in the CLI and the test suite.

Audiences:

* A reply or a mid-turn approval goes to **the person who started the turn**.
* An inbox approval or a Cortex item goes to **everyone who may act on that
  agent**: every admin, plus members granted the agent. That is the same rule
  ``POST /api/hitl/resolve`` and the Pulse routes enforce, so nobody is woken
  up for something they cannot do anything about.
* A bell notification goes to **admins**: it is about the deployment.

Payload contract (read by the app, ``mobile/src/native/push.ts``):
``data`` always carries ``kind`` and, where relevant, ``agent``,
``thread_id`` and ``item_id``, plus ``host`` when the box knows its public URL,
so a phone signed in to several deployments opens the right one. An approval
also carries ``count`` (how many decisions it takes), and is sent with the
``approval`` category, whose Approve / Decline buttons the app registers,
unless one of its actions is high risk.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from olano.push.dispatcher import PushMessage, get_push_dispatcher

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 160

_PRIORITY_RANK = {"low": 0, "normal": 1, "urgent": 2}
_LEVEL_RANK = {"info": 0, "success": 0, "warn": 1, "error": 2}


# ── audiences ────────────────────────────────────────────────────────────────


def _multi_user() -> bool:
    from olano.runtime import auth as auth_layer

    return auth_layer.multi_user_enabled()


def agent_audience(agent: str) -> set[str]:
    """User ids that may act on ``agent``: active admins plus granted members."""
    if not _multi_user():
        return {"owner"}
    from olano.users.store import get_user_store

    store = get_user_store()
    active = {u.id: u for u in store.db.list_users() if u.is_active}
    ids = {uid for uid, u in active.items() if u.is_admin}
    for row in store.db.list_users_for_agent(agent):
        if row.get("user_id") in active:
            ids.add(row["user_id"])
    return ids


def admin_audience() -> set[str]:
    if not _multi_user():
        return {"owner"}
    from olano.users.store import get_user_store

    return {u.id for u in get_user_store().db.list_users() if u.is_active and u.is_admin}


# ── helpers ──────────────────────────────────────────────────────────────────


def _public_host() -> str:
    raw = os.environ.get("LIONCLAW_PUBLIC_URL") or os.environ.get("OLANO_PUBLIC_URL") or ""
    try:
        return urlparse(raw.strip()).hostname or ""
    except ValueError:
        return ""


def _agent_label(agent: str) -> str:
    """The agent's display name when the runtime knows it, else its handle."""
    try:
        from olano.config.loader import load_agent_config

        cfg = load_agent_config(agent)
        label = str(getattr(cfg, "display_name", "") or "").strip()
        return label or agent
    except Exception:
        return agent


def _preview(text: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[: PREVIEW_CHARS - 1].rstrip() + "…"


def _settings() -> Any:
    from olano.config.system_settings import load_push_settings

    return load_push_settings()


def _data(kind: str, **extra: str) -> dict[str, str]:
    out = {"kind": kind}
    host = _public_host()
    if host:
        out["host"] = host
    out.update({k: v for k, v in extra.items() if v})
    return out


def _submit(message: PushMessage) -> bool:
    dispatcher = get_push_dispatcher(start=False)
    if dispatcher is None or not message.devices:
        return False
    return dispatcher.submit(message)


def _devices(user_ids: set[str], kind: str) -> list[Any]:
    from olano.push.store import get_device_store

    if not user_ids:
        return []
    return get_device_store().for_users(user_ids, kind)


def _active() -> bool:
    return get_push_dispatcher(start=False) is not None


# ── producers ────────────────────────────────────────────────────────────────


def reply_ready(
    *, user_id: str, agent: str, thread_id: str, text: str = "", failed: bool = False
) -> bool:
    """A dashboard turn finished while nobody was watching it."""
    if not _active():
        return False
    try:
        devices = _devices({user_id}, "replies")
        if not devices:
            return False
        settings = _settings()
        label = _agent_label(agent)
        if failed:
            body = "Ran into a problem with your request. Tap to see what happened."
        elif settings.previews and text.strip():
            body = _preview(text)
        else:
            body = "Replied to your message."
        return _submit(
            PushMessage(
                devices=devices,
                title=label,
                body=body,
                data=_data("reply", agent=agent, thread_id=thread_id),
                collapse_key=f"reply:{agent}:{thread_id}"[:64],
                thread_id=f"{agent}:{thread_id}"[:64],
                channel="replies",
                # A Reply field on the notification: answering it continues
                # this conversation without opening the app. A failed turn
                # wants a look first, so it gets none.
                category="" if failed else "reply",
            )
        )
    except Exception:
        logger.debug("push: reply_ready failed", exc_info=True)
        return False


def _approval_category(risks: list[str]) -> str:
    """Approve / Decline buttons on the notification, unless something is high risk.

    A high-risk action (a payment, a send, a browser purchase) opens the app
    instead, where the person sees what exactly will run before deciding.
    """
    return "" if any(r.strip().lower() == "high" for r in risks) else "approval"


def chat_approval_needed(
    *, user_id: str, agent: str, thread_id: str, actions: list[dict[str, Any]]
) -> bool:
    """A turn the person started is paused on their approval."""
    if not _active():
        return False
    try:
        devices = _devices({user_id}, "approvals")
        if not devices:
            return False
        names = [str(a.get("name") or "an action") for a in actions or []]
        body = f"Wants to run {names[0] if names else 'an action'}"
        if len(names) > 1:
            body += f" and {len(names) - 1} more"
        body += ". Approve or reject it in the app."
        return _submit(
            PushMessage(
                devices=devices,
                title=f"{_agent_label(agent)} needs your approval",
                body=body,
                # `count` is how many decisions /api/chat/approval expects.
                data=_data(
                    "approval", agent=agent, thread_id=thread_id, count=str(len(names) or 1)
                ),
                collapse_key=f"approval:{agent}:{thread_id}"[:64],
                thread_id=f"{agent}:{thread_id}"[:64],
                channel="approvals",
                category=_approval_category([str(a.get("risk") or "") for a in actions or []]),
            )
        )
    except Exception:
        logger.debug("push: chat_approval_needed failed", exc_info=True)
        return False


def inbox_approval_needed(*, request_id: str, agent: str, action: str, risk: str = "") -> bool:
    """A background or delegated turn is paused in the Approvals inbox."""
    if not _active():
        return False
    try:
        devices = _devices(agent_audience(agent), "approvals")
        if not devices:
            return False
        body = f"Wants to run {action or 'an action'}"
        if risk:
            body += f" ({risk} risk)"
        body += ". It waits in Approvals until someone decides."
        return _submit(
            PushMessage(
                devices=devices,
                title=f"{_agent_label(agent)} needs approval",
                body=body,
                data=_data("inbox_approval", agent=agent, item_id=request_id),
                collapse_key=f"inbox:{request_id}"[:64],
                channel="approvals",
                category=_approval_category([risk]),
            )
        )
    except Exception:
        logger.debug("push: inbox_approval_needed failed", exc_info=True)
        return False


def browser_check_needed(*, agent: str, site: str) -> bool:
    """The agent's browser is waiting for a person to pass a site's
    "verify you are human" check. Goes to everyone who can open the agent's
    chat, where the live browser is; names the site, never the page."""
    if not _active():
        return False
    try:
        devices = _devices(agent_audience(agent), "approvals")
        if not devices:
            return False
        return _submit(
            PushMessage(
                devices=devices,
                title=f"{_agent_label(agent)} needs you in its browser",
                body=f"{site or 'A site'} wants a person to confirm they are human. "
                "Open the chat and tick the box.",
                data=_data("browser_check", agent=agent),
                collapse_key=f"browser_check:{agent}"[:64],
                channel="approvals",
            )
        )
    except Exception:
        logger.debug("push: browser_check_needed failed", exc_info=True)
        return False


def pulse_item_added(*, agent: str, item: Any) -> bool:
    """Cortex produced a card. Pushed by lane and priority (see PushSettings)."""
    if not _active():
        return False
    try:
        lane = str(getattr(getattr(item, "lane_type", None), "value", "") or "")
        settings = _settings()
        if lane == "approval":
            title = f"{_agent_label(agent)} wants to make a change"
            fixed = "Cortex has a proposal waiting for your approval."
        elif lane == "pulse":
            level = str(getattr(getattr(item, "priority_level", None), "value", "normal"))
            if _PRIORITY_RANK.get(level, 1) < _PRIORITY_RANK.get(settings.cortex_min_priority, 2):
                return False
            title = f"{_agent_label(agent)} has a new finding"
            fixed = "Cortex found something for you to look at."
        else:
            # The ops lane (self-checks) reaches admins as bell alerts already.
            return False
        devices = _devices(agent_audience(agent), "cortex")
        if not devices:
            return False
        item_title = str(getattr(item, "title", "") or "").strip()
        body = _preview(item_title) if settings.previews and item_title else fixed
        item_id = str(getattr(item, "id", "") or "")
        return _submit(
            PushMessage(
                devices=devices,
                title=title,
                body=body,
                data=_data("pulse", agent=agent, item_id=item_id, lane=lane),
                collapse_key=f"pulse:{agent}:{item_id}"[:64],
                thread_id=f"cortex:{agent}"[:64],
                channel="cortex",
                priority="high" if lane == "approval" else "normal",
            )
        )
    except Exception:
        logger.debug("push: pulse_item_added failed", exc_info=True)
        return False


def operator_alert(*, notification: Any) -> bool:
    """A new bell notification at or above ``push.alert_min_level``."""
    if not _active():
        return False
    try:
        settings = _settings()
        level = str(getattr(notification, "level", "info") or "info")
        if _LEVEL_RANK.get(level, 0) < _LEVEL_RANK.get(settings.alert_min_level, 1):
            return False
        devices = _devices(admin_audience(), "alerts")
        if not devices:
            return False
        title = str(getattr(notification, "title", "") or "Something needs attention")
        body_text = str(getattr(notification, "body", "") or "")
        body = (
            _preview(body_text)
            if settings.previews and body_text
            else ("Open the app to see what needs attention.")
        )
        key = str(getattr(notification, "dedupe_key", "") or getattr(notification, "id", ""))
        agent = str(getattr(notification, "agent", "") or "")
        return _submit(
            PushMessage(
                devices=devices,
                title=title,
                body=body,
                data=_data("alert", agent=agent, item_id=str(getattr(notification, "id", ""))),
                collapse_key=f"alert:{key}"[:64],
                channel="alerts",
                priority="normal" if level == "warn" else "high",
                # A condition that keeps re-raising itself must not page a phone
                # every time: once per key per six hours is plenty.
                throttle_key=f"alert:{key}",
                throttle_seconds=6 * 3600,
            )
        )
    except Exception:
        logger.debug("push: operator_alert failed", exc_info=True)
        return False


def test_message(*, user_id: str) -> PushMessage:
    """The "Send a test notification" message for one person's phones."""
    from olano.push.store import get_device_store

    return PushMessage(
        devices=get_device_store().for_user(user_id),
        title="Olano",
        body="Notifications are working on this phone.",
        data=_data("test"),
        channel="alerts",
    )
