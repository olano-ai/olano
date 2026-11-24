"""WhatsApp Business: everything an agent does with its business WhatsApp, one toolkit.

Replying in a conversation is the connector's job. Everything else an agent
does with WhatsApp's Business Platform lives here, whichever of the two
business connectors the agent has: **WhatsApp (Twilio Business API)** or
**WhatsApp (Meta Cloud API)**. The tools, their names and what they ask for are
the same on both, so an agent's instructions do not depend on the provider.
Each tool asks :func:`_whatsapp_meta.meta_context` first: on a Meta connector
(or with Meta credentials in the vault and no connector) it hands its checked
arguments to ``tools/_whatsapp_meta.py``; otherwise it runs the Twilio path
written here. Two kinds of tool share the toolkit, and who holds each differs:

**Everyday tools** (``config/internal_tools.WHATSAPP_BUSINESS_TOOL_NAMES``):
list the approved templates, send one, check what WhatsApp reported about a
message, check whether the 24-hour window is open for a number. Bound
implicitly to every agent with either business connector (mirrored by the
builder, ``resolve_effective_tool_names`` and ``whoami``) and switched off per
agent (``disabled_tools``), from the connector panel or the Tools tab. This is
what a customer-facing agent needs.

**Admin tools**: create a template (with buttons), submit it for approval,
check approval status, delete one; the account's WhatsApp numbers and its
business profile; on Twilio, its balance and its usage. These act on the
account itself, which every agent on it shares, so they are never implicit:
``admin.integrations.operator_granted_tool_names`` is every member of this
toolkit that is not on the internal-tools catalog, and a config write that newly
names one needs an administrator present (``olano_admin._privileged_tools_denial``).
The toolkit is on ``TOOLKIT_DENYLIST`` (no agent adds it to itself or another
agent by name); an administrator switches the admin tools on in the connector
panel or on the Tools tab, for the one agent that manages the account.

All of them are ``owner_only``: a customer writing to the agent that holds them
can drive none of them. Sending, creating, submitting and deleting a template
and changing the profile are in the ``write_external`` approval category, so
the stricter security profiles ask a human first.

**No silent success.** The provider accepts a message before WhatsApp has
looked at it; a refusal (a sandbox number not joined, a variable mismatch, a
marketing limit) only shows up in the delivery status. The send waits a few
seconds for that verdict and reports it, and ``whatsapp_message_status`` asks
again later.

**Meta's rules are checked before a review is spent**
(``whatsapp_templates.template_problems``): a draft that breaks one is refused
with every problem listed, instead of being rejected by WhatsApp a day later
with one line.

**Which Twilio account.** The agent's own WhatsApp (Twilio) connector when it
has one (the account it sends from), else ``TWILIO_ACCOUNT_SID`` and
``TWILIO_AUTH_TOKEN`` from this agent's vault or environment; the requirements
entry in ``tools/requirements.py`` is what makes the Tools tab ask for the keys.
The Meta side's equivalent is described in ``tools/_whatsapp_meta.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Optional

from langchain_core.tools import tool

from olano.platforms.twilio_content import (
    CREATABLE_CATEGORIES,
    MAX_BUTTON_TITLE,
    PROFILE_LIMITS,
    PROFILE_VERTICALS,
    STATUS_HELP,
    USAGE_PERIODS,
    Template,
    TwilioContentClient,
    TwilioContentError,
    ambiguous_matches,
    find_template,
    is_whatsapp_provided_number,
    language_problem,
    normalize_template_name,
    parse_buttons,
    placeholders_in,
    template_problems,
    whatsapp_provided_number_problem,
)
from olano.security.tool_metadata import tool_metadata
from olano.tools import _whatsapp_meta as meta_side
from olano.tools._credentials import get_credential

#: Seconds between delivery-receipt checks after a send, and how many checks.
#: WhatsApp's refusals arrive within a few seconds; tests set the pause to 0.
RECEIPT_POLL_SECONDS = 2.0
RECEIPT_POLLS = 3

_MISSING_ACCOUNT = (
    "No WhatsApp Business account is available to this agent. Either give it a WhatsApp "
    "business connector (WhatsApp (Twilio Business API) or WhatsApp (Meta Cloud API)), or "
    "store the account's keys in its vault: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN for "
    "Twilio (Twilio Console home page, Account Info panel), or WHATSAPP_META_ACCESS_TOKEN "
    "and WHATSAPP_META_WABA_ID for Meta (Meta App Dashboard, WhatsApp, API Setup). "
    "magic_link_request_secret sends the person a secure form for each."
)

_REVIEW_NOTE = (
    "WhatsApp usually reviews a template within 24 hours. Check with "
    "whatsapp_template_status; once it reads approved, whatsapp_template_send can use it."
)

#: Twilio's shared WhatsApp sandbox sends only Twilio's own three sample
#: templates ("You can't use custom message templates with the Sandbox",
#: twilio.com/docs/whatsapp/sandbox). An account's approved templates belong to
#: its WhatsApp Business Account, which the sandbox number is not part of, so
#: every send of one from the sandbox fails with 63027 after Twilio accepted it.
SANDBOX_TEMPLATES = (
    "This agent's WhatsApp connector sends from Twilio's shared sandbox number "
    "(+1 415 523 8886), and the sandbox cannot send the account's own templates: "
    "WhatsApp refuses every one with Twilio error 63027."
)
SANDBOX_GUIDANCE = (
    "Do not retry. Templates need the business's own WhatsApp number, registered in the "
    "Twilio Console under Messaging, Senders, WhatsApp senders, set as the connector's "
    "WhatsApp sender number. Until then, an ordinary message still reaches someone who "
    "wrote in the last 24 hours."
)

#: What each WhatsApp delivery status means, for the person reading the answer.
_STATUS_MEANS = {
    "sent": "WhatsApp accepted it; it has not reached the phone yet",
    "delivered": "it reached the person's phone",
    "read": "the person opened it",
}


# ── shared plumbing ──────────────────────────────────────────────────────────


def current_agent_name() -> str:
    from olano.tools.talk_to import current_agent

    return current_agent.get() or ""


def whatsapp_adapter(agent: str = "") -> Any:
    """The executing agent's WhatsApp business connector, or ``None``.

    The Meta Cloud API connector when the agent has one, else the Twilio one:
    an agent is not expected to run both, and when it does, the direct
    connector is the one its tools act on.
    """
    try:
        from olano.platforms import get_platform_registry

        adapters = get_platform_registry().for_agent(agent or current_agent_name())
        return adapters.get(meta_side.META_KEY) or adapters.get("whatsapp_twilio")
    except Exception:
        return None


def meta() -> Optional[meta_side.MetaContext]:
    """The Meta account this agent's tools act on, or ``None`` for the Twilio path."""
    return meta_side.meta_context(whatsapp_adapter(), get_credential)


