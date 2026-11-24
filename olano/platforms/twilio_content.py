"""Twilio Content API: the WhatsApp message templates a Twilio account holds.

WhatsApp lets a business open a conversation, or write to someone more than
24 hours after they last wrote, only with a template Meta has approved. Twilio
stores each template as a Content resource (an ``HX…`` SID) and relays it to
Meta for review. This module is the client for that API, used by the WhatsApp
over Twilio adapter (sending) and the WhatsApp Business toolkit (listing,
creating, submitting, checking, deleting). It also reads the account facts the
toolkit reports: the account's WhatsApp senders, its balance and its usage.
The template model and WhatsApp's rules are shared with the Meta Cloud API
connector and live in ``whatsapp_templates``.

Endpoints (JSON, HTTP Basic auth with the account SID and auth token)::

    https://content.twilio.com
    GET    /v1/ContentAndApprovals                       every template + its approval
    GET    /v1/Content/{sid}                             one template
    GET    /v1/Content/{sid}/ApprovalRequests            its WhatsApp approval
    POST   /v1/Content                                   create
    POST   /v1/Content/{sid}/ApprovalRequests/whatsapp   submit to Meta (name, category)
    DELETE /v1/Content/{sid}                             delete

    https://messaging.twilio.com
    GET    /v2/Channels/Senders?Channel=whatsapp         WhatsApp senders + status

    https://api.twilio.com
    GET    /2010-04-01/Accounts/{sid}/Balance.json       the account balance

A template's buttons live in its content type: ``twilio/quick-reply`` (up to
ten reply buttons) or ``twilio/call-to-action`` (up to two website buttons and
one phone button, a 640-character body). A placeholder may sit in a button too,
at the end of a website address, and needs a value on every send like one in
the body: an unfilled one goes out as the template's sample text. Twilio's
variables are one namespace across the body and the buttons.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Optional

# The provider-neutral template model and WhatsApp's own rules live in
# ``whatsapp_templates``; they are re-exported here because this module is where
# they were first defined and callers still import them from it.
from olano.platforms.whatsapp_templates import (  # noqa: F401
    _E164,
    CATEGORIES,
    CREATABLE_CATEGORIES,
    MAX_BUTTON_TITLE,
    MAX_CALL_TO_ACTION_BODY,
    MAX_PHONE_BUTTONS,
    MAX_REPLY_BUTTONS,
    MAX_TEMPLATE_BODY,
    MAX_URL_BUTTONS,
    MAX_VARIABLE_KEY,
    PROFILE_LIMITS,
    PROFILE_VERTICALS,
    STATUS_HELP,
    TEMPLATE_NAME_RE,
    Button,
    Template,
    ambiguous_matches,
    clean_variable,
    find_template,
    language_problem,
    normalize_template_name,
    parse_buttons,
    placeholders_in,
    render,
    template_problems,
)

logger = logging.getLogger(__name__)

CONTENT_API_BASE = "https://content.twilio.com/v1"
SENDERS_API = "https://messaging.twilio.com/v2/Channels/Senders"
ACCOUNTS_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"


#: Twilio's usage-record periods (``Usage/Records/<period>.json``).
USAGE_PERIODS: dict[str, str] = {
    "this_month": "ThisMonth",
    "last_month": "LastMonth",
    "today": "Today",
    "yesterday": "Yesterday",
}

_PROFILE_KEYS = (
    "name",
    "about",
    "address",
    "description",
    "vertical",
    "emails",
    "websites",
    "logo_url",
)

#: WhatsApp sender states the Senders API reports, in plain words.
SENDER_STATUS_HELP: dict[str, str] = {
    "ONLINE": "active: it sends and receives messages",
    "ONLINE:UPDATING": "active while a change to it is applied",
    "OFFLINE": "not working; offline_reasons says why",
    "CREATING": "being set up",
    "PENDING_VERIFICATION": "waiting for the number's verification code",
    "VERIFYING": "verification in progress",
    "TWILIO_REVIEW": "in review at Twilio",
    "DRAFT": "a draft that was never finished",
    "STUBBED": "a placeholder that was never finished",
}


#: Twilio error codes a WhatsApp delivery commonly fails with, in plain words.
#: Every code also has a page at ``https://www.twilio.com/docs/api/errors/<code>``.
WHATSAPP_ERROR_HINTS: dict[int, str] = {
    63015: "the Twilio sandbox only delivers to numbers that joined it with the join code",
    63016: "the 24-hour window is closed and the message was not an approved template",
    63024: "the recipient is not a valid WhatsApp user",
    63027: (
        "WhatsApp has no approved template with this name and language for the sending "
        "number; Twilio's shared sandbox number can never send your own templates, a free "
        "555 number from WhatsApp is not supported by Twilio, and a template approved on "
        "another WhatsApp Business Account does not exist for this one"
    ),
    63028: "the number of variables sent does not match the template",
    63049: "Meta chose not to deliver this marketing message to this person",
}

_CACHE_TTL_SECONDS = 60.0
_MAX_PAGES = 20


def twilio_error(status: int, body: str) -> str:
    """Twilio's own error sentence (and code) from a 4xx/5xx response body."""
    import json

    detail = body
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        code = payload.get("code")
        if message:
            detail = f"{message} (error {code})" if code else message
    return f"Twilio {status}: {str(detail).strip()[:300]}"


