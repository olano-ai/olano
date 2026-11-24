"""Meta's Graph API for the WhatsApp Cloud API: one client for the connector and the tools.

The WhatsApp (Meta Cloud API) connector (``whatsapp_meta_api.py``) talks to
Meta directly, with no provider in between, and everything it and the agent's
WhatsApp tools ask of Meta goes through :class:`MetaGraphClient`::

    https://graph.facebook.com/<version>
    GET    /{phone-number-id}?fields=...                the number: name, status, quality, limit
    POST   /{phone-number-id}/messages                  send (text, media, template), mark read
    POST   /{phone-number-id}/media                     upload a file (multipart) -> media id
    GET    /{media-id}                                  a received file's short-lived URL
    GET    /{phone-number-id}/whatsapp_business_profile the profile customers see
    POST   /{phone-number-id}/whatsapp_business_profile change it
    POST   /{phone-number-id}  webhook_configuration    this number's webhooks go to this URL
    GET    /{waba-id}/phone_numbers                     the Business Account's numbers
    GET    /{waba-id}/message_templates                 its templates, approval and category
    POST   /{waba-id}/message_templates                 create one (Meta reviews it at once)
    DELETE /{waba-id}/message_templates?name=&hsm_id=   delete one
    GET    /{waba-id}/subscribed_apps                   apps receiving its webhooks
    POST   /{waba-id}/subscribed_apps                   subscribe this token's app
    GET    /app                                         the app the token belongs to
    GET    /debug_token                                 the token's expiry and scopes
    GET    /me/permissions                              the token's granted permissions

Every call carries the access token as a bearer header and, when the app
secret is known, an ``appsecret_proof`` (HMAC-SHA256 of the token keyed by the
secret): an app with "Require App Secret" switched on refuses a call without
one, and one that has it off ignores it.

**A template's header and button variables get their own keys.** Meta numbers
a header's ``{{1}}`` and a website button's ``{{1}}`` separately from the
body's, so :func:`template_from_graph` rewrites them as ``{{header}}`` and
``{{button_N}}`` (N is the button's position, from 1) and keeps the original
parameter name in ``provider_data`` for :func:`template_send_components`. The
agent then fills one flat set of keys, the same way it does on Twilio.

**Delivery is only ever reported by webhook.** The Cloud API has no endpoint
that answers "what happened to message X": the connector records every status
Meta posts (sent, delivered, read, failed, with the billing category) and the
status tool reads that record.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import quote

from olano.platforms.whatsapp_templates import (
    PROFILE_VERTICALS,
    Button,
    Template,
    placeholders_in,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
#: Graph API versions live about two years; Meta's WhatsApp docs use this one
#: in September 2026, and the connector's ``graph_version`` setting overrides it.
DEFAULT_GRAPH_VERSION = "v25.0"
_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")

#: What the connector reads about its own number.
PHONE_FIELDS = (
    "id,display_phone_number,verified_name,quality_rating,status,name_status,"
    "code_verification_status,platform_type,throughput,account_mode,"
    "whatsapp_business_manager_messaging_limit,webhook_configuration"
)
_TEMPLATE_FIELDS = (
    "id,name,language,status,category,previous_category,correct_category,components,"
    "rejected_reason,quality_score,parameter_format"
)
_PROFILE_FIELDS = "about,address,description,email,profile_picture_url,websites,vertical"

#: Meta's per-kind upload limits (bytes).
MEDIA_LIMITS: dict[str, int] = {
    "image": 5 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
    "sticker": 500 * 1024,
}

#: The two permissions a Cloud API token needs.
PERMISSION_MESSAGING = "whatsapp_business_messaging"
PERMISSION_MANAGEMENT = "whatsapp_business_management"

#: The agent-facing profile categories (shared with Twilio) as Meta's enums.
VERTICAL_ENUMS: dict[str, str] = {
    "Automotive": "AUTO",
    "Beauty/Spa/Salon": "BEAUTY",
    "Clothing/Apparel": "APPAREL",
    "Education": "EDU",
    "Entertainment": "ENTERTAIN",
    "Event Planning": "EVENT_PLAN",
    "Finance/Banking": "FINANCE",
    "Food/Grocery": "GROCERY",
    "Hotel/Lodging": "HOTEL",
    "Medical/Health": "HEALTH",
    "Non-profit": "NONPROFIT",
    "Professional Services": "PROF_SERVICES",
    "Public Service": "GOVT",
    "Restaurant": "RESTAURANT",
    "Shopping/Retail": "RETAIL",
    "Travel/Transportation": "TRAVEL",
    "Other": "OTHER",
}
_VERTICAL_LABELS = {enum: label for label, enum in VERTICAL_ENUMS.items()}
assert set(VERTICAL_ENUMS) == set(PROFILE_VERTICALS)

#: A phone number's ``status`` in plain words.
NUMBER_STATUS_HELP: dict[str, str] = {
    "CONNECTED": "active: it sends and receives messages",
    "PENDING": "registration is not finished",
    "UNVERIFIED": "the number has not been verified yet",
    "DISCONNECTED": "disconnected from the Cloud API; register it again",
    "FLAGGED": "flagged for low quality; its messaging limit may drop",
    "RESTRICTED": "restricted after a policy issue; it cannot start conversations",
    "RATE_LIMITED": "temporarily rate limited",
    "BANNED": "banned by WhatsApp",
    "MIGRATED": "moved to another Business Account",
    "DELETED": "deleted",
}

#: Meta error codes a WhatsApp send or delivery commonly fails with, in plain
#: words (developers.facebook.com/documentation/business-messaging/whatsapp/
#: support/error-codes).
META_ERROR_HINTS: dict[int, str] = {
    190: "the access token expired or was revoked; store a new one",
    10: "the access token lacks a permission this needs",
    200: (
        "the token's system user has no access to this WhatsApp Business Account or number, "
        "or no token was sent"
    ),
    100: "Meta refused a parameter",
    368: "the WhatsApp Business Account is restricted or disabled for a policy violation",
    131000: "WhatsApp failed with an unknown error; try again",
    130429: "the number sends faster than its throughput allows; slow down",
    131005: "the access token lacks permission for this number",
    131008: "a required parameter is missing",
    131009: "a parameter value is not valid",
    131016: "WhatsApp is temporarily unavailable; try again shortly",
    131021: "the recipient is the business's own number",
    131026: (
        "WhatsApp could not deliver it: the number may not be on WhatsApp, may use an old "
        "app version, or has not accepted WhatsApp's latest terms"
    ),
    131030: (
        "the recipient is not on the test number's allowed list; add it in the Meta App "
        "Dashboard (WhatsApp, API Setup), or use the business's own number"
    ),
    131031: (
        "the WhatsApp Business Account is restricted or disabled for a policy violation, or "
        "Meta could not verify the request against it"
    ),
    131037: "the number's display name is not approved yet",
    131042: "a payment problem on the WhatsApp Business Account; fix it in WhatsApp Manager",
    131045: "the number is not registered with the Cloud API",
    131048: "the number is limited after too many of its messages were blocked or reported",
    131057: "the WhatsApp Business Account is in maintenance mode; try again later",
    131047: (
        "more than 24 hours passed since the person last wrote; only an approved template is "
        "delivered now"
    ),
    131049: "Meta chose not to deliver this marketing message to this person, to protect engagement",
    131050: "the person has stopped marketing messages from this business",
    131051: "WhatsApp does not support this message type",
    131052: "the person's attachment could not be downloaded",
    131053: "the file could not be uploaded to WhatsApp",
    131056: "too many messages to this person in a short time; wait before sending again",
    132000: "the number of values sent does not match the template's placeholders",
    132001: "no approved template with this name exists in this language",
    132005: "with these values the message is too long",
    132007: "the template's content breaks a WhatsApp policy",
    132012: "a value does not have the format the template expects",
    132015: "the template is paused because of low quality",
    132016: "the template is disabled",
    133010: "the number is not registered with WhatsApp",
    135000: "Meta refused the request's parameters for an unknown reason",
}

#: The billing ``pricing.type`` of a delivered message, in plain words.
PRICING_TYPE_HELP: dict[str, str] = {
    "regular": "billed at the category's rate",
    "free_customer_service": "free: sent inside the 24-hour window after the person wrote",
    "free_entry_point": "free: the person came from an ad or a Facebook Page button",
}

#: A Business-Scoped User ID: how Meta addresses a person who uses a WhatsApp
#: username and does not share a phone number ("US.13491208655302741918",
#: optionally "US.ENT.…"). Such a person has no phone number to send to.
_BSUID_RE = re.compile(r"^[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}$")
_MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def normalize_recipient(value: Any) -> str:
    """``+E164`` for a phone number, the id itself for a BSUID, else ``""``.

    Accepts the forms a person or a webhook uses: ``+1 202 555 0143``,
    ``12025550143``, ``whatsapp:+12025550143``.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if _BSUID_RE.match(raw):
        return raw
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    for junk in (" ", "-", "(", ")", "."):
        raw = raw.replace(junk, "")
    digits = raw[1:] if raw.startswith("+") else raw
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        return ""
    return f"+{digits}"