def twilio_account() -> tuple[str, str]:
    """``(account_sid, auth_token)`` for this agent, or ``("", "")``."""
    adapter = whatsapp_adapter()
    if meta_side.is_meta_adapter(adapter):
        adapter = None
    sid = str(getattr(adapter, "_account_sid", "") or "")
    token = str(getattr(adapter, "_auth_token", "") or "")
    if sid and token:
        return sid, token
    return (
        get_credential("TWILIO_ACCOUNT_SID").strip(),
        get_credential("TWILIO_AUTH_TOKEN").strip(),
    )


def content_client() -> Optional[TwilioContentClient]:
    sid, token = twilio_account()
    if not sid or not token:
        return None
    return TwilioContentClient(sid, token)


def ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def refuse(message: str, *, guidance: str = "", **extra: Any) -> str:
    """A refusal the model should relay rather than retry blindly."""
    payload: dict[str, Any] = {"error": message}
    if guidance:
        payload["agent_guidance"] = guidance
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def missing_account() -> str:
    return refuse(
        _MISSING_ACCOUNT,
        guidance="Do not retry until the credentials exist. Tell the person what is missing.",
    )


def sends_from_sandbox(adapter: Any) -> bool:
    """True when the connector sends from Twilio's shared WhatsApp sandbox number."""
    return adapter is not None and bool(getattr(adapter, "is_sandbox", False))


def connected_adapter() -> tuple[Any, str]:
    """This agent's running WhatsApp business connector, or ``(None, refusal)``."""
    adapter = whatsapp_adapter()
    if adapter is None:
        return None, refuse(
            "This agent has no WhatsApp business connector (Twilio Business API or Meta Cloud "
            "API), so it has no WhatsApp number to send from.",
            guidance="Do not retry. An operator adds the connector on the Connectors tab.",
        )
    if not getattr(adapter, "is_connected", False):
        return None, refuse(
            "This agent's WhatsApp business connector is not running.",
            guidance=(
                "Do not retry. The connector's diagnostics on the Connectors tab say why; "
                "the person may need to fix its credentials and restart the agent."
            ),
        )
    return adapter, ""


def twilio_refusal(exc: Exception) -> str:
    if isinstance(exc, TwilioContentError) and exc.status in (401, 403):
        return refuse(
            f"Twilio refused this account's credentials ({exc}).",
            guidance=(
                "Do not retry. The Account SID or Auth Token is wrong, or the token was "
                "rotated in the Twilio Console; the person has to store the current one."
            ),
        )
    return refuse(f"Twilio could not complete this ({exc}).")


def resolve_template(
    templates: list[Template], ref: str, *, approved_only: bool, language: str = ""
) -> tuple[Optional[Template], str]:
    """``(template, "")`` or ``(None, refusal)`` for the template *ref* names."""
    pool = [t for t in templates if t.approved] if approved_only else templates
    found = find_template(pool, ref, language)
    if found is not None:
        return found, ""
    same_name = ambiguous_matches(pool, ref, language)
    if len(same_name) > 1:
        return None, refuse(
            f"Several templates are named {ref!r}.",
            guidance="Pass the language, or the SID, of the one you mean.",
            candidates=[t.describe() for t in same_name],
        )
    if approved_only:
        unapproved = find_template(templates, ref)
        if unapproved is not None:
            return None, refuse(
                f"Template {unapproved.name} is {unapproved.status}, not approved, so WhatsApp "
                "would not deliver it.",
                guidance="Pick an approved one from whatsapp_template_list.",
            )
    return None, refuse(
        f"No template named {ref!r} on this Twilio account"
        + (f" in {language}" if language else "")
        + ".",
        guidance=(
            "List them with whatsapp_template_list and use a name or SID from it."
            if approved_only
            else "List them with whatsapp_template_status and use a name or SID from it."
        ),
    )


def _category(value: str) -> str:
    return str(value or "").strip().upper()