def describe_error_code(code: Any) -> str:
    """A delivery error code as a sentence the agent can relay."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return f"Twilio error {code}"
    hint = WHATSAPP_ERROR_HINTS.get(number)
    text = f"Twilio error {number}"
    if hint:
        text += f": {hint}"
    return f"{text} (https://www.twilio.com/docs/api/errors/{number})"


#: Twilio's call-to-action button types, as :class:`Button` kinds.
_ACTION_KINDS = {
    "URL": "url",
    "PHONE_NUMBER": "phone",
    "QUICK_REPLY": "reply",
    "COPY_CODE": "copy_code",
}


def _buttons(spec: Mapping[str, Any]) -> tuple[Button, ...]:
    """The buttons of one content type (quick-reply or call-to-action)."""
    out: list[Button] = []
    for action in spec.get("actions") or []:
        if not isinstance(action, dict):
            continue
        raw_type = str(action.get("type") or "QUICK_REPLY").upper()
        kind = _ACTION_KINDS.get(raw_type, raw_type.lower())
        value = action.get("url") or action.get("phone") or action.get("code") or action.get("id")
        out.append(Button(kind, str(action.get("title") or ""), str(value or "")))
    return tuple(out)


def _primary_content(types: Any) -> tuple[str, str, tuple[Button, ...]]:
    """``(content_type, body, buttons)`` a template sends, preferring plain text."""
    if not isinstance(types, dict):
        return "", "", ()
    text = types.get("twilio/text")
    if isinstance(text, dict) and text.get("body") and len(types) == 1:
        return "twilio/text", str(text["body"]), ()
    # A template with buttons may also carry a plain-text fallback for other
    # channels; WhatsApp sends the buttons, so they are what describes it.
    for name, spec in types.items():
        if isinstance(spec, dict) and spec.get("body") and spec.get("actions"):
            return str(name), str(spec["body"]), _buttons(spec)
    for name, spec in types.items():
        if isinstance(spec, dict) and spec.get("body"):
            return str(name), str(spec["body"]), ()
    first = next(iter(types), "")
    return str(first), "", ()


def content_types_for(body: str, buttons: list[Button]) -> dict[str, Any]:
    """The ``types`` object of a Content create request for this body and buttons."""
    if not buttons:
        return {"twilio/text": {"body": body}}
    if all(b.kind == "reply" for b in buttons):
        return {
            "twilio/quick-reply": {
                "body": body,
                "actions": [{"title": b.title, "id": b.value} for b in buttons],
            }
        }
    actions: list[dict[str, str]] = []
    for b in buttons:
        if b.kind == "url":
            actions.append({"type": "URL", "title": b.title, "url": b.value})
        elif b.kind == "phone":
            actions.append({"type": "PHONE_NUMBER", "title": b.title, "phone": b.value})
    return {"twilio/call-to-action": {"body": body, "actions": actions}}


def template_from_api(row: Mapping[str, Any]) -> Template:
    """Build a :class:`Template` from a Content (or ContentAndApprovals) row.

    The approval arrives in two shapes: flat on a ContentAndApprovals row
    (``approval_requests: {name, category, status, …}``) and keyed by channel
    on an ApprovalRequests fetch (``{"whatsapp": {…}}``). Both are read.
    """
    content_type, body, buttons = _primary_content(row.get("types"))
    approval = row.get("approval_requests")
    if isinstance(approval, dict) and isinstance(approval.get("whatsapp"), dict):
        approval = approval["whatsapp"]
    if not isinstance(approval, dict):
        approval = {}
    samples = row.get("variables")
    return Template(
        sid=str(row.get("sid") or ""),
        friendly_name=str(row.get("friendly_name") or ""),
        language=str(row.get("language") or ""),
        content_type=content_type,
        body=body,
        samples={str(k): str(v) for k, v in samples.items()} if isinstance(samples, dict) else {},
        whatsapp_name=str(approval.get("name") or ""),
        category=str(approval.get("category") or "").upper(),
        status=str(approval.get("status") or "unsubmitted").strip().lower() or "unsubmitted",
        rejection_reason=str(approval.get("rejection_reason") or ""),
        date_updated=str(row.get("date_updated") or ""),
        buttons=buttons,
    )


class TwilioContentError(RuntimeError):
    """A refused Content API call, carrying Twilio's own sentence."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