def recipient_fields(chat_id: str) -> dict[str, str]:
    """The addressing half of a ``/messages`` payload for *chat_id*.

    A phone number goes in ``to``; a BSUID goes in ``recipient``. Meta accepts
    a BSUID in ``to`` and then drops the message silently, so the difference
    matters.
    """
    if _BSUID_RE.match(chat_id):
        return {"recipient_type": "individual", "recipient": chat_id}
    return {"recipient_type": "individual", "to": chat_id.lstrip("+")}


def appsecret_proof(access_token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """True when ``X-Hub-Signature-256`` is the HMAC-SHA256 of the body under the app secret."""
    header = (header or "").strip()
    if not app_secret or not header.startswith("sha256="):
        return False
    expected = header[len("sha256=") :].strip().lower()
    computed = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed.encode(), expected.encode())


def describe_meta_error(code: Any, detail: str = "") -> str:
    """A Meta error code as a sentence the agent can relay."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return f"WhatsApp error {code}" + (f": {detail}" if detail else "")
    hint = META_ERROR_HINTS.get(number, "")
    text = f"WhatsApp error {number}"
    if hint:
        text += f": {hint}"
    if detail and detail.lower() not in hint.lower():
        text += f" ({detail})"
    return text


class MetaGraphError(RuntimeError):
    """A refused Graph API call, carrying Meta's own sentence and code."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        user_message: str = "",
        trace: str = "",
    ):
        self.status = status
        self.code = code
        self.subcode = subcode
        self.user_message = user_message
        self.trace = trace
        text = f"Meta {status}: {message}"
        if code is not None:
            text += f" (error {code})"
        if user_message and user_message not in message:
            text += f" {user_message}"
        super().__init__(text)

    @property
    def auth_refused(self) -> bool:
        """The token itself is bad (expired, revoked, malformed)."""
        return self.code == 190 or self.status == 401

    @property
    def permission_refused(self) -> bool:
        return self.code in (10, 200, 131005) or (
            self.status == 403 and self.code not in (None, 190)
        )