# ── everyday tools: bound with the connector ─────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def whatsapp_template_list(
    search: Annotated[
        str,
        "Optional words to filter by template name or text. Leave empty to list them all.",
    ] = "",
) -> str:
    """List the message templates WhatsApp has approved for this agent's WhatsApp Business account.

    Use before whatsapp_template_send, and whenever a WhatsApp message is refused
    because the 24-hour window is closed: outside that window WhatsApp delivers
    only an approved template. Each entry gives the template's name (what
    whatsapp_template_send takes), language, category (what WhatsApp bills it
    as), its text with placeholders such as {{1}} or {{first_name}}, a header,
    footer and buttons if it has them, and under "variables" every placeholder
    with a sample value, keyed exactly as whatsapp_template_send expects its
    values ({{header}} and {{button_1}} included where they appear). Only
    approved templates are listed, since nothing else can be sent. Read-only.
    """
    ctx = meta()
    if ctx is not None:
        return await meta_side.template_list(ctx, search)
    client = content_client()
    if client is None:
        return missing_account()
    try:
        templates = await client.list_templates()
    except Exception as exc:
        return twilio_refusal(exc)
    approved = [t for t in templates if t.approved]
    words = [w for w in search.lower().split() if w]
    if words:
        approved = [t for t in approved if all(w in f"{t.name} {t.body}".lower() for w in words)]
    rows = [t.describe(with_status=False) for t in approved]
    result: dict[str, object] = {"approved_templates": rows, "count": len(rows)}
    if sends_from_sandbox(whatsapp_adapter()):
        result["cannot_send_from_this_number"] = f"{SANDBOX_TEMPLATES} {SANDBOX_GUIDANCE}"
    if not rows:
        waiting = sum(1 for t in templates if t.status in ("received", "pending"))
        result["note"] = (
            "No approved template matches."
            + (f" {waiting} template(s) are still in WhatsApp's review." if waiting else "")
            + " Templates are created and submitted by the agent that manages the Twilio "
            "account, or in the Twilio Console (Messaging, Content Template Builder)."
        )
    return ok(result)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def whatsapp_template_send(
    to: Annotated[
        str,
        "The recipient's WhatsApp number in international format, e.g. +15551234567",
    ],
    template: Annotated[
        str,
        "The template's name or id, exactly as whatsapp_template_list shows it",
    ],
    variables: Annotated[
        Optional[dict[str, str]],
        "A value for every placeholder in the template, keyed exactly as the placeholder is "
        'written: {"1": "Maria", "2": "Friday at 3pm"} for {{1}} and {{2}}, or '
        '{"first_name": "Maria"} for {{first_name}}. whatsapp_template_list shows the keys, '
        "a header's or a website button's placeholder included. Plain text only; line breaks "
        "become spaces.",
    ] = None,
    language: Annotated[
        str,
        "The template's language code (e.g. en_US), only needed when several templates share "
        "the name in different languages",
    ] = "",
) -> str:
    """Send an approved WhatsApp template to a number, inside or outside the 24-hour window.

    This is how you write to someone on WhatsApp who has not messaged in the
    last 24 hours, or who has never messaged. Fill every placeholder, keyed
    as the template writes it ({{1}} takes "1", {{first_name}} takes
    "first_name"): a template sent with a missing value is refused rather
    than sent with WhatsApp's sample text. From Twilio's shared sandbox
    number no template of the account can be sent at all, and this says so
    instead of trying. After sending, this waits a few seconds for WhatsApp's
    verdict and reports it: "delivered"/"sent", "accepted" (no verdict yet),
    or the reason WhatsApp refused it; whatsapp_message_status checks again
    later. Each template message is billed by Meta, and WhatsApp's policy
    requires that the person agreed to receive messages from the business, so
    send only what the person running this agent asked for.
    """
    adapter, refusal = connected_adapter()
    if adapter is None:
        return refusal
    if sends_from_sandbox(adapter):
        return refuse(SANDBOX_TEMPLATES, guidance=SANDBOX_GUIDANCE)
    problem = str(getattr(adapter, "sender_problem", "") or "")
    if problem:
        return refuse(
            problem,
            guidance=(
                "Do not retry. The person fixes the connector's WhatsApp sender number on the "
                "Connectors tab and restarts the agent; whatsapp_sender_status lists the "
                "account's numbers."
            ),
        )
    ctx = meta()
    if ctx is not None:
        return await meta_side.template_send(
            ctx,
            to,
            template,
            variables or {},
            language,
            polls=RECEIPT_POLLS,
            pause=RECEIPT_POLL_SECONDS,
        )
    client = adapter.content_client()
    try:
        templates = await client.list_templates()
    except Exception as exc:
        return twilio_refusal(exc)
    found, refusal = resolve_template(templates, template, approved_only=True, language=language)
    if found is None:
        return refusal

    result = await adapter.send_template(to, found, variables or {})
    if not result.success:
        return refuse(
            str(result.error or "The template could not be sent."),
            template=found.describe(with_status=False),
        )

    raw = result.raw_response if isinstance(result.raw_response, dict) else {}
    conversation_sid = str(raw.get("conversation_sid") or "")
    status, detail = "", ""
    if conversation_sid and result.message_id:
        for attempt in range(RECEIPT_POLLS):
            if attempt or RECEIPT_POLL_SECONDS:
                await asyncio.sleep(RECEIPT_POLL_SECONDS)
            status, detail = await adapter.delivery_receipt(conversation_sid, result.message_id)
            if status:
                break
    if status in ("failed", "undelivered"):
        extra: dict[str, Any] = {"message_sid": result.message_id}
        if "63027" in detail:
            extra.update(await _explain_63027(adapter, client, found))
        return refuse(
            f"WhatsApp did not deliver the template: {detail or status}.",
            guidance=extra.pop(
                "guidance", "Tell the person why; do not resend the same message unchanged."
            ),
            **extra,
        )
    return ok(
        {
            "sent": True,
            "status": status or "accepted",
            "status_means": (
                "WhatsApp confirmed it"
                if status
                else "Twilio accepted it; WhatsApp had not confirmed delivery yet"
            ),
            "to": to,
            "template": found.name,
            "text": raw.get("rendered", ""),
            "message_sid": result.message_id,
        }
    )