class TwilioContentClient:
    """Async client for the Content API (templates) of one Twilio account."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        self.account_sid = account_sid
        self._auth = (account_sid, auth_token)
        self._timeout = timeout
        # Tests inject an ``httpx.MockTransport``; production uses the default.
        self._transport = transport

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> Any:
        import httpx

        kwargs: dict[str, Any] = {"auth": self._auth, "timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.request(method, url, json=json_body, params=params)
        if resp.status_code >= 400:
            raise TwilioContentError(resp.status_code, twilio_error(resp.status_code, resp.text))
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def list_templates(self, *, fresh: bool = False) -> list[Template]:
        """Every template on the account with its WhatsApp approval.

        Cached for a minute per account: an agent typically lists and then
        sends, and the list is the same both times. Anything that changes a
        template drops the cache (:func:`invalidate_templates`).
        """
        cached = _CACHE.get(self.account_sid)
        if cached and not fresh and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return list(cached[1])
        url: Optional[str] = f"{CONTENT_API_BASE}/ContentAndApprovals"
        params: Optional[dict[str, str]] = {"PageSize": "100"}
        out: list[Template] = []
        pages = 0
        while url and pages < _MAX_PAGES:
            pages += 1
            payload = await self._request("GET", url, params=params)
            rows = payload.get("contents") if isinstance(payload, dict) else None
            out.extend(template_from_api(r) for r in rows or [] if isinstance(r, dict))
            meta = payload.get("meta") if isinstance(payload, dict) else None
            next_url = meta.get("next_page_url") if isinstance(meta, dict) else None
            url, params = (str(next_url), None) if next_url else (None, None)
        _CACHE[self.account_sid] = (time.monotonic(), list(out))
        return out

    async def get_template(self, sid: str) -> Template:
        """One template with its current approval, fetched fresh."""
        row = await self._request("GET", f"{CONTENT_API_BASE}/Content/{sid}")
        try:
            approvals = await self._request(
                "GET", f"{CONTENT_API_BASE}/Content/{sid}/ApprovalRequests"
            )
        except TwilioContentError as exc:
            if exc.status != 404:
                raise
            approvals = {}
        whatsapp = approvals.get("whatsapp") if isinstance(approvals, dict) else None
        return template_from_api({**row, "approval_requests": whatsapp or {}})

    async def create_template(
        self,
        *,
        friendly_name: str,
        language: str,
        body: str,
        samples: Mapping[str, str],
        buttons: tuple[Button, ...] | list[Button] = (),
    ) -> Template:
        """Create a template: plain text, or with reply or website/phone buttons."""
        payload = {
            "friendly_name": friendly_name,
            "language": language,
            "variables": {str(k): str(v) for k, v in samples.items()},
            "types": content_types_for(body, list(buttons)),
        }
        row = await self._request("POST", f"{CONTENT_API_BASE}/Content", json_body=payload)
        invalidate_templates(self.account_sid)
        return template_from_api(row)

    async def submit_for_whatsapp(self, sid: str, *, name: str, category: str) -> dict[str, Any]:
        payload = {"name": name, "category": category}
        result = await self._request(
            "POST", f"{CONTENT_API_BASE}/Content/{sid}/ApprovalRequests/whatsapp", json_body=payload
        )
        invalidate_templates(self.account_sid)
        return result if isinstance(result, dict) else {}

    async def delete_template(self, sid: str) -> None:
        await self._request("DELETE", f"{CONTENT_API_BASE}/Content/{sid}")
        invalidate_templates(self.account_sid)

    async def list_senders(self) -> list[dict[str, Any]]:
        """The account's WhatsApp senders, each flattened to what an operator reads.

        ``quality_rating`` and ``messaging_limit`` are Meta's: a low rating
        precedes a limit cut, and the limit is how many people a day the number
        may start conversations with.
        """
        url: Optional[str] = SENDERS_API
        params: Optional[dict[str, str]] = {"Channel": "whatsapp", "PageSize": "50"}
        out: list[dict[str, Any]] = []
        pages = 0
        while url and pages < _MAX_PAGES:
            pages += 1
            payload = await self._request("GET", url, params=params)
            for row in (payload.get("senders") if isinstance(payload, dict) else None) or []:
                if isinstance(row, dict):
                    out.append(_sender_row(row))
            meta = payload.get("meta") if isinstance(payload, dict) else None
            url = meta.get("next_page_url") if isinstance(meta, dict) else None
            params = None  # the next-page URL carries its own query
        return out

    async def update_sender_profile(
        self, sender_sid: str, profile: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace a WhatsApp sender's business profile (Meta shows it to customers)."""
        payload = {"profile": {k: v for k, v in profile.items() if k in _PROFILE_KEYS}}
        row = await self._request("POST", f"{SENDERS_API}/{sender_sid}", json_body=payload)
        return _sender_row(row) if isinstance(row, dict) else {}

    async def usage(self, period_key: str) -> list[dict[str, Any]]:
        """Twilio's usage records for one period, every category, flattened."""
        url: Optional[str] = (
            f"{ACCOUNTS_API_BASE}/{self.account_sid}/Usage/Records/{period_key}.json"
        )
        params: Optional[dict[str, str]] = {"PageSize": "1000"}
        out: list[dict[str, Any]] = []
        pages = 0
        while url and pages < _MAX_PAGES:
            pages += 1
            payload = await self._request("GET", url, params=params)
            rows = payload.get("usage_records") if isinstance(payload, dict) else None
            for row in rows or []:
                if isinstance(row, dict):
                    out.append(
                        {
                            key: row.get(key)
                            for key in (
                                "category",
                                "description",
                                "count",
                                "count_unit",
                                "usage",
                                "usage_unit",
                                "price",
                                "price_unit",
                                "start_date",
                                "end_date",
                            )
                        }
                    )
            next_uri = payload.get("next_page_uri") if isinstance(payload, dict) else None
            url = f"https://api.twilio.com{next_uri}" if next_uri else None
            params = None
        return out

    async def balance(self) -> dict[str, str]:
        """``{"balance": "12.29", "currency": "USD"}`` of this account."""
        payload = await self._request("GET", f"{ACCOUNTS_API_BASE}/{self.account_sid}/Balance.json")
        payload = payload if isinstance(payload, dict) else {}
        return {
            "balance": str(payload.get("balance") or ""),
            "currency": str(payload.get("currency") or ""),
        }


