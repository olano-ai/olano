"""Approvals answered in the chat, and the record of who decided.

A principal running a workflow from WhatsApp must see and decide each gated
action (an email to the trader, a payment) without opening the dashboard. With
``chat_approvals`` on, an owner's turn posts a YES/NO prompt with a code and
waits; the reply is intercepted before the busy-session logic (the waiting turn
is what makes the chat busy). Every outcome, including the old auto-approval
and a timeout, lands in the append-only approval record.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

from olano.platforms import chat_approval
from olano.platforms.chat_approval import (
    chat_approvals_enabled,
    make_chat_approval_handler,
    maybe_resolve_reply,
    phone_digits,
    sender_identities,
)
from olano.security import hitl

PM = "6591234567@s.whatsapp.net"
CHAT = PM


@dataclass
class _Source:
    chat_id: str = CHAT
    user_id: str = PM
    username: str = ""
    user_id_alt: str = ""


@dataclass
class _Event:
    text: str
    source: _Source
    message_id: str = "m1"


class _Sent:
    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.fail = fail

    async def __call__(self, chat_id: str, text: str) -> Any:
        if self.fail:
            raise RuntimeError("bridge down")
        self.texts.append(text)
        return None


_ACTION = {
    "name": "gmail_send",
    "args": {"to": "chen.yi@example.com", "subject": "GCF1 trades 22 Sep"},
    "risk": "high",
}


async def _wait_for_prompt(sent: _Sent) -> str:
    for _ in range(100):
        if sent.texts:
            return re.search(r"code (\w{4})", sent.texts[0]).group(1)
        await asyncio.sleep(0.01)
    raise AssertionError("no prompt was sent")


async def test_owner_approves_by_replying_with_the_code() -> None:
    adapter = object()
    sent = _Sent()
    handler = make_chat_approval_handler(
        adapter,
        agent_name="desk",
        platform="whatsapp",
        chat_id=CHAT,
        approvers=sender_identities(PM),
        send=sent,
    )
    task = asyncio.create_task(handler([_ACTION]))
    code = await _wait_for_prompt(sent)

    prompt = sent.texts[0]
    assert "Action: gmail_send" in prompt
    assert "to: chen.yi@example.com" in prompt
    assert f"Reply YES {code} to approve or NO {code} to reject" in prompt

    # A bare "yes" is an ordinary message, not an answer.
    assert maybe_resolve_reply(adapter, _Event("yes", _Source())) is None
    ack = maybe_resolve_reply(adapter, _Event(f"yes {code.lower()}", _Source()))
    assert ack == f"Approved ({code}). Running gmail_send."
    assert await task == [{"type": "approve"}]

    [row] = hitl.read_audit_log()
    assert row["status"] == "approved"
    assert row["decided_by"] == f"whatsapp:{PM}"
    assert row["via"] == "whatsapp"
    assert row["args"]["to"] == "chen.yi@example.com"
    assert chat_approval.pending_count() == 0


async def test_rejection_and_a_stranger_in_the_group() -> None:
    adapter = object()
    sent = _Sent()
    handler = make_chat_approval_handler(
        adapter,
        agent_name="desk",
        platform="whatsapp",
        chat_id="120363@g.us",
        approvers=sender_identities(PM),
        send=sent,
    )
    task = asyncio.create_task(handler([_ACTION]))
    code = await _wait_for_prompt(sent)

    stranger = _Source(chat_id="120363@g.us", user_id="6599999999@s.whatsapp.net")
    refusal = maybe_resolve_reply(adapter, _Event(f"YES {code}", stranger))
    assert refusal == f"Approval {code} can only be answered by the person who asked for it."
    assert not task.done()

    owner = _Source(chat_id="120363@g.us", user_id="abc123@lid", user_id_alt=PM)
    assert maybe_resolve_reply(adapter, _Event(f"NO {code}", owner)).startswith("Rejected")
    [decision] = await task
    assert decision["type"] == "reject"
    assert hitl.read_audit_log()[0]["status"] == "rejected"


async def test_another_agents_prompt_is_not_answered() -> None:
    sent = _Sent()
    mine, theirs = object(), object()
    handler = make_chat_approval_handler(
        mine,
        agent_name="desk",
        platform="whatsapp",
        chat_id=CHAT,
        approvers=sender_identities(PM),
        send=sent,
    )
    task = asyncio.create_task(handler([_ACTION]))
    code = await _wait_for_prompt(sent)
    assert maybe_resolve_reply(theirs, _Event(f"YES {code}", _Source())) is None
    maybe_resolve_reply(mine, _Event(f"YES {code}", _Source()))
    await task


async def test_no_answer_rejects_and_says_so() -> None:
    sent = _Sent()
    handler = make_chat_approval_handler(
        object(),
        agent_name="desk",
        platform="whatsapp",
        chat_id=CHAT,
        approvers=sender_identities(PM),
        timeout_seconds=1,
        send=sent,
    )
    [decision] = await handler([_ACTION])
    assert decision["type"] == "reject"
    assert "expired" in sent.texts[-1]
    row = hitl.read_audit_log()[0]
    assert row["via"] == "timeout" and row["decided_by"] == "system"


async def test_an_undeliverable_prompt_fails_closed() -> None:
    handler = make_chat_approval_handler(
        object(),
        agent_name="desk",
        platform="whatsapp",
        chat_id=CHAT,
        approvers=sender_identities(PM),
        send=_Sent(fail=True),
    )
    [decision] = await handler([_ACTION])
    assert decision["type"] == "reject"
    assert hitl.read_audit_log()[0]["via"] == "undeliverable"


async def test_the_adapter_intercepts_the_reply_before_the_busy_session(
    tmp_path, monkeypatch
) -> None:
    """A reply must not interrupt the turn that is waiting for it."""
    from olano.platforms.base import MessageEvent, MessageType
    from olano.platforms.config import PlatformConfig
    from olano.platforms.whatsapp import WhatsAppAdapter

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={}), agent_name="desk")
    adapter._message_handler = AsyncMock(return_value="")
    adapter._send_with_retry = AsyncMock()

    sent = _Sent()
    handler = make_chat_approval_handler(
        adapter,
        agent_name="desk",
        platform="whatsapp",
        chat_id=CHAT,
        approvers=sender_identities(PM),
        send=sent,
    )
    task = asyncio.create_task(handler([_ACTION]))
    code = await _wait_for_prompt(sent)

    source = adapter.build_source(chat_id=CHAT, user_id=PM)
    event = MessageEvent(text=f"YES {code}", message_type=MessageType.TEXT, source=source)
    await adapter.handle_message(event)

    assert await task == [{"type": "approve"}]
    adapter._message_handler.assert_not_called()
    assert "Approved" in adapter._send_with_retry.call_args.kwargs["content"]


def test_phone_matching_only_uses_real_phone_identifiers() -> None:
    assert phone_digits("6591234567@s.whatsapp.net") == "6591234567"
    assert phone_digits("6591234567:7@s.whatsapp.net") == "6591234567"
    assert phone_digits("+65 9123-4567") == "6591234567"
    assert phone_digits("abc123@lid") == ""
    assert phone_digits("@boss") == ""
    assert "tel:6591234567" in sender_identities("x@lid", "", PM)


def test_whatsapp_owner_listed_by_phone_is_trusted() -> None:
    from olano.runtime.server import _normalize_allowed_users, _sender_is_trusted

    owners = _normalize_allowed_users(["+65 9123 4567"])
    assert _sender_is_trusted(PM, "", owners, phone=PM)
    assert _sender_is_trusted("abc123@lid", "", owners, phone=PM)
    assert not _sender_is_trusted(
        "6599999999@s.whatsapp.net", "", owners, phone="6599999999@s.whatsapp.net"
    )
    # Without the WhatsApp-only phone argument nothing changes for other platforms.
    assert not _sender_is_trusted(PM, "", owners)


def test_chat_approvals_switch_reads_bools_and_strings() -> None:
    assert chat_approvals_enabled({"chat_approvals": True})
    assert chat_approvals_enabled({"chat_approvals": "on"})
    assert not chat_approvals_enabled({"chat_approvals": "false"})
    assert not chat_approvals_enabled({})


# ── the approval record ──────────────────────────────────────────────────────


async def test_dashboard_queue_records_request_and_decider() -> None:
    task = asyncio.create_task(
        hitl.request_approval("desk", "gmail_send", "send", payload={"args": {"to": "x"}})
    )
    for _ in range(100):
        if hitl.list_pending():
            break
        await asyncio.sleep(0.01)
    [req] = hitl.list_pending()
    assert hitl.resolve(req.id, True, "ok", decided_by="human-carlos", via="dashboard")
    assert await task is True
    # A second click cannot rewrite the first decision.
    assert hitl.resolve(req.id, False) is False

    [row] = hitl.read_audit_log()
    assert row["status"] == "approved"
    assert row["decided_by"] == "human-carlos"
    assert row["via"] == "dashboard"
    assert row["args"] == {"to": "x"}


async def test_a_request_left_open_by_a_dead_process_is_marked_abandoned() -> None:
    hitl._audit({"event": "requested", "id": "old1", "agent": "desk", "action": "x", "pid": 999999})
    hitl._AUDIT_RECONCILED = False

    task = asyncio.create_task(hitl.request_approval("desk", "y", "d", timeout_seconds=0))
    await task
    rows = {r["id"]: r for r in hitl.read_audit_log()}
    assert rows["old1"]["status"] == "abandoned"


def test_history_is_scoped_to_granted_agents() -> None:
    hitl.record_decision(agent_name="desk", action="a", approved=True, decided_by="x", via="v")
    hitl.record_decision(agent_name="other", action="b", approved=False, decided_by="y", via="v")
    assert [r["agent"] for r in hitl.read_audit_log(agents={"desk"})] == ["desk"]
    assert [r["agent"] for r in hitl.read_audit_log()] == ["other", "desk"]


def test_secrets_never_reach_the_record_or_the_chat() -> None:
    from olano.platforms.chat_approval import render_prompt

    action = {"name": "secret_set", "args": {"name": "BROKER_API", "value": "s3cr3t-Value-99xZ"}}
    prompt = render_prompt(action, "ABCD", 300)
    assert "s3cr3t" not in prompt
    assert "BROKER_API" in prompt

    hitl.record_decision(
        agent_name="desk",
        action="gmail_send",
        approved=True,
        decided_by="x",
        via="v",
        args={
            "to": "a@b.example",
            "api_key": "abc",
            "body": "key sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
        },
    )
    raw = hitl.audit_log_path().read_text()
    assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEE" not in raw
    assert '"api_key": "[REDACTED]"' in raw
    assert "a@b.example" in raw


def test_business_whatsapp_senders_match_owner_numbers() -> None:
    from olano.runtime.server import _normalize_allowed_users, _sender_is_trusted

    owners = _normalize_allowed_users(["+65 9123 4567"])
    # Twilio reports whatsapp:+E164, the Meta Cloud API +E164.
    assert _sender_is_trusted("whatsapp:+6591234567", "", owners, phone="whatsapp:+6591234567")
    assert _sender_is_trusted("+6591234567", "", owners, phone="+6591234567")
    assert not _sender_is_trusted("6599999999", "", owners, phone="6599999999")
    # A business-scoped user id is matched as typed, never as a phone number.
    bsuid = _normalize_allowed_users(["US.13491208655302741918"])
    assert _sender_is_trusted("us.13491208655302741918", "", bsuid, phone="us.13491208655302741918")
    assert phone_digits("US.13491208655302741918") == ""