async def _explain_63027(
    adapter: Any, client: TwilioContentClient, template: Template
) -> dict[str, Any]:
    """What a 63027 refusal from the account's own number carries, beyond the code.

    63027 says WhatsApp has no template with this name and language for the
    sending number. The sandbox case never reaches a send. A free 555 number
    from WhatsApp is checked first and from the number alone: Twilio does not
    support those, and the Senders API reports one ONLINE on the right
    WhatsApp Business Account, so without this branch the explanation below
    would blame an approved template and send the person off to recreate it
    (the reported case). Then the number not being a sender, or not being
    online, which the Senders API answers. What is left is a template WhatsApp
    does not hold for the number's Business Account, which only WhatsApp
    Manager can confirm; the guidance says where to look rather than asserting
    a cause nobody here has seen.
    """
    out: dict[str, Any] = {
        "template_name": template.name,
        "template_language": template.language,
        "sends_from": str(getattr(adapter, "sender_address", "") or ""),
    }
    if is_whatsapp_provided_number(out["sends_from"]):
        out["guidance"] = (
            whatsapp_provided_number_problem(out["sends_from"])
            + " The template itself needs no change. Tell the person this; do not resend "
            "until the connector sends from a supported number."
        )
        return out
    try:
        senders = await client.list_senders()
    except Exception as exc:
        out["sender_check"] = f"the account's senders could not be read ({exc})"
        out["guidance"] = (
            "Run whatsapp_sender_status once Twilio answers: the template must be approved "
            "on the WhatsApp Business Account the sending number belongs to."
        )
        return out
    mine = next((s for s in senders if s.get("number") == out["sends_from"]), None)
    if mine is None:
        out["account_senders"] = [s.get("number") for s in senders]
        out["guidance"] = (
            f"The connector sends from {out['sends_from']}, which is not a WhatsApp sender on "
            "this Twilio account, so the account's templates do not exist for it. Check the "
            "connector's WhatsApp sender number and Account SID: a number registered on "
            "another Twilio account or subaccount has that account's templates, not these."
        )
        return out
    out["sender"] = {
        "status": mine.get("status"),
        "waba_id": mine.get("waba_id"),
        "offline_reasons": mine.get("offline_reasons") or [],
    }
    if mine.get("status") != "ONLINE":
        reasons = "; ".join(str(r) for r in mine.get("offline_reasons") or [])
        out["guidance"] = (
            f"The sending number is {mine.get('status')}, not online "
            f"({mine.get('status_means') or 'not sending'})"
            + (f": {reasons}" if reasons else "")
            + ". WhatsApp delivers nothing from it until it is online; finish the number's "
            "setup in the Twilio Console (Messaging, Senders, WhatsApp senders)."
        )
        return out
    waba = mine.get("waba_id") or "unknown"
    out["guidance"] = (
        f"The number is online on WhatsApp Business Account {waba}, yet WhatsApp found no "
        f"approved template named {template.name} in language {template.language or 'unknown'} "
        "for it. WhatsApp keeps templates per Business Account, so Twilio's approval badge "
        "does not settle it: the usual cause is a template approved while the Twilio account "
        "was connected to another Business Account, or deleted or disabled in WhatsApp "
        "Manager since. Ask the person to look for it in WhatsApp Manager, under "
        f"Business Account {waba}, Message templates. If it is not there, create it again with "
        "whatsapp_template_create (under a new name if the old one is taken) and submit it. "
        "Do not resend this one unchanged."
    )
    return out


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def whatsapp_message_status(
    to: Annotated[
        str,
        "The WhatsApp number the message went to, in international format, e.g. +15551234567",
    ],
    message_sid: Annotated[
        str,
        "The message's id, as whatsapp_template_send returned it in message_sid",
    ],
) -> str:
    """Check what WhatsApp reported about a message sent earlier: sent, delivered, read, or why it failed.

    whatsapp_template_send waits only a few seconds for WhatsApp's verdict;
    use this to check later, for example whether a reminder was delivered or
    read before following up. "read" appears only when the person has read
    receipts switched on, so "delivered" is not proof they have not seen it.
    On the Meta Cloud API connector the answer also says how WhatsApp billed
    the message (its category, and whether it was free). Read-only.
    """
    adapter, refusal = connected_adapter()
    if adapter is None:
        return refusal
    sid = str(message_sid or "").strip()
    ctx = meta()
    if ctx is not None:
        return await meta_side.message_status(ctx, to, sid)
    status, detail = await adapter.message_status(to, sid)
    if status in ("failed", "undelivered"):
        return refuse(
            f"WhatsApp did not deliver it: {detail or status}.",
            guidance="Tell the person why; do not resend the same message unchanged.",
            message_sid=sid,
        )
    return ok(
        {
            "message_sid": sid,
            "to": to,
            "status": status or "unknown",
            "status_means": _STATUS_MEANS.get(status, detail or "WhatsApp has not reported yet"),
        }
    )


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def whatsapp_window_status(
    to: Annotated[
        str,
        "The person's WhatsApp number in international format, e.g. +15551234567",
    ],
) -> str:
    """Check whether WhatsApp's 24-hour window is open for a number: whether an ordinary message would be delivered, or only an approved template.

    The window opens each time the person writes in and closes 24 hours after
    their last message. Inside it, an ordinary message (a reply, a
    notification) is delivered; outside it, WhatsApp delivers only an approved
    template (whatsapp_template_send). Ask this before choosing how to write
    to someone, or when a message was refused for a closed window. Reports
    when the person last wrote and when the window closes. Read-only.
    """
    adapter, refusal = connected_adapter()
    if adapter is None:
        return refusal
    status = await adapter.window_status(to)
    if status.get("error"):
        return refuse(str(status["error"]))
    status["next"] = (
        "An ordinary message reaches this person now; the window closes at window_closes_at."
        if status.get("window_open")
        else (
            "Only an approved template reaches this person now: whatsapp_template_list, then "
            "whatsapp_template_send. Attachments cannot be sent until they write again."
        )
    )
    return ok(status)