#: A free number WhatsApp itself hands out when a business signs up with "Use a
#: display name only": a United States number in area code 555, which the
#: North American numbering plan gives to no telephone line, so a sender there
#: can only be one of these. Twilio's sign-up guide says Twilio does not
#: support them, and the Senders API still reports such a sender ONLINE, so
#: its state is no evidence either way. What an operator sees is both halves
#: failing: the Address Configuration for inbound is refused as "Invalid
#: Address" (Twilio has a help article under that title for 555 numbers) and
#: templates sent from it fail with 63027.
_WHATSAPP_PROVIDED = re.compile(r"^(?:whatsapp:)?\+?1555\d{7}$")


def is_whatsapp_provided_number(address: Any) -> bool:
    """True for a free 555 number from WhatsApp, which Twilio does not support."""
    raw = re.sub(r"[\s\-().]", "", str(address or "")).lower()
    return bool(_WHATSAPP_PROVIDED.match(raw))


def whatsapp_provided_number_problem(address: str) -> str:
    """Why a 555 sender fails and what replaces it, in words for a person."""
    return (
        f"{address} is a free number from WhatsApp (area code 555, what the 'Use a display "
        "name only' choice gives at sign-up), and Twilio does not support these numbers: "
        "Twilio refuses to route its incoming messages ('Invalid Address') and templates "
        "sent from it fail (error 63027), although the Twilio Console lists it as online. "
        "Register a Twilio phone number, or a number the business owns that is not on "
        "WhatsApp yet, as a WhatsApp sender on this Twilio account (Messaging, Senders, "
        "WhatsApp senders) under the same WhatsApp Business Account, so the approved "
        "templates carry over, and enter it as the connector's WhatsApp sender number."
    )


