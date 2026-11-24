"""Answer an agent's approval requests by replying in the chat.

Gated tool calls (sending an email, a payment, a shell command, per the
agent's ``security`` profile) need a human decision. On a messenger that
decision used to have two outcomes: a sender on the channel's owner list was
**auto-approved** without being asked, and anyone else's request waited in the
dashboard's Approvals queue. Neither suits a principal who runs the workflow
from their phone and must see, and decide, each consequential action.

With ``chat_approvals`` switched on for a channel (``channels.<platform>
.chat_approvals: true``), an owner's turn that reaches a gated action posts a
prompt in the same chat::

    Approval needed (code 4F2A)
    Action: gmail_send
    to: chen.yi@example.com
    subject: GCF1 trades 22 Sep
    Reply YES 4F2A to approve or NO 4F2A to reject. Expires in 5 min.

and waits. The reply is intercepted by
:meth:`BasePlatformAdapter.handle_message` **before** the busy-session logic,
because the conversation is busy (its turn is the one waiting) and any other
message there would interrupt it. Works on every text platform; buttons are
not needed, which is the point for WhatsApp.

Rules that keep it safe:

- the code is required, so a stray "yes" never approves anything, and several
  prompts in one chat stay distinct;
- only the person whose message started the turn can answer (matched on the
  stable identifiers the trust check uses, never a display name);
- no answer before the agent's ``security.hitl_timeout_seconds`` rejects;
- a prompt that cannot be delivered rejects (fail closed);
- every outcome is written to the approval record (``security/hitl.py``)
  with who decided and through which platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# "YES 4F2A", "approve 4f2a", "no 4F2A.": a verdict word, then the code.
_REPLY_RE = re.compile(
    r"^\s*(?P<verdict>yes|y|approve|approved|ok|no|n|reject|rejected|deny)\s+"
    r"(?P<code>[A-Za-z0-9]{4})\s*[.!]?\s*$",
    re.IGNORECASE,
)
_APPROVE_WORDS = frozenset({"yes", "y", "approve", "approved", "ok"})
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I lookalikes
_ARG_PREVIEW_CHARS = 300
_ARGS_MAX_LINES = 8


@dataclass
class _Pending:
    future: asyncio.Future[tuple[str, str]]
    approvers: frozenset[str]
    agent: str
    action: str


# (id(adapter), chat_id, CODE) -> pending prompt. Keyed per adapter instance
# because each agent owns its own adapter, so two agents in one chat (a group)
# never answer each other's prompts.
_pending: dict[tuple[int, str, str], _Pending] = {}


def chat_approvals_enabled(extra: dict[str, Any] | None) -> bool:
    """Whether a channel's config switches chat approvals on."""
    raw = (extra or {}).get("chat_approvals")
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def phone_digits(value: str) -> str:
    """The phone number in a WhatsApp JID or a typed phone entry, as digits.

    ``6591234567@s.whatsapp.net``, ``6591234567:7@s.whatsapp.net``,
    ``6591234567@c.us``, Twilio's ``whatsapp:+6591234567``, the Meta Cloud API's
    ``+6591234567`` and ``+65 9123-4567`` all give ``6591234567``. Anything
    else (an ``@lid`` id, a Telegram handle) gives ``""``, so a phone match can
    never be made out of an identifier that is not a phone number.
    """
    s = str(value or "").strip().lower().removeprefix("whatsapp:")
    if s.endswith(("@s.whatsapp.net", "@c.us")):
        local = s.split("@", 1)[0].split(":", 1)[0]
        return local if local.isdigit() else ""
    if s and re.fullmatch(r"\+?[\d\s\-().]{6,}", s):
        return re.sub(r"\D", "", s)
    return ""


def sender_identities(user_id: str, username: str = "", phone: str = "") -> frozenset[str]:
    """The stable identifiers one sender is known by, normalized for matching."""
    ids = {
        str(user_id or "").strip().lstrip("@").lower(),
        str(username or "").strip().lstrip("@").lower(),
    }
    digits = phone_digits(phone) or phone_digits(user_id)
    if digits:
        ids.add(f"tel:{digits}")
    return frozenset(i for i in ids if i)


def _new_code(adapter: Any, chat_id: str) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
        if (id(adapter), chat_id, code) not in _pending:
            return code
    return secrets.token_hex(2).upper()  # pragma: no cover - 32^4 codes, ~never