# ── admin tools: granted by an administrator ─────────────────────────────────


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def whatsapp_template_create(
    name: Annotated[
        str,
        "Template name: lowercase letters, digits and underscores, e.g. order_ready. Other "
        "characters are converted to underscores.",
    ],
    body: Annotated[
        str,
        "The message text, with placeholders for the parts that change: numbered, e.g. "
        "'Hi {{1}}, your order {{2}} is ready for pickup at our Main Street store.', or "
        "named in lowercase, e.g. 'Hi {{first_name}}, your order {{order_number}} is ready.'",
    ],
    category: Annotated[
        str,
        "UTILITY for updates about something the customer asked for (orders, appointments, "
        "account alerts); MARKETING for promotions and offers.",
    ] = "UTILITY",
    language: Annotated[str, "Language code of the text, e.g. en, es, pt_BR"] = "en",
    samples: Annotated[
        Optional[dict[str, str]],
        "A realistic sample value for every placeholder, used by WhatsApp's review, keyed as "
        'the placeholder is written, e.g. {"1": "Maria", "2": "#1042"} or '
        '{"first_name": "Maria"}',
    ] = None,
    buttons: Annotated[
        Optional[list[dict[str, str]]],
        "Optional buttons under the message. Reply buttons (up to 10), which send their "
        'title back as the person\'s answer: [{"type": "reply", "title": "Yes, confirm"}, '
        '{"type": "reply", "title": "Reschedule"}]. Or website and phone buttons (up to 2 '
        'websites, 1 phone): [{"type": "url", "title": "Track order", "url": '
        '"https://shop.example/track/{{2}}"}, {"type": "phone", "title": "Call us", '
        f'"phone": "+15551234567"}}]. Titles are at most {MAX_BUTTON_TITLE} characters; a '
        "website address may end with one placeholder; the two kinds are not mixed.",
    ] = None,
    submit: Annotated[bool, "Submit to WhatsApp for approval right away (default true)"] = True,
) -> str:
    """Create a WhatsApp message template on the business account and submit it to WhatsApp for approval.

    Draft the text with the person first and show it to them: it goes to
    Meta's review under the business's name. WhatsApp's rules are checked
    before anything is created, and a draft that breaks one is refused with
    every problem listed, so fix and call again. The rules: placeholders are
    either numbered {{1}}, {{2}}... with no gaps, or named in lowercase such
    as {{first_name}}, never both in one template; never side by side, never
    the first or last thing in the text; each placeholder needs at least two
    more words of fixed text; every placeholder needs a sample; at most 1024
    characters (640 with website or phone buttons). Buttons are optional: reply
    buttons let the person answer with one tap, website and phone buttons open
    a page or start a call.
    A new template cannot be sent until WhatsApp approves it, usually within
    24 hours (check with whatsapp_template_status). Templates cannot be edited
    after submission: to change one, create a new one. On the Meta Cloud API
    connector a template is always submitted when it is created.
    """
    ctx = meta()
    client = content_client() if ctx is None else None
    if ctx is None and client is None:
        return missing_account()
    template_name = normalize_template_name(name)
    upper = _category(category)
    sample_values = {str(k): str(v) for k, v in (samples or {}).items()}
    parsed_buttons, problems = parse_buttons(buttons)
    problems += template_problems(body, sample_values, category=upper, buttons=parsed_buttons)
    lang_problem = language_problem(language)
    if lang_problem:
        problems.append(lang_problem)
    if not template_name:
        problems.append("Give the template a name made of letters, digits or underscores.")
    if problems:
        return refuse(
            "The template was not created: it breaks WhatsApp's template rules.",
            guidance="Fix every problem listed, show the person the new draft, then call again.",
            problems=problems,
        )
    keys = placeholders_in(" ".join([body, *(f"{b.title} {b.value}" for b in parsed_buttons)]))
    if ctx is not None:
        return await meta_side.template_create(
            ctx,
            name=template_name,
            body=body,
            category=upper,
            language=language,
            samples={k: sample_values[k] for k in keys},
            buttons=parsed_buttons,
            submit=submit,
        )
    assert client is not None
    try:
        existing = await client.list_templates(fresh=True)
    except Exception as exc:
        return twilio_refusal(exc)
    clash = [
        t
        for t in existing
        if template_name in (t.whatsapp_name.lower(), t.friendly_name.lower())
        and (t.language or "") == language
    ]
    if clash:
        return refuse(
            f"A template named {template_name} already exists in {language} ({clash[0].status}).",
            guidance="Pick another name, or delete the old one first if it is not needed.",
            existing=clash[0].describe(),
        )
    try:
        created = await client.create_template(
            friendly_name=template_name,
            language=language,
            body=body.strip(),
            samples={k: sample_values[k] for k in keys},
            buttons=parsed_buttons,
        )
    except Exception as exc:
        return twilio_refusal(exc)

    result: dict[str, object] = {"created": created.describe(), "submitted": False}
    if not submit:
        result["next"] = (
            "Created but not submitted. Submit it with whatsapp_template_submit when the "
            "person is ready."
        )
        return ok(result)
    try:
        approval = await client.submit_for_whatsapp(created.sid, name=template_name, category=upper)
    except Exception as exc:
        result["submit_error"] = str(exc)
        result["next"] = (
            "The template exists on the account but WhatsApp did not take the submission. "
            "Fix the reason and retry with whatsapp_template_submit."
        )
        return ok(result)
    status = str(approval.get("status") or "received").lower()
    result.update(
        {
            "submitted": True,
            "status": status,
            "status_means": STATUS_HELP.get(status, ""),
            "next": _REVIEW_NOTE,
        }
    )
    return ok(result)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def whatsapp_template_submit(
    template: Annotated[str, "The template's name or id"],
    category: Annotated[
        str,
        "UTILITY for updates about something the customer asked for; MARKETING for promotions.",
    ] = "UTILITY",
    whatsapp_name: Annotated[
        str,
        "Name to submit under (lowercase letters, digits, underscores). Leave empty to use the "
        "template's own name.",
    ] = "",
) -> str:
    """Submit an existing, never-submitted template to WhatsApp for approval.

    For a template created with submit=false, or one someone built in the
    Twilio Console without submitting. WhatsApp's rules are checked first and
    a draft that breaks one is refused with the problems listed. A template
    already submitted cannot be submitted again; a rejected one cannot be
    edited, so create a corrected copy under a new name instead. On the Meta
    Cloud API connector every template is submitted when it is created, and
    this says where the template stands.
    """
    ctx = meta()
    if ctx is not None:
        return await meta_side.template_submit(ctx, template)
    client = content_client()
    if client is None:
        return missing_account()
    try:
        templates = await client.list_templates(fresh=True)
    except Exception as exc:
        return twilio_refusal(exc)
    found, refusal = resolve_template(templates, template, approved_only=False)
    if found is None:
        return refusal
    if found.status != "unsubmitted":
        return refuse(
            f"Template {found.name} was already submitted ({found.status}: "
            f"{STATUS_HELP.get(found.status, '')}).",
            guidance=(
                "A rejected template cannot be resubmitted or edited: create a corrected copy "
                "with whatsapp_template_create under a new name."
                if found.status == "rejected"
                else "Nothing to do; check progress with whatsapp_template_status."
            ),
        )
    upper = _category(category)
    submit_name = normalize_template_name(whatsapp_name or found.friendly_name or found.name)
    problems = template_problems(found.body, found.samples, category=upper, buttons=found.buttons)
    if not submit_name:
        problems.append("Give a name made of letters, digits or underscores.")
    if problems:
        return refuse(
            f"Template {found.name} was not submitted: it breaks WhatsApp's template rules.",
            guidance=(
                "Templates cannot be edited; create a corrected copy with "
                "whatsapp_template_create and delete this one if it is not needed."
            ),
            problems=problems,
        )
    try:
        approval = await client.submit_for_whatsapp(found.sid, name=submit_name, category=upper)
    except Exception as exc:
        return twilio_refusal(exc)
    status = str(approval.get("status") or "received").lower()
    return ok(
        {
            "submitted": True,
            "template": found.name,
            "whatsapp_name": submit_name,
            "category": upper,
            "status": status,
            "status_means": STATUS_HELP.get(status, ""),
            "next": _REVIEW_NOTE,
        }
    )


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def whatsapp_template_status(
    template: Annotated[
        str,
        "A template's name or id. Leave empty for every template on the account.",
    ] = "",
) -> str:
    """Show WhatsApp approval status and category: of one template, or of every template on the account.

    Reports each template's status (unsubmitted, received, pending, approved,
    rejected, paused or disabled) with what it means, WhatsApp's reason when it
    rejected one, and its category, including when WhatsApp moved it to
    another category (which changes what it is billed as). Always asks fresh.
    Read-only; safe to run on a schedule to follow a review.
    """
    ctx = meta()
    if ctx is not None:
        return await meta_side.template_status(ctx, template)
    client = content_client()
    if client is None:
        return missing_account()
    try:
        templates = await client.list_templates(fresh=True)
    except Exception as exc:
        return twilio_refusal(exc)
    if template.strip():
        found, refusal = resolve_template(templates, template, approved_only=False)
        if found is None:
            return refusal
        try:
            fresh = await client.get_template(found.sid)
        except Exception as exc:
            return twilio_refusal(exc)
        return ok({"template": fresh.describe()})
    counts: dict[str, int] = {}
    for t in templates:
        counts[t.status] = counts.get(t.status, 0) + 1
    return ok(
        {
            "templates": [t.describe() for t in templates],
            "count_by_status": counts,
            "categories_you_can_create": list(CREATABLE_CATEGORIES),
        }
    )