def graph_error(status: int, body: Any) -> MetaGraphError:
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return MetaGraphError(status, str(body)[:300] if body else "no detail")
    data = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
    detail = str(data.get("details") or "") if data else ""
    message = str(err.get("message") or "no detail")
    if detail and detail not in message:
        message = f"{message}: {detail}"

    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return MetaGraphError(
        status,
        message[:500],
        code=_int(err.get("code")),
        subcode=_int(err.get("error_subcode")),
        user_message=str(err.get("error_user_msg") or "")[:300],
        trace=str(err.get("fbtrace_id") or ""),
    )


# ── templates ────────────────────────────────────────────────────────────────


def _examples(example: Any, positional_key: str, named_key: str) -> list[tuple[str, str]]:
    """``[(key, sample)]`` from a component's ``example`` object."""
    if not isinstance(example, dict):
        return []
    named = example.get(named_key)
    if isinstance(named, list):
        return [
            (str(p.get("param_name") or ""), str(p.get("example") or ""))
            for p in named
            if isinstance(p, dict) and p.get("param_name")
        ]
    values = example.get(positional_key)
    if isinstance(values, list) and values and isinstance(values[0], list):
        values = values[0]
    if isinstance(values, list):
        return [(str(i + 1), str(v)) for i, v in enumerate(values)]
    return []


