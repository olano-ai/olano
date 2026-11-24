"""The Meta Cloud API side of the WhatsApp Business toolkit.

``whatsapp_business_tools`` holds the agent's WhatsApp tools, and every one of
them works the same way on either business connector: WhatsApp through Twilio
or WhatsApp through Meta's own Cloud API. Each tool asks :func:`meta_context`
first; when the agent's WhatsApp is Meta's, the tool hands its (already
validated) arguments to the matching function here, and otherwise it runs its
Twilio path, which is unchanged. Keeping the Meta half in its own module keeps
the Twilio half readable and untouched.

Which account a tool acts on: the agent's WhatsApp (Meta Cloud API) connector
when it has one (the number it sends from), else ``WHATSAPP_META_ACCESS_TOKEN``
plus ``WHATSAPP_META_WABA_ID`` (and ``WHATSAPP_META_PHONE_NUMBER_ID`` for the
profile) from this agent's vault, for an administrator's agent that manages
the account without sending from it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

from olano.platforms.whatsapp_meta_graph import (
    MetaGraphClient,
    MetaGraphError,
    normalize_recipient,
    number_row,
    template_create_payload,
)
from olano.platforms.whatsapp_templates import (
    CREATABLE_CATEGORIES,
    STATUS_HELP,
    Button,
    Template,
    ambiguous_matches,
    find_template,
)

META_KEY = "whatsapp_meta_api"

#: What each WhatsApp delivery status means, for the person reading the answer.
STATUS_MEANS = {
    "sent": "WhatsApp accepted it; it has not reached the phone yet",
    "delivered": "it reached the person's phone",
    "read": "the person opened it",
    "played": "the person played the voice message",
}

_REVIEW_NOTE = (
    "WhatsApp usually reviews a template within 24 hours, often within minutes. Check with "
    "whatsapp_template_status; once it reads approved, whatsapp_template_send can use it."
)


@dataclass
class MetaContext:
    """The Graph client a tool uses, and the connector behind it when there is one."""

    graph: MetaGraphClient
    adapter: Any = None

    @property
    def waba_id(self) -> str:
        return self.graph.waba_id


def ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def refuse(message: str, *, guidance: str = "", **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message}
    if guidance:
        payload["agent_guidance"] = guidance
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def is_meta_adapter(adapter: Any) -> bool:
    return adapter is not None and callable(getattr(adapter, "graph_client", None))


def meta_context(adapter: Any, get_credential: Any) -> Optional[MetaContext]:
    """The Meta account this agent's WhatsApp tools act on, or ``None`` (Twilio's turn)."""
    if is_meta_adapter(adapter):
        return MetaContext(adapter.graph_client(), adapter)
    if adapter is not None:
        return None  # the agent's WhatsApp is Twilio's
    token = str(get_credential("WHATSAPP_META_ACCESS_TOKEN") or "").strip()
    waba = str(get_credential("WHATSAPP_META_WABA_ID") or "").strip()
    if not token or not waba:
        return None
    return MetaContext(
        MetaGraphClient(
            token,
            waba_id=waba,
            phone_number_id=str(get_credential("WHATSAPP_META_PHONE_NUMBER_ID") or "").strip(),
            app_secret=str(get_credential("WHATSAPP_META_APP_SECRET") or "").strip(),
        )
    )


def meta_refusal(exc: Exception) -> str:
    if isinstance(exc, MetaGraphError) and exc.auth_refused:
        return refuse(
            f"Meta refused this account's access token ({exc}).",
            guidance=(
                "Do not retry. The token expired or was revoked; the person stores a new system "
                "user token (Business settings, System users) in the connector or the vault."
            ),
        )
    if isinstance(exc, MetaGraphError) and exc.permission_refused:
        return refuse(
            f"Meta refused this for lack of permission ({exc}).",
            guidance=(
                "Do not retry. The token needs whatsapp_business_management (templates, numbers, "
                "profile) and whatsapp_business_messaging (sending), with access to this "
                "WhatsApp Business Account; the person grants them in Business settings."
            ),
        )
    return refuse(f"Meta could not complete this ({exc}).")


def resolve(
    templates: list[Template], ref: str, *, approved_only: bool, language: str = ""
) -> tuple[Optional[Template], str]:
    pool = [t for t in templates if t.approved] if approved_only else templates
    found = find_template(pool, ref, language)
    if found is not None:
        return found, ""
    same_name = ambiguous_matches(pool, ref, language)
    if len(same_name) > 1:
        return None, refuse(
            f"Several templates are named {ref!r}, one per language.",
            guidance="Pass the language, or the sid, of the one you mean.",
            candidates=[t.describe() for t in same_name],
        )
    if approved_only:
        other = find_template(templates, ref, language)
        if other is not None:
            return None, refuse(
                f"Template {other.name} is {other.status}, not approved, so WhatsApp would not "
                "deliver it.",
                guidance="Pick an approved one from whatsapp_template_list.",
            )
    return None, refuse(
        f"No template named {ref!r} on this WhatsApp Business Account"
        + (f" in {language}" if language else "")
        + ".",
        guidance="List them with whatsapp_template_list (or whatsapp_template_status) and use a "
        "name or id from it.",
    )


# ── everyday ─────────────────────────────────────────────────────────────────


async def template_list(ctx: MetaContext, search: str) -> str:
    try:
        templates = await ctx.graph.list_templates()
    except Exception as exc:
        return meta_refusal(exc)
    approved = [t for t in templates if t.approved]
    words = [w for w in search.lower().split() if w]
    if words:
        approved = [
            t for t in approved if all(w in f"{t.name} {t.header} {t.body}".lower() for w in words)
        ]
    rows = [t.describe(with_status=False) for t in approved]
    result: dict[str, object] = {"approved_templates": rows, "count": len(rows)}
    if not rows:
        waiting = sum(1 for t in templates if t.status in ("pending", "in_appeal"))
        result["note"] = (
            "No approved template matches."
            + (f" {waiting} template(s) are still in WhatsApp's review." if waiting else "")
            + " Templates are created by the agent that manages the WhatsApp Business Account, "
            "or in WhatsApp Manager (Account tools, Message templates)."
        )
    return ok(result)


def _billing(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    from olano.platforms.whatsapp_meta_api import billing_summary

    return billing_summary(record)


async def template_send(
    ctx: MetaContext,
    to: str,
    ref: str,
    variables: dict[str, str],
    language: str,
    *,
    polls: int,
    pause: float,
) -> str:
    adapter = ctx.adapter
    try:
        templates = await ctx.graph.list_templates()
    except Exception as exc:
        return meta_refusal(exc)
    found, refusal = resolve(templates, ref, approved_only=True, language=language)
    if found is None:
        return refusal
    result = await adapter.send_template(to, found, variables)
    if not result.success:
        return refuse(
            str(result.error or "The template could not be sent."),
            template=found.describe(with_status=False),
        )
    raw = result.raw_response if isinstance(result.raw_response, dict) else {}
    status, detail = "", ""
    for attempt in range(polls):
        if attempt or pause:
            await asyncio.sleep(pause)
        status, detail = await adapter.message_status(to, result.message_id)
        if status:
            break
    record = adapter.message_record(result.message_id)
    if status == "failed":
        guidance = "Tell the person why; do not resend the same message unchanged."
        if record.get("error_code") == 132001:
            guidance = (
                f"WhatsApp has no approved {found.name} in {found.language} for this number. "
                "Check the template in WhatsApp Manager (Message templates) under Business "
                f"Account {ctx.waba_id}; do not resend it unchanged."
            )
        return refuse(
            f"WhatsApp did not deliver the template: {detail or status}.",
            guidance=guidance,
            message_sid=result.message_id,
        )
    out: dict[str, Any] = {
        "sent": True,
        "status": status or "accepted",
        "status_means": (
            STATUS_MEANS.get(status, "WhatsApp confirmed it")
            if status
            else "Meta accepted it; WhatsApp had not confirmed delivery yet"
        ),
        "to": to,
        "template": found.name,
        "language": found.language,
        "category": found.category,
        "text": raw.get("rendered", ""),
        "message_sid": result.message_id,
    }
    billing = _billing(record)
    if billing:
        out["billing"] = billing
    return ok(out)


async def message_status(ctx: MetaContext, to: str, message_id: str) -> str:
    adapter = ctx.adapter
    status, detail = await adapter.message_status(to, message_id)
    record = adapter.message_record(message_id)
    if status == "failed":
        return refuse(
            f"WhatsApp did not deliver it: {detail or status}.",
            guidance="Tell the person why; do not resend the same message unchanged.",
            message_sid=message_id,
        )
    out: dict[str, Any] = {
        "message_sid": message_id,
        "to": to,
        "status": status or "unknown",
        "status_means": STATUS_MEANS.get(status, detail or "WhatsApp has not reported yet"),
    }
    if record.get("template"):
        out["template"] = record["template"]
    billing = _billing(record)
    if billing:
        out["billing"] = billing
    return ok(out)


# ── administration ───────────────────────────────────────────────────────────


async def template_create(
    ctx: MetaContext,
    *,
    name: str,
    body: str,
    category: str,
    language: str,
    samples: dict[str, str],
    buttons: list[Button],
    submit: bool,
) -> str:
    if not submit:
        return refuse(
            "Not created: on the Meta Cloud API a template goes to WhatsApp's review the moment "
            "it is created, so there is no unsubmitted draft.",
            guidance="Call again with submit=true once the person has approved the text.",
        )
    try:
        existing = await ctx.graph.list_templates(fresh=True)
    except Exception as exc:
        return meta_refusal(exc)
    clash = [t for t in existing if t.name.lower() == name and t.language == language]
    if clash:
        return refuse(
            f"A template named {name} already exists in {language} ({clash[0].status}).",
            guidance="Pick another name, or delete the old one first if it is not needed.",
            existing=clash[0].describe(),
        )
    payload = template_create_payload(
        name=name,
        language=language,
        category=category,
        body=body.strip(),
        samples=samples,
        buttons=buttons,
    )
    try:
        created = await ctx.graph.create_template(payload)
    except Exception as exc:
        return meta_refusal(exc)
    status = str(created.get("status") or "pending").lower()
    result: dict[str, Any] = {
        "created": {"name": name, "sid": created.get("id"), "language": language},
        "submitted": True,
        "status": status,
        "status_means": STATUS_HELP.get(status, ""),
        "category": str(created.get("category") or category).upper(),
        "next": _REVIEW_NOTE,
    }
    if result["category"] != category:
        result["category_note"] = (
            f"WhatsApp filed it as {result['category']}, not {category}; it is billed as "
            f"{result['category']}."
        )
    return ok(result)


async def template_submit(ctx: MetaContext, ref: str) -> str:
    try:
        templates = await ctx.graph.list_templates(fresh=True)
    except Exception as exc:
        return meta_refusal(exc)
    found, refusal = resolve(templates, ref, approved_only=False)
    if found is None:
        return refusal
    return refuse(
        f"Template {found.name} is already with WhatsApp ({found.status}: "
        f"{STATUS_HELP.get(found.status, '')}): the Meta Cloud API submits a template when it "
        "is created.",
        guidance=(
            "A rejected template is fixed by creating a corrected copy with "
            "whatsapp_template_create under a new name (or editing it in WhatsApp Manager)."
            if found.status == "rejected"
            else "Nothing to do; check progress with whatsapp_template_status."
        ),
    )


async def template_status(ctx: MetaContext, ref: str) -> str:
    try:
        templates = await ctx.graph.list_templates(fresh=True)
    except Exception as exc:
        return meta_refusal(exc)
    if ref.strip():
        found, refusal = resolve(templates, ref, approved_only=False)
        if found is None:
            return refusal
        return ok({"template": found.describe()})
    counts: dict[str, int] = {}
    for t in templates:
        counts[t.status] = counts.get(t.status, 0) + 1
    return ok(
        {
            "whatsapp_business_account": ctx.waba_id,
            "templates": [t.describe() for t in templates],
            "count_by_status": counts,
            "categories_you_can_create": list(CREATABLE_CATEGORIES),
        }
    )


async def template_delete(ctx: MetaContext, ref: str, confirm_name: str) -> str:
    try:
        templates = await ctx.graph.list_templates(fresh=True)
    except Exception as exc:
        return meta_refusal(exc)
    found, refusal = resolve(templates, ref, approved_only=False)
    if found is None:
        return refusal
    if confirm_name.strip().lower() not in {found.name.lower(), found.sid.lower()}:
        return refuse(
            f"Not deleted: confirm_name {confirm_name!r} does not match the template's name "
            f"{found.name!r}.",
            guidance="Pass the exact name only after the person confirmed this deletion.",
        )
    try:
        await ctx.graph.delete_template(found.name, found.sid)
    except Exception as exc:
        return meta_refusal(exc)
    return ok(
        {"deleted": True, "template": found.name, "sid": found.sid, "language": found.language}
    )


async def sender_status(ctx: MetaContext) -> str:
    try:
        rows = await ctx.graph.waba_numbers()
    except Exception as exc:
        return meta_refusal(exc)
    senders = [number_row(r, ctx.waba_id) for r in rows]
    for row in senders:
        try:
            row["profile"] = await ctx.graph.business_profile(row["phone_number_id"])
        except Exception:
            row["profile"] = {}
    result: dict[str, object] = {
        "whatsapp_business_account": ctx.waba_id,
        "senders": senders,
        "count": len(senders),
    }
    if ctx.adapter is not None:
        number_id = str(getattr(ctx.adapter, "phone_number_id", "") or "")
        result["this_agent_sends_from"] = str(getattr(ctx.adapter, "sender_address", "") or "")
        if number_id and number_id not in {r["phone_number_id"] for r in senders}:
            result["note"] = (
                f"This agent's connector sends from phone number ID {number_id}, which is not on "
                f"WhatsApp Business Account {ctx.waba_id}: check both IDs in the connector."
            )
    if not senders:
        result["note"] = (
            "No phone number is on this WhatsApp Business Account yet: add one in WhatsApp "
            "Manager (Phone numbers) or the Meta App Dashboard (WhatsApp, API Setup)."
        )
    return ok(result)


async def profile_update(ctx: MetaContext, number: str, changes: dict[str, Any]) -> str:
    """Apply validated *changes* (the Twilio tool's shape) to one number's profile."""
    meta_changes: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "emails":
            meta_changes["email"] = value[0]["email"] if value else ""
        elif key == "websites":
            meta_changes["websites"] = [w["website"] for w in value]
        else:
            meta_changes[key] = value
    target_id = ctx.graph.phone_number_id
    target_number = str(getattr(ctx.adapter, "sender_address", "") or "")
    if str(number or "").strip():
        wanted = normalize_recipient(number)
        try:
            rows = await ctx.graph.waba_numbers()
        except Exception as exc:
            return meta_refusal(exc)
        found = next(
            (r for r in rows if normalize_recipient(r.get("display_phone_number")) == wanted), None
        )
        if found is None:
            return refuse(
                f"{wanted or number} is not a number on WhatsApp Business Account {ctx.waba_id}.",
                guidance="whatsapp_sender_status lists the numbers that are.",
                numbers=[normalize_recipient(r.get("display_phone_number")) for r in rows],
            )
        target_id, target_number = str(found.get("id") or ""), wanted
    if not target_id:
        return refuse(
            "No number given and no phone number ID is known to take one from.",
            guidance="Pass the number; whatsapp_sender_status lists the account's numbers.",
        )
    try:
        await ctx.graph.update_business_profile(meta_changes, target_id)
        profile = await ctx.graph.business_profile(target_id)
    except Exception as exc:
        return meta_refusal(exc)
    return ok({"updated": True, "number": target_number or target_id, "profile": profile})