@tool_metadata(capability="write_external", risk="high", requires_hitl=True, owner_only=True)
@tool
async def whatsapp_template_delete(
    template: Annotated[str, "The template's name or id"],
    confirm_name: Annotated[
        str,
        "The template's name typed out again, exactly as whatsapp_template_status shows it, "
        "to confirm the deletion",
    ],
) -> str:
    """Delete a WhatsApp template from the business account. This cannot be undone.

    Every agent on the account loses it at once, and an approved template that
    is deleted has to be recreated and wait for WhatsApp's review again. Only
    delete what the person explicitly asked to delete, and pass its name in
    confirm_name.
    """
    ctx = meta()
    if ctx is not None:
        return await meta_side.template_delete(ctx, template, confirm_name)
    client = content_client()
    if client is None:
        return missing_account()
    try:
        templates = await client.list_templates(fresh=True)
    except Exception as exc:
        return twilio_refusal(exc)
    found, refusal = resolve_template(templates, template, approved_only=False)
    if found is None:
        return refusal
    if confirm_name.strip().lower() not in {found.name.lower(), found.sid.lower()}:
        return refuse(
            f"Not deleted: confirm_name {confirm_name!r} does not match the template's name "
            f"{found.name!r}.",
            guidance="Pass the exact name only after the person confirmed this deletion.",
        )
    try:
        await client.delete_template(found.sid)
    except Exception as exc:
        return twilio_refusal(exc)
    return ok({"deleted": True, "template": found.name, "sid": found.sid})


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def whatsapp_sender_status() -> str:
    """Show the business account's WhatsApp numbers: whether each is online, its business profile, Meta's quality rating, and its messaging limit.

    Use it when setting up a number (it shows whether registration has
    finished), when messages or templates fail for a reason that may be the
    number itself (an OFFLINE sender lists why), and to tell whether this
    agent's WhatsApp connector sends from one of these numbers or from Twilio's
    shared sandbox, which cannot send the account's templates. A row with a
    ``problem`` is a number Twilio does not support even though it reads
    ONLINE (a free 555 number from WhatsApp); relay that problem. The quality
    rating is Meta's view of how recipients react (a low one precedes a lower
    limit); the messaging limit is how many people a day the number may start
    conversations with. Read-only.
    """
    ctx = meta()
    if ctx is not None:
        return await meta_side.sender_status(ctx)
    client = content_client()
    if client is None:
        return missing_account()
    try:
        senders = await client.list_senders()
    except Exception as exc:
        return twilio_refusal(exc)
    result: dict[str, object] = {"senders": senders, "count": len(senders)}
    adapter = whatsapp_adapter()
    if adapter is not None:
        address = str(getattr(adapter, "sender_address", "") or "")
        result["this_agent_sends_from"] = address
        if sends_from_sandbox(adapter):
            result["note"] = f"{SANDBOX_TEMPLATES} {SANDBOX_GUIDANCE}"
        elif is_whatsapp_provided_number(address):
            # Listed ONLINE by the Senders API all the same, which is exactly
            # why the person reads the row as healthy.
            result["note"] = whatsapp_provided_number_problem(address)
        elif address and address not in {row.get("number") for row in senders}:
            result["note"] = (
                f"This agent's connector sends from {address}, which is not a WhatsApp sender "
                "on this Twilio account: check the connector's WhatsApp sender number and "
                "Account SID."
            )
    if not senders:
        result["note"] = (
            str(result.get("note") or "")
            + " No WhatsApp number is registered on this account yet: register one in the "
            "Twilio Console (Messaging, Senders, WhatsApp senders). Until then only Twilio's "
            "shared sandbox works, and it cannot send the account's templates."
        ).strip()
    return ok(result)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def whatsapp_profile_update(
    number: Annotated[
        str,
        "The WhatsApp number whose profile to change, in international format. Leave empty "
        "for the number this agent's connector sends from.",
    ] = "",
    about: Annotated[
        Optional[str],
        f"The short line under the business name (at most {PROFILE_LIMITS['about']} characters)",
    ] = None,
    description: Annotated[
        Optional[str],
        f"What the business does (at most {PROFILE_LIMITS['description']} characters)",
    ] = None,
    address: Annotated[
        Optional[str],
        f"Street address (at most {PROFILE_LIMITS['address']} characters)",
    ] = None,
    email: Annotated[Optional[str], "Contact email address"] = None,
    website: Annotated[Optional[str], "Website, starting with https://"] = None,
    vertical: Annotated[
        Optional[str],
        "Business category, one of: " + ", ".join(PROFILE_VERTICALS),
    ] = None,
) -> str:
    """Update the WhatsApp business profile customers see for one of the account's numbers.

    Changes the About line, description, address, email, website and business
    category shown when someone opens the business in WhatsApp. Only the
    fields you pass change; the others keep their current value. The display
    name is not changed here: Meta reviews name changes, so that is done in
    the Twilio Console or WhatsApp Manager. Show the person the new values
    before calling this.
    """
    ctx = meta()
    client = content_client() if ctx is None else None
    if ctx is None and client is None:
        return missing_account()
    changes: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in (("about", about), ("description", description), ("address", address)):
        if value is None:
            continue
        text = " ".join(str(value).split())
        if len(text) > PROFILE_LIMITS[key]:
            problems.append(
                f"{key} is {len(text)} characters; WhatsApp allows {PROFILE_LIMITS[key]}."
            )
        changes[key] = text
    if email is not None:
        text = str(email).strip()
        if text and ("@" not in text or " " in text):
            problems.append(f"{text!r} is not an email address.")
        changes["emails"] = [{"email": text, "label": "Email"}] if text else []
    if website is not None:
        text = str(website).strip()
        if text and not text.startswith("https://"):
            problems.append("The website must start with https://.")
        changes["websites"] = [{"website": text, "label": "Website"}] if text else []
    if vertical is not None:
        match = next(
            (v for v in PROFILE_VERTICALS if v.lower() == str(vertical).strip().lower()), None
        )
        if match is None:
            problems.append(
                f"{vertical!r} is not a WhatsApp business category; use one of: "
                + ", ".join(PROFILE_VERTICALS)
            )
        else:
            changes["vertical"] = match
    if not changes:
        return refuse(
            "Nothing to change: pass at least one of about, description, address, email, "
            "website or vertical."
        )
    if problems:
        return refuse("The profile was not changed.", problems=problems)
    if ctx is not None:
        return await meta_side.profile_update(ctx, number, changes)
    assert client is not None

    adapter = whatsapp_adapter()
    target = str(number or "").strip()
    if not target:
        if sends_from_sandbox(adapter):
            return refuse(
                "This agent's connector sends from Twilio's shared sandbox number, which has no "
                "business profile of yours to change.",
                guidance="Pass one of the account's own numbers (whatsapp_sender_status lists them).",
            )
        target = str(getattr(adapter, "sender_address", "") or "")
    if not target:
        return refuse(
            "No number given and this agent has no WhatsApp connector to take one from.",
            guidance="Pass the number; whatsapp_sender_status lists the account's numbers.",
        )
    digits = "+" + "".join(ch for ch in target if ch.isdigit())
    try:
        senders = await client.list_senders()
    except Exception as exc:
        return twilio_refusal(exc)
    found = next((s for s in senders if str(s.get("number", "")).endswith(digits)), None)
    if found is None:
        return refuse(
            f"{digits} is not a WhatsApp sender on this Twilio account.",
            guidance="whatsapp_sender_status lists the numbers that are.",
            numbers=[s.get("number") for s in senders],
        )
    raw_profile = found.get("profile")
    current: dict[str, Any] = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    profile = {**current, **changes}
    if not profile.get("name"):
        return refuse(
            f"The profile of {digits} has no display name yet, so it cannot be updated here; "
            "finish the number's registration in the Twilio Console first."
        )
    try:
        updated = await client.update_sender_profile(str(found["sid"]), profile)
    except Exception as exc:
        return twilio_refusal(exc)
    return ok({"updated": True, "number": found.get("number"), "profile": updated.get("profile")})


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def twilio_account_balance() -> str:
    """Show the Twilio account's current balance and currency.

    Twilio charges per WhatsApp message on top of Meta's fee, and when a
    prepaid balance runs out it stops sending, WhatsApp included, so check
    this when sends start failing without another reason, or before a large
    send. Read-only.
    """
    client = content_client()
    if client is None:
        return missing_account()
    try:
        balance = await client.balance()
    except Exception as exc:
        return twilio_refusal(exc)
    return ok({"account_sid": client.account_sid, **balance})


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
async def twilio_usage_summary(
    period: Annotated[
        str,
        "this_month (default), last_month, today or yesterday",
    ] = "this_month",
) -> str:
    """Show what the Twilio account spent on WhatsApp in a period: conversations and template messages by category, and the account's total spend.

    WhatsApp usage is billed per conversation and per template category
    (utility, marketing, authentication), and Twilio adds its own fee; this
    reads Twilio's usage records for the period so the person can see the
    count and the cost of each, plus the account's total across every Twilio
    product. Read-only.
    """
    client = content_client()
    if client is None:
        return missing_account()
    key = USAGE_PERIODS.get(str(period or "").strip().lower().replace("-", "_").replace(" ", "_"))
    if key is None:
        return refuse(f"period must be one of {', '.join(USAGE_PERIODS)}; got {period!r}.")
    try:
        rows = await client.usage(key)
    except Exception as exc:
        return twilio_refusal(exc)
    whatsapp = [r for r in rows if str(r.get("category", "")).startswith("channels-whatsapp")]
    total = next((r for r in rows if r.get("category") == "totalprice"), None)
    spent = 0.0
    currency = ""
    for r in whatsapp:
        try:
            spent += float(r.get("price") or 0)
        except (TypeError, ValueError):
            pass
        currency = currency or str(r.get("price_unit") or "")
    return ok(
        {
            "period": period,
            "whatsapp": whatsapp,
            "whatsapp_total": {"price": f"{spent:.4f}", "price_unit": currency},
            "account_total": total,
            "note": (
                "Meta's per-conversation charge is billed by Twilio and included here; "
                "a template sent to someone who wrote in the last 24 hours may fall in a "
                "free or service conversation instead of a template category."
            ),
        }
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