def template_from_graph(row: Mapping[str, Any]) -> Template:
    """Build a :class:`Template` from one ``message_templates`` row."""
    header = header_format = footer = body = ""
    samples: dict[str, str] = {}
    buttons: list[Button] = []
    data: dict[str, Any] = {
        "parameter_format": str(row.get("parameter_format") or "POSITIONAL").upper(),
        "buttons": {},
    }
    for component in row.get("components") or []:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").upper()
        if kind == "HEADER":
            header_format = str(component.get("format") or "TEXT").upper()
            if header_format == "TEXT":
                text = str(component.get("text") or "")
                keys = placeholders_in(text)
                if keys:
                    data["header_param"] = keys[0]
                    text = re.sub(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}", "{{header}}", text, count=1)
                    for _key, sample in _examples(
                        component.get("example"), "header_text", "header_text_named_params"
                    )[:1]:
                        samples["header"] = sample
                header = text
            elif header_format in ("IMAGE", "VIDEO", "DOCUMENT"):
                # A media header takes the file on every send: a link.
                header = f"[{header_format.lower()}: {{{{header}}}}]"
                samples["header"] = f"https link to the {header_format.lower()}"
        elif kind == "BODY":
            body = str(component.get("text") or "")
            for key, sample in _examples(
                component.get("example"), "body_text", "body_text_named_params"
            ):
                samples.setdefault(key, sample)
        elif kind == "FOOTER":
            footer = str(component.get("text") or "")
        elif kind == "BUTTONS":
            for index, raw in enumerate(component.get("buttons") or []):
                if not isinstance(raw, dict):
                    continue
                btype = str(raw.get("type") or "").upper()
                title = str(raw.get("text") or "")
                key = f"button_{index + 1}"
                if btype == "QUICK_REPLY":
                    buttons.append(Button("reply", title, title))
                elif btype == "URL":
                    url = str(raw.get("url") or "")
                    url_keys = placeholders_in(url)
                    if url_keys:
                        prefix = url.split("{{", 1)[0]
                        url = prefix + "{{" + key + "}}"
                        data["buttons"][key] = {
                            "index": index,
                            "sub_type": "url",
                            "param": url_keys[0],
                        }
                        examples = raw.get("example")
                        if isinstance(examples, list) and examples:
                            sample = str(examples[0])
                            samples[key] = (
                                sample[len(prefix) :] if sample.startswith(prefix) else sample
                            )
                    buttons.append(Button("url", title, url))
                elif btype == "PHONE_NUMBER":
                    buttons.append(Button("phone", title, str(raw.get("phone_number") or "")))
                elif btype in ("COPY_CODE", "OTP"):
                    # A copy-code button (an offer) or a one-time-password
                    # button takes its code on every send.
                    sub_type = "copy_code" if btype == "COPY_CODE" else "url"
                    data["buttons"][key] = {"index": index, "sub_type": sub_type}
                    buttons.append(Button("copy_code", title or "Copy code", "{{" + key + "}}"))
                    if raw.get("example"):
                        samples[key] = str(raw.get("example"))
                else:
                    buttons.append(Button(btype.lower() or "button", title, ""))
    status = str(row.get("status") or "").lower()
    quality = row.get("quality_score")
    rejected = str(row.get("rejected_reason") or "")
    name = str(row.get("name") or "")
    return Template(
        sid=str(row.get("id") or ""),
        friendly_name=name,
        whatsapp_name=name,
        language=str(row.get("language") or ""),
        body=body,
        samples=samples,
        category=str(row.get("category") or "").upper(),
        status=status or "pending",
        rejection_reason="" if rejected.upper() in ("", "NONE") else rejected,
        buttons=tuple(buttons),
        header=header,
        header_format="" if header_format == "TEXT" else header_format,
        footer=footer,
        previous_category=str(row.get("previous_category") or "").upper(),
        correct_category=str(row.get("correct_category") or "").upper(),
        quality=str(quality.get("score") or "") if isinstance(quality, dict) else "",
        provider_data=data,
    )