def _sender_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One Senders API row, as the admin toolkit reports it."""
    status = str(row.get("status") or "").upper()
    raw_props = row.get("properties")
    raw_profile = row.get("profile")
    raw_config = row.get("configuration")
    props: Mapping[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    profile: Mapping[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    config: Mapping[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    out: dict[str, Any] = {
        "sid": str(row.get("sid") or ""),
        "number": str(row.get("sender_id") or ""),
        "status": status,
        "status_means": SENDER_STATUS_HELP.get(status, ""),
        # The WhatsApp Business Account the number belongs to. A template is
        # approved on one such account and exists for its numbers only.
        "waba_id": str(config.get("waba_id") or ""),
        "display_name": str(profile.get("name") or row.get("friendly_name") or ""),
        "quality_rating": props.get("quality_rating"),
        "messaging_limit": props.get("messaging_limit"),
        "profile": {k: profile[k] for k in _PROFILE_KEYS if profile.get(k) not in (None, "", [])},
    }
    reasons = [
        str(r.get("message") or r.get("code") or "")
        for r in row.get("offline_reasons") or []
        if isinstance(r, dict)
    ]
    if reasons:
        out["offline_reasons"] = reasons
    if is_whatsapp_provided_number(out["number"]):
        out["problem"] = whatsapp_provided_number_problem(out["number"])
    return out


# account SID -> (monotonic stamp, templates)
_CACHE: dict[str, tuple[float, list[Template]]] = {}


def invalidate_templates(account_sid: str = "") -> None:
    """Forget the cached template list (one account, or all when empty)."""
    if account_sid:
        _CACHE.pop(account_sid, None)
    else:
        _CACHE.clear()