def _format_args(args: Any) -> list[str]:
    if isinstance(args, dict):
        lines = []
        for key, value in list(args.items())[:_ARGS_MAX_LINES]:
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = " ".join(str(text).split())
            if len(text) > _ARG_PREVIEW_CHARS:
                text = text[:_ARG_PREVIEW_CHARS] + "…"
            lines.append(f"{key}: {text}")
        if len(args) > _ARGS_MAX_LINES:
            lines.append(f"(+{len(args) - _ARGS_MAX_LINES} more)")
        return lines
    text = " ".join(str(args or "").split())
    return [text[:_ARG_PREVIEW_CHARS]] if text else []


def render_prompt(action: dict[str, Any], code: str, timeout_seconds: int) -> str:
    from olano.security.hitl import redact_args

    minutes = max(1, round(timeout_seconds / 60))
    name = str(action.get("name") or "tool")
    lines = [
        f"Approval needed (code {code})",
        f"Action: {name}",
        # A chat message lives on a phone and in the messenger's servers:
        # never put a credential in it, even one the owner is about to store.
        *_format_args(redact_args(name, action.get("args"))),
        "",
        f"Reply YES {code} to approve or NO {code} to reject. Expires in {minutes} min.",
    ]
    return "\n".join(lines)


def make_chat_approval_handler(
    adapter: Any,
    *,
    agent_name: str,
    platform: str,
    chat_id: str,
    approvers: Iterable[str],
    timeout_seconds: int = 300,
    send: Callable[[str, str], Awaitable[Any]] | None = None,
) -> Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]:
    """Build the runner's ``approval_handler`` for one chat turn.

    ``approvers`` are the :func:`sender_identities` of the person whose
    message started the turn. ``send(chat_id, text)`` defaults to the
    adapter's own send.
    """
    approver_ids = frozenset(approvers)
    deliver = send or (lambda cid, text: adapter.send(cid, text))
    timeout = max(1, int(timeout_seconds or 300))

    async def handle(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from olano.security import hitl

        decisions: list[dict[str, Any]] = []
        for action in actions:
            name = str(action.get("name") or "tool")
            code = _new_code(adapter, chat_id)
            key = (id(adapter), chat_id, code)
            future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
            _pending[key] = _Pending(future, approver_ids, agent_name, name)
            verdict, who, via, note = "reject", "system", "timeout", "no answer in time"
            try:
                result = await deliver(chat_id, render_prompt(action, code, timeout))
                if getattr(result, "success", True) is False:
                    raise RuntimeError(getattr(result, "error", "") or "send failed")
                try:
                    verdict, who = await asyncio.wait_for(future, timeout)
                    via, note = platform, ""
                except TimeoutError:
                    try:
                        await deliver(chat_id, f"Approval {code} expired. {name} was not run.")
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("[%s] chat approval prompt failed: %s", agent_name, exc)
                via, note = "undeliverable", f"prompt not delivered: {exc}"
            finally:
                _pending.pop(key, None)

            approved = verdict == "approve"
            hitl.record_decision(
                agent_name=agent_name,
                action=name,
                approved=approved,
                decided_by=f"{platform}:{who}" if via == platform else "system",
                via=via,
                note=note,
                args=action.get("args"),
                risk=str(action.get("risk") or ""),
            )
            if approved:
                decisions.append({"type": "approve"})
            else:
                reason = "the person asked rejected it" if via == platform else note
                decisions.append({"type": "reject", "message": f"Not approved: {reason}."})
        return decisions

    return handle


def maybe_resolve_reply(adapter: Any, event: Any) -> str | None:
    """Resolve a pending approval from an inbound chat reply.

    Returns ``None`` when the message is not an answer to a pending prompt in
    this chat (it then proceeds as an ordinary message), otherwise the short
    acknowledgement to send back (possibly a refusal).
    """
    match = _REPLY_RE.match(getattr(event, "text", "") or "")
    if not match:
        return None
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    code = match.group("code").upper()
    pending = _pending.get((id(adapter), chat_id, code))
    if pending is None:
        return None
    if pending.future.done():
        return f"Approval {code} was already answered."
    user_id = str(getattr(source, "user_id", "") or "")
    who = sender_identities(
        user_id,
        str(getattr(source, "username", "") or ""),
        str(getattr(source, "user_id_alt", "") or ""),
    )
    if not (who & pending.approvers):
        return f"Approval {code} can only be answered by the person who asked for it."
    approve = match.group("verdict").lower() in _APPROVE_WORDS
    pending.future.set_result(("approve" if approve else "reject", user_id or "unknown"))
    if approve:
        return f"Approved ({code}). Running {pending.action}."
    return f"Rejected ({code}). {pending.action} will not run."


def pending_count() -> int:
    """How many chat approval prompts are waiting (for diagnostics and tests)."""
    return len(_pending)