def _text_param(value: str, name: str = "") -> dict[str, str]:
    param = {"type": "text", "text": value}
    if name:
        param["parameter_name"] = name
    return param


def template_send_components(template: Template, values: Mapping[str, str]) -> list[dict[str, Any]]:
    """The ``components`` of a template send, from values keyed as the template lists them.

    The caller has already checked that every placeholder has a value.
    """
    data = template.provider_data or {}
    named = str(data.get("parameter_format") or "").upper() == "NAMED"
    components: list[dict[str, Any]] = []
    if "{{header}}" in template.header:
        value = values["header"]
        if template.header_format in ("IMAGE", "VIDEO", "DOCUMENT"):
            kind = template.header_format.lower()
            components.append(
                {"type": "header", "parameters": [{"type": kind, kind: {"link": value}}]}
            )
        else:
            param_name = str(data.get("header_param") or "") if named else ""
            components.append({"type": "header", "parameters": [_text_param(value, param_name)]})
    body_keys = placeholders_in(template.body)
    if body_keys:
        if not named:
            body_keys = sorted(body_keys, key=lambda k: int(k) if k.isdigit() else 0)
        components.append(
            {
                "type": "body",
                "parameters": [_text_param(values[k], k if named else "") for k in body_keys],
            }
        )
    for key, spec in sorted(
        (data.get("buttons") or {}).items(), key=lambda item: int(item[1].get("index", 0))
    ):
        if key not in values:
            continue
        sub_type = str(spec.get("sub_type") or "url")
        if sub_type == "copy_code":
            param: dict[str, str] = {"type": "coupon_code", "coupon_code": values[key]}
        else:
            # Meta builds the address from the value verbatim and refuses one
            # with a space or a colon in it, so the value is percent-encoded
            # (an escape the agent already wrote is kept as it is).
            param = _text_param(
                quote(values[key], safe="-_.~/%"),
                str(spec.get("param") or "") if named and spec.get("param") else "",
            )
        components.append(
            {
                "type": "button",
                "sub_type": sub_type,
                "index": str(spec.get("index", 0)),
                "parameters": [param],
            }
        )
    return components


def template_create_payload(
    *,
    name: str,
    language: str,
    category: str,
    body: str,
    samples: Mapping[str, str],
    buttons: tuple[Button, ...] | list[Button] = (),
) -> dict[str, Any]:
    """A ``message_templates`` create request from the tool's draft.

    The draft uses one set of keys across the body and a website button (the
    Twilio convention the tool documents); Meta numbers a button's variable on
    its own, so the address's placeholder becomes ``{{1}}`` here with the
    sample completing the example address.
    """
    keys = placeholders_in(body)
    named = any(not k.isdigit() for k in keys) or any(
        not k.isdigit() for b in buttons for k in placeholders_in(b.value)
    )
    body_component: dict[str, Any] = {"type": "BODY", "text": body}
    if keys and named:
        body_component["example"] = {
            "body_text_named_params": [{"param_name": k, "example": samples[k]} for k in keys]
        }
    elif keys:
        ordered = sorted(keys, key=int)
        body_component["example"] = {"body_text": [[samples[k] for k in ordered]]}
    components: list[dict[str, Any]] = [body_component]
    if buttons:
        out: list[dict[str, Any]] = []
        for button in buttons:
            if button.kind == "reply":
                out.append({"type": "QUICK_REPLY", "text": button.title})
            elif button.kind == "url":
                url_keys = placeholders_in(button.value)
                if url_keys:
                    # A named template names the address's variable too; a
                    # numbered one numbers it on its own, from 1.
                    prefix = button.value.split("{{", 1)[0]
                    variable = "{{" + url_keys[0] + "}}" if named else "{{1}}"
                    out.append(
                        {
                            "type": "URL",
                            "text": button.title,
                            "url": prefix + variable,
                            "example": [prefix + samples[url_keys[0]]],
                        }
                    )
                else:
                    out.append({"type": "URL", "text": button.title, "url": button.value})
            elif button.kind == "phone":
                out.append(
                    {"type": "PHONE_NUMBER", "text": button.title, "phone_number": button.value}
                )
        components.append({"type": "BUTTONS", "buttons": out})
    payload: dict[str, Any] = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }
    if named:
        payload["parameter_format"] = "named"
    return payload


# ── numbers ──────────────────────────────────────────────────────────────────


def number_row(row: Mapping[str, Any], waba_id: str = "") -> dict[str, Any]:
    """One phone number, flattened like the Twilio sender rows the tools report."""
    status = str(row.get("status") or "").upper()
    limit = row.get("whatsapp_business_manager_messaging_limit") or row.get("messaging_limit_tier")
    if isinstance(limit, dict):
        limit = limit.get("current_limit") or limit.get("tier") or ""
    throughput = row.get("throughput")
    out: dict[str, Any] = {
        "sid": str(row.get("id") or ""),
        "phone_number_id": str(row.get("id") or ""),
        "number": normalize_recipient(row.get("display_phone_number")),
        "status": status,
        "status_means": NUMBER_STATUS_HELP.get(status, ""),
        "waba_id": waba_id,
        "display_name": str(row.get("verified_name") or ""),
        "name_status": str(row.get("name_status") or ""),
        "quality_rating": row.get("quality_rating"),
        "messaging_limit": limit,
    }
    if isinstance(throughput, dict) and throughput.get("level"):
        out["throughput"] = throughput.get("level")
    if row.get("account_mode"):
        out["mode"] = row.get("account_mode")
    if row.get("code_verification_status"):
        out["verification"] = row.get("code_verification_status")
    return out


def profile_from_graph(row: Mapping[str, Any]) -> dict[str, Any]:
    """A business profile in the tools' vocabulary (vertical as a label)."""
    out = {k: row.get(k) for k in ("about", "address", "description", "email") if row.get(k)}
    websites = row.get("websites")
    if isinstance(websites, list) and websites:
        out["websites"] = [str(w) for w in websites]
    vertical = str(row.get("vertical") or "")
    if vertical and vertical not in ("UNDEFINED", "NOT_A_BIZ"):
        out["vertical"] = _VERTICAL_LABELS.get(vertical, vertical)
    return out


# ── the client ───────────────────────────────────────────────────────────────

_CACHE_TTL_SECONDS = 60.0
_MAX_PAGES = 20
# waba id -> (monotonic stamp, templates)
_TEMPLATE_CACHE: dict[str, tuple[float, list[Template]]] = {}


def invalidate_templates(waba_id: str = "") -> None:
    if waba_id:
        _TEMPLATE_CACHE.pop(waba_id, None)
    else:
        _TEMPLATE_CACHE.clear()


class MetaGraphClient:
    """Async client for one WhatsApp Business Account and one of its numbers.

    Used two ways: the connector keeps one open for its lifetime
    (:meth:`open` / :meth:`aclose`, one connection pool for every send), and a
    tool builds one per call and never opens it, in which case each request
    uses a short-lived client.
    """

    def __init__(
        self,
        access_token: str,
        *,
        waba_id: str = "",
        phone_number_id: str = "",
        app_secret: str = "",
        version: str = DEFAULT_GRAPH_VERSION,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        self.access_token = access_token
        self.waba_id = str(waba_id or "").strip()
        self.phone_number_id = str(phone_number_id or "").strip()
        self.app_secret = app_secret
        self.version = version if _VERSION_RE.match(version or "") else DEFAULT_GRAPH_VERSION
        self._timeout = timeout
        # Tests inject an ``httpx.MockTransport``; production uses the default.
        self._transport = transport
        self._client: Any = None

    # ── plumbing ──────────────────────────────────────────────────────────

    def _new_client(self) -> Any:
        import httpx

        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def open(self) -> None:
        if self._client is None:
            self._client = self._new_client()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def url(self, path: str) -> str:
        return f"{GRAPH_BASE}/{self.version}/{path.lstrip('/')}"

    def _auth(self, params: Optional[dict[str, Any]]) -> dict[str, Any]:
        out = dict(params or {})
        if self.app_secret:
            out["appsecret_proof"] = appsecret_proof(self.access_token, self.app_secret)
        return out

    async def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        files: Any = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("https://") else self.url(path_or_url)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        kwargs: dict[str, Any] = {"params": self._auth(params), "headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files
        if self._client is not None:
            resp = await self._client.request(method, url, **kwargs)
        else:
            async with self._new_client() as client:
                resp = await client.request(method, url, **kwargs)
        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            payload = {"error": {"message": resp.text[:300]}}
        if resp.status_code >= 400:
            raise graph_error(resp.status_code, payload)
        return payload

    async def _pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every ``data`` row of a paginated edge."""
        out: list[dict[str, Any]] = []
        url: Optional[str] = path
        query: Optional[dict[str, Any]] = params
        for _ in range(_MAX_PAGES):
            if not url:
                break
            payload = await self.request("GET", url, params=query)
            rows = payload.get("data") if isinstance(payload, dict) else None
            out.extend(r for r in rows or [] if isinstance(r, dict))
            paging = payload.get("paging") if isinstance(payload, dict) else None
            nxt = paging.get("next") if isinstance(paging, dict) else None
            # The next link carries its own query (and the token, which the
            # header also sends); keep ours out of it.
            url, query = (str(nxt), None) if nxt else (None, None)
        return out

    # ── the number, the token, the app ────────────────────────────────────

    async def phone_number(self, fields: str = PHONE_FIELDS) -> dict[str, Any]:
        payload = await self.request("GET", self.phone_number_id, params={"fields": fields})
        return payload if isinstance(payload, dict) else {}

    async def waba_numbers(self) -> list[dict[str, Any]]:
        return await self._pages(
            f"{self.waba_id}/phone_numbers",
            {"fields": PHONE_FIELDS.replace(",webhook_configuration", ""), "limit": 100},
        )

    async def app(self) -> dict[str, Any]:
        payload = await self.request("GET", "app")
        return payload if isinstance(payload, dict) else {}

    async def debug_token(self, app_id: str) -> dict[str, Any]:
        """The token's type, expiry and scopes (needs the app secret)."""
        url = self.url("debug_token")
        params = {"input_token": self.access_token, "access_token": f"{app_id}|{self.app_secret}"}
        async with self._new_client() as client:
            resp = await client.get(url, params=params)
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code >= 400:
            raise graph_error(resp.status_code, payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    async def permissions(self) -> list[dict[str, Any]]:
        payload = await self.request("GET", "me/permissions")
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    # ── webhooks ──────────────────────────────────────────────────────────

    async def subscribed_apps(self) -> list[dict[str, Any]]:
        payload = await self.request("GET", f"{self.waba_id}/subscribed_apps")
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def subscribe_app(self) -> dict[str, Any]:
        """Subscribe this token's app to the Business Account's webhooks."""
        payload = await self.request("POST", f"{self.waba_id}/subscribed_apps")
        return payload if isinstance(payload, dict) else {}

    async def set_number_callback(self, url: str, verify_token: str) -> dict[str, Any]:
        """Send this number's webhooks to *url* (Meta verifies it with a GET first)."""
        body = {
            "webhook_configuration": {"override_callback_uri": url, "verify_token": verify_token}
        }
        payload = await self.request("POST", self.phone_number_id, json_body=body)
        return payload if isinstance(payload, dict) else {}

    async def set_waba_callback(self, url: str, verify_token: str) -> dict[str, Any]:
        """Send the whole Business Account's webhooks to *url*."""
        body = {"override_callback_uri": url, "verify_token": verify_token}
        payload = await self.request("POST", f"{self.waba_id}/subscribed_apps", json_body=body)
        return payload if isinstance(payload, dict) else {}

    # ── templates ─────────────────────────────────────────────────────────

    async def list_templates(self, *, fresh: bool = False) -> list[Template]:
        """Every template on the Business Account, cached for a minute."""
        cached = _TEMPLATE_CACHE.get(self.waba_id)
        if cached and not fresh and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return list(cached[1])
        rows = await self._pages(
            f"{self.waba_id}/message_templates", {"fields": _TEMPLATE_FIELDS, "limit": 100}
        )
        out = [template_from_graph(r) for r in rows]
        _TEMPLATE_CACHE[self.waba_id] = (time.monotonic(), list(out))
        return out

    async def get_template(self, template_id: str) -> Template:
        row = await self.request("GET", template_id, params={"fields": _TEMPLATE_FIELDS})
        return template_from_graph(row if isinstance(row, dict) else {})

    async def create_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create (and so submit) a template; ``{"id", "status", "category"}``."""
        result = await self.request("POST", f"{self.waba_id}/message_templates", json_body=payload)
        invalidate_templates(self.waba_id)
        return result if isinstance(result, dict) else {}

    async def delete_template(self, name: str, template_id: str = "") -> None:
        params = {"name": name}
        if template_id:
            params["hsm_id"] = template_id
        await self.request("DELETE", f"{self.waba_id}/message_templates", params=params)
        invalidate_templates(self.waba_id)

    # ── profile ───────────────────────────────────────────────────────────

    async def business_profile(self, phone_number_id: str = "") -> dict[str, Any]:
        payload = await self.request(
            "GET",
            f"{phone_number_id or self.phone_number_id}/whatsapp_business_profile",
            params={"fields": _PROFILE_FIELDS},
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        return profile_from_graph(first)

    async def update_business_profile(
        self, changes: Mapping[str, Any], phone_number_id: str = ""
    ) -> None:
        body: dict[str, Any] = {"messaging_product": "whatsapp"}
        for key, value in changes.items():
            if key == "vertical":
                body["vertical"] = VERTICAL_ENUMS.get(str(value), str(value))
            elif key == "websites":
                body["websites"] = [str(w) for w in value]
            else:
                body[key] = value
        await self.request(
            "POST",
            f"{phone_number_id or self.phone_number_id}/whatsapp_business_profile",
            json_body=body,
        )

    # ── messaging ─────────────────────────────────────────────────────────

    async def send_message(self, payload: dict[str, Any]) -> str:
        """POST one ``/messages`` payload; the new message's id (``wamid.…``)."""
        body = {"messaging_product": "whatsapp", **payload}
        result = await self.request("POST", f"{self.phone_number_id}/messages", json_body=body)
        messages = result.get("messages") if isinstance(result, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            return str(messages[0].get("id") or "")
        return ""

    async def mark_read(self, message_id: str, *, typing: bool = False) -> None:
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            body["typing_indicator"] = {"type": "text"}
        await self.request("POST", f"{self.phone_number_id}/messages", json_body=body)

    async def upload_media(self, filename: str, data: bytes, mime: str) -> str:
        files = {"file": (filename, data, mime)}
        form = {"messaging_product": "whatsapp", "type": mime}
        result = await self.request("POST", f"{self.phone_number_id}/media", data=form, files=files)
        return str(result.get("id") or "") if isinstance(result, dict) else ""

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        """A received file's bytes and MIME type (two steps: its URL, then the bytes)."""
        if not _MEDIA_ID_RE.match(media_id):
            raise MetaGraphError(400, f"malformed media id {media_id[:40]!r}")
        meta = await self.request("GET", media_id)
        url = str(meta.get("url") or "") if isinstance(meta, dict) else ""
        mime = str(meta.get("mime_type") or "") if isinstance(meta, dict) else ""
        size = int(meta.get("file_size") or 0) if isinstance(meta, dict) else 0
        if size and size > max_bytes:
            raise MetaGraphError(413, f"the file is {size} bytes, over the {max_bytes} limit")
        if not url.startswith("https://"):
            raise MetaGraphError(502, "Meta gave no download address for the file")
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if self._client is not None:
            resp = await self._client.get(url, headers=headers)
        else:
            async with self._new_client() as client:
                resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise MetaGraphError(resp.status_code, "the file could not be downloaded")
        if len(resp.content) > max_bytes:
            raise MetaGraphError(413, f"the file is over the {max_bytes} limit")
        return resp.content, mime
