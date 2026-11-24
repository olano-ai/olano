"""HubSpot CRM toolkit for the HubSpotAgent subagent.

A batteries-included wrapper around the official HubSpot Python SDK
(``hubspot-api-client``, imported as ``hubspot``) so agents don't have to
fight HubSpot's remote MCP server. One access token (``HUBSPOT_ACCESS_TOKEN``)
drives the whole toolkit: either the HubSpot connector's OAuth token
(Connections → HubSpot, refreshed by the broker every 30 minutes) or a
private-app token pasted into the vault.

Coverage mirrors what HubSpot exposes through its CRM API:

  • Records  — contacts, companies, deals, tickets, products, line items
  • Activities (engagements) — notes, tasks, calls, emails, meetings
  • Pipelines & stages, owners/users
  • Associations between any two object types
  • Generic CRUD + search over any object type for everything else
    (carts, invoices, orders, quotes, subscriptions, …)

All writes respect the access token's scopes and the underlying user's
HubSpot permissions. The toolkit never fabricates IDs — every mutation
echoes back the object type and the ID HubSpot returned.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_HS_CREDS_MSG = (
    "[missing-credentials] HUBSPOT_ACCESS_TOKEN not set. Connect HubSpot under "
    'Connections (or send a sign-in link with magic_link_connect_service("hubspot")), '
    "or store a private-app token in the vault."
)

# HubSpot answers a call the token's scopes do not cover with 403 and
# ``"category":"MISSING_SCOPES"``. On an OAuth connection that is not a dead
# sign-in: the connector asks for most scopes as optional, and HubSpot drops
# the ones the portal's plan or the installing user does not have.
_MISSING_SCOPE_HINT = (
    " HubSpot says this connection lacks the scope this call needs. If the "
    "HubSpot plan includes the feature, reconnect HubSpot "
    '(magic_link_connect_service("hubspot")) as a user with access to it, or add '
    "the scope to the private-app token; otherwise the account's plan does not "
    "offer it."
)


def _with_scope_hint(text: str) -> str:
    """Append the missing-scope explanation when *text* is HubSpot's 403."""
    if "MISSING_SCOPES" in text or "required scopes" in text:
        return text + _MISSING_SCOPE_HINT
    return text


def _fail(where: str, exc: Exception) -> str:
    """A tool's error result for an SDK exception."""
    return _with_scope_hint(f"[tool-error] {where}: {exc}")


# Default property sets so list/get calls return something useful without the
# caller having to enumerate fields. HubSpot returns a small default set
# otherwise (often just id + timestamps).
_DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "contacts": [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
    ],
    "companies": ["name", "domain", "industry", "city", "country", "phone", "numberofemployees"],
    "deals": ["dealname", "amount", "dealstage", "pipeline", "closedate", "dealtype"],
    "tickets": ["subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority"],
    "products": ["name", "price", "description", "hs_sku"],
    "line_items": ["name", "quantity", "price", "amount", "hs_product_id"],
    "notes": ["hs_note_body", "hs_timestamp"],
    "tasks": [
        "hs_task_subject",
        "hs_task_body",
        "hs_task_status",
        "hs_task_priority",
        "hs_timestamp",
    ],
    "calls": ["hs_call_title", "hs_call_body", "hs_call_status", "hs_timestamp"],
    "emails": ["hs_email_subject", "hs_email_text", "hs_timestamp"],
    "meetings": ["hs_meeting_title", "hs_meeting_body", "hs_meeting_start_time", "hs_timestamp"],
}

# Activity (engagement) object types and their primary body/subject fields,
# used by hubspot_log_activity to map a plain message onto HubSpot's schema.
_ACTIVITY_PROPS: dict[str, dict[str, str]] = {
    "note": {"_object": "notes", "body": "hs_note_body"},
    "task": {"_object": "tasks", "subject": "hs_task_subject", "body": "hs_task_body"},
    "call": {"_object": "calls", "subject": "hs_call_title", "body": "hs_call_body"},
    "email": {"_object": "emails", "subject": "hs_email_subject", "body": "hs_email_text"},
    "meeting": {"_object": "meetings", "subject": "hs_meeting_title", "body": "hs_meeting_body"},
}


def _hs_client() -> tuple[Any, str | None]:
    """Return (client, error_string). One is always None."""
    hs_mod, err = optional_import(
        "hubspot", purpose="HubSpot CRM access", pip_name="hubspot-api-client"
    )
    if err or hs_mod is None:
        return None, err
    token = get_credential("HUBSPOT_ACCESS_TOKEN")
    if not token:
        return None, _HS_CREDS_MSG
    try:
        return hs_mod.HubSpot(access_token=token), None
    except Exception as exc:
        return None, f"[tool-error] HubSpot auth: {exc}"


def _props_for(object_type: str, extra: str = "") -> list[str]:
    """Resolve the property list for a read, honouring a caller override."""
    if extra.strip():
        return [p.strip() for p in extra.split(",") if p.strip()]
    return _DEFAULT_PROPERTIES.get(object_type, [])


def _summarize(obj, object_type: str = "") -> str:
    """One-line summary of a HubSpot object returned by the SDK."""
    props = getattr(obj, "properties", {}) or {}
    oid = getattr(obj, "id", "?")
    # Prefer a human label when one of the well-known name fields is present.
    label = (
        props.get("dealname")
        or props.get("name")
        or props.get("subject")
        or props.get("hs_task_subject")
        or props.get("hs_note_body")
        or " ".join(v for v in (props.get("firstname", ""), props.get("lastname", "")) if v).strip()
    )
    email = props.get("email")
    parts = [f"id={oid}"]
    if label:
        parts.insert(0, str(label)[:80])
    if email:
        parts.append(f"<{email}>")
    return " ".join(parts)


def _detail(obj, object_type: str) -> str:
    """Multi-line property dump of a single object."""
    props = getattr(obj, "properties", {}) or {}
    oid = getattr(obj, "id", "?")
    lines = [f"{object_type} {oid}:"]
    for k, v in props.items():
        if v not in (None, ""):
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


# ── Generic CRUD / search (works for any object type) ───────────────────────


@tool
def hubspot_list_objects(
    object_type: str = "contacts", limit: int = 10, properties: str = ""
) -> str:
    """List HubSpot records of any object type.

    object_type: contacts, companies, deals, tickets, products, line_items,
    notes, tasks, calls, emails, meetings, or any custom object. properties:
    optional comma-separated field names to return.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        page = client.crm.objects.basic_api.get_page(
            object_type=object_type,
            limit=min(max(limit, 1), 100),
            properties=_props_for(object_type, properties) or None,
        )
        results = getattr(page, "results", []) or []
        if not results:
            return f"No {object_type} found."
        lines = [f"{object_type} ({len(results)}):"]
        lines += [f"  {_summarize(o, object_type)}" for o in results]
        return "\n".join(lines)
    except Exception as exc:
        return _fail(f"hubspot_list_objects({object_type})", exc)


@tool
def hubspot_get_object(object_type: str, object_id: str, properties: str = "") -> str:
    """Get a single HubSpot record of any object type by its ID."""
    client, err = _hs_client()
    if err:
        return err
    try:
        obj = client.crm.objects.basic_api.get_by_id(
            object_type=object_type,
            object_id=object_id,
            properties=_props_for(object_type, properties) or None,
        )
        return _detail(obj, object_type)
    except Exception as exc:
        return _fail(f"hubspot_get_object({object_type}/{object_id})", exc)


@tool
def hubspot_search(object_type: str, query: str, property_name: str = "", limit: int = 10) -> str:
    """Search any HubSpot object type.

    With property_name set, matches that property with CONTAINS_TOKEN; without
    it, runs a full-text search across default searchable properties.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        from hubspot.crm.objects import (
            Filter,
            FilterGroup,
            PublicObjectSearchRequest,
        )

        kwargs: dict = {"limit": min(max(limit, 1), 100)}
        props = _props_for(object_type)
        if props:
            kwargs["properties"] = props
        if property_name.strip():
            kwargs["filter_groups"] = [
                FilterGroup(
                    filters=[
                        Filter(
                            property_name=property_name.strip(),
                            operator="CONTAINS_TOKEN",
                            value=query,
                        )
                    ]
                )
            ]
        else:
            kwargs["query"] = query
        results = client.crm.objects.search_api.do_search(
            object_type=object_type,
            public_object_search_request=PublicObjectSearchRequest(**kwargs),
        )
        items = getattr(results, "results", []) or []
        if not items:
            return f"No {object_type} matched '{query}'."
        lines = [f"{object_type} matching '{query}' ({len(items)}):"]
        lines += [f"  {_summarize(o, object_type)}" for o in items]
        return "\n".join(lines)
    except Exception as exc:
        return _fail(f"hubspot_search({object_type})", exc)


@tool
def hubspot_create_object(object_type: str, properties_json: str) -> str:
    """Create a HubSpot record of any object type.

    properties_json: JSON object of HubSpot property names → values, e.g.
    '{"dealname": "Acme renewal", "amount": "5000"}'.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        from hubspot.crm.objects import SimplePublicObjectInputForCreate

        props = json.loads(properties_json)
        obj = client.crm.objects.basic_api.create(
            object_type=object_type,
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            ),
        )
        return f"Created {object_type} (id={getattr(obj, 'id', '?')})."
    except Exception as exc:
        return _fail(f"hubspot_create_object({object_type})", exc)


@tool
def hubspot_update_object(object_type: str, object_id: str, properties_json: str) -> str:
    """Update a HubSpot record of any object type.

    properties_json: JSON object of property names → new values.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        from hubspot.crm.objects import SimplePublicObjectInput

        props = json.loads(properties_json)
        client.crm.objects.basic_api.update(
            object_type=object_type,
            object_id=object_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return f"Updated {object_type} {object_id}."
    except Exception as exc:
        return _fail(f"hubspot_update_object({object_type}/{object_id})", exc)


# ── Contacts (convenience) ──────────────────────────────────────────────────


@tool
def hubspot_list_contacts(limit: int = 10) -> str:
    """List HubSpot contacts."""
    return cast(str, hubspot_list_objects.invoke({"object_type": "contacts", "limit": limit}))


@tool
def hubspot_search_contacts(query: str, limit: int = 5) -> str:
    """Search HubSpot contacts by email substring."""
    return cast(
        str,
        hubspot_search.invoke(
            {"object_type": "contacts", "query": query, "property_name": "email", "limit": limit}
        ),
    )


@tool
def hubspot_create_contact(first_name: str, last_name: str, email: str, company: str = "") -> str:
    """Create a new HubSpot contact."""
    props = {"firstname": first_name, "lastname": last_name, "email": email}
    if company:
        props["company"] = company
    client, err = _hs_client()
    if err:
        return err
    try:
        from hubspot.crm.objects import SimplePublicObjectInputForCreate

        obj = client.crm.objects.basic_api.create(
            object_type="contacts",
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            ),
        )
        return f"Created contact {first_name} {last_name} (id={getattr(obj, 'id', '?')})."
    except Exception as exc:
        return _fail("hubspot_create_contact", exc)


@tool
def hubspot_update_contact(contact_id: str, properties_json: str) -> str:
    """Update a HubSpot contact. properties_json='{"firstname": "Jane"}'."""
    return cast(
        str,
        hubspot_update_object.invoke(
            {
                "object_type": "contacts",
                "object_id": contact_id,
                "properties_json": properties_json,
            }
        ),
    )


# ── Companies (convenience) ─────────────────────────────────────────────────


@tool
def hubspot_get_company(company_id: str) -> str:
    """Get a HubSpot company record by ID."""
    return cast(
        str, hubspot_get_object.invoke({"object_type": "companies", "object_id": company_id})
    )


@tool
def hubspot_create_company(name: str, domain: str = "", properties_json: str = "") -> str:
    """Create a HubSpot company. properties_json adds extra fields (optional)."""
    props: dict = {"name": name}
    if domain:
        props["domain"] = domain
    if properties_json.strip():
        try:
            props.update(json.loads(properties_json))
        except Exception as exc:
            return f"[tool-error] hubspot_create_company: bad properties_json: {exc}"
    return cast(
        str,
        hubspot_create_object.invoke(
            {"object_type": "companies", "properties_json": json.dumps(props)}
        ),
    )


# ── Deals (convenience) ─────────────────────────────────────────────────────


@tool
def hubspot_list_deals(limit: int = 10, stage: str = "") -> str:
    """List HubSpot deals, optionally filtered by deal stage."""
    if stage.strip():
        return cast(
            str,
            hubspot_search.invoke(
                {
                    "object_type": "deals",
                    "query": stage,
                    "property_name": "dealstage",
                    "limit": limit,
                }
            ),
        )
    return cast(str, hubspot_list_objects.invoke({"object_type": "deals", "limit": limit}))


@tool
def hubspot_create_deal(
    name: str, amount: float, stage: str = "appointmentscheduled", pipeline: str = "default"
) -> str:
    """Create a HubSpot deal."""
    props = {
        "dealname": name,
        "amount": str(amount),
        "dealstage": stage,
        "pipeline": pipeline,
    }
    client, err = _hs_client()
    if err:
        return err
    try:
        from hubspot.crm.objects import SimplePublicObjectInputForCreate

        obj = client.crm.objects.basic_api.create(
            object_type="deals",
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            ),
        )
        return f"Created deal '{name}' (id={getattr(obj, 'id', '?')})."
    except Exception as exc:
        return _fail("hubspot_create_deal", exc)


@tool
def hubspot_update_deal(deal_id: str, properties_json: str) -> str:
    """Update a HubSpot deal. properties_json='{"dealstage": "closedwon"}'."""
    return cast(
        str,
        hubspot_update_object.invoke(
            {"object_type": "deals", "object_id": deal_id, "properties_json": properties_json}
        ),
    )


# ── Tickets ─────────────────────────────────────────────────────────────────


@tool
def hubspot_list_tickets(limit: int = 10) -> str:
    """List HubSpot support tickets."""
    return cast(str, hubspot_list_objects.invoke({"object_type": "tickets", "limit": limit}))


@tool
def hubspot_create_ticket(
    subject: str, content: str = "", pipeline: str = "0", stage: str = "1", priority: str = ""
) -> str:
    """Create a HubSpot support ticket.

    pipeline/stage default to HubSpot's first support pipeline ("0"/"1").
    priority is optional (LOW / MEDIUM / HIGH).
    """
    props = {"subject": subject, "hs_pipeline": pipeline, "hs_pipeline_stage": stage}
    if content:
        props["content"] = content
    if priority:
        props["hs_ticket_priority"] = priority.upper()
    return cast(
        str,
        hubspot_create_object.invoke(
            {"object_type": "tickets", "properties_json": json.dumps(props)}
        ),
    )


# ── Products & line items ───────────────────────────────────────────────────


@tool
def hubspot_create_product(
    name: str, price: float = 0.0, description: str = "", sku: str = ""
) -> str:
    """Create a HubSpot product in the product library."""
    props: dict = {"name": name, "price": str(price)}
    if description:
        props["description"] = description
    if sku:
        props["hs_sku"] = sku
    return cast(
        str,
        hubspot_create_object.invoke(
            {"object_type": "products", "properties_json": json.dumps(props)}
        ),
    )


@tool
def hubspot_create_line_item(
    name: str, price: float, quantity: int = 1, product_id: str = ""
) -> str:
    """Create a HubSpot line item (a priced product on a deal/quote).

    Pass product_id to link a product from the library; associate the line
    item to a deal afterwards with hubspot_associate.
    """
    props: dict = {"name": name, "price": str(price), "quantity": str(quantity)}
    if product_id:
        props["hs_product_id"] = product_id
    return cast(
        str,
        hubspot_create_object.invoke(
            {"object_type": "line_items", "properties_json": json.dumps(props)}
        ),
    )


# ── Activities / engagements ────────────────────────────────────────────────


@tool
def hubspot_log_activity(
    activity_type: str,
    body: str,
    subject: str = "",
    associate_object_type: str = "",
    associate_object_id: str = "",
) -> str:
    """Log a CRM activity (engagement): note, task, call, email, or meeting.

    activity_type: one of note, task, call, email, meeting. Optionally pass
    associate_object_type + associate_object_id (e.g. contacts / <id>) to link
    the activity to a record.
    """
    spec = _ACTIVITY_PROPS.get(activity_type.lower().strip())
    if spec is None:
        return (
            f"[tool-error] hubspot_log_activity: unknown activity_type "
            f"'{activity_type}'. Use one of: {', '.join(_ACTIVITY_PROPS)}."
        )
    import time

    object_type = spec["_object"]
    props: dict = {"hs_timestamp": str(int(time.time() * 1000))}
    props[spec["body"]] = body
    if subject and "subject" in spec:
        props[spec["subject"]] = subject
    create_res: str = hubspot_create_object.invoke(
        {"object_type": object_type, "properties_json": json.dumps(props)}
    )
    if create_res.startswith("[") or not (associate_object_type and associate_object_id):
        return create_res
    # Extract the new id and associate it to the requested record.
    new_id = create_res.rsplit("id=", 1)[-1].rstrip(").")
    assoc = hubspot_associate.invoke(
        {
            "from_object_type": object_type,
            "from_id": new_id,
            "to_object_type": associate_object_type,
            "to_id": associate_object_id,
        }
    )
    return f"{create_res}\n{assoc}"


# ── Associations ────────────────────────────────────────────────────────────


@tool
def hubspot_associate(from_object_type: str, from_id: str, to_object_type: str, to_id: str) -> str:
    """Create the default association between two HubSpot records.

    e.g. associate a contact to a company, or a note to a deal:
    from_object_type=notes, from_id=<note>, to_object_type=deals, to_id=<deal>.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        client.crm.associations.v4.basic_api.create_default(
            from_object_type=from_object_type,
            from_object_id=from_id,
            to_object_type=to_object_type,
            to_object_id=to_id,
        )
        return (
            f"Associated {from_object_type} {from_id} ↔ {to_object_type} {to_id} "
            f"(default association)."
        )
    except Exception as exc:
        return _fail("hubspot_associate", exc)


# ── Pipelines & owners ──────────────────────────────────────────────────────


@tool
def hubspot_get_pipeline(object_type: str = "deals") -> str:
    """List pipelines and their stages for an object type (deals or tickets)."""
    client, err = _hs_client()
    if err:
        return err
    try:
        pipelines = client.crm.pipelines.pipelines_api.get_all(object_type=object_type)
        results = getattr(pipelines, "results", []) or []
        if not results:
            return f"No {object_type} pipelines found."
        lines = [f"{object_type} pipelines ({len(results)}):"]
        for p in results:
            lines.append(f"  {getattr(p, 'label', '?')} (id={getattr(p, 'id', '?')})")
            for s in getattr(p, "stages", []) or []:
                lines.append(f"    - {getattr(s, 'label', '?')} (id={getattr(s, 'id', '?')})")
        return "\n".join(lines)
    except Exception as exc:
        return _fail(f"hubspot_get_pipeline({object_type})", exc)


@tool
def hubspot_list_owners(limit: int = 50) -> str:
    """List HubSpot owners (users that records can be assigned to)."""
    client, err = _hs_client()
    if err:
        return err
    try:
        page = client.crm.owners.owners_api.get_page(limit=min(max(limit, 1), 100))
        owners = getattr(page, "results", []) or []
        if not owners:
            return "No owners found."
        lines = [f"Owners ({len(owners)}):"]
        for o in owners:
            name = " ".join(
                v for v in (getattr(o, "first_name", ""), getattr(o, "last_name", "")) if v
            ).strip()
            lines.append(
                f"  {name or '(no name)'} <{getattr(o, 'email', '')}> (id={getattr(o, 'id', '?')})"
            )
        return "\n".join(lines)
    except Exception as exc:
        return _fail("hubspot_list_owners", exc)


# ════════════════════════════════════════════════════════════════════════════
#  Extended coverage — Properties, Lists, Batch, Files, Associations,
#  Marketing, Schemas, Imports/Exports, Quotes/Invoices, Workflows, Analytics,
#  CMS, and a generic REST escape hatch.
#
#  Endpoints HubSpot ships in its Python SDK use the typed client above. The
#  ones below that aren't in the SDK (workflows, analytics, …) — or whose typed
#  models are awkward — go through ``client.api_request``, the SDK's own
#  authenticated request builder, so they still reuse the access token, base
#  URL, and headers. No second HTTP stack, no second auth path.
# ════════════════════════════════════════════════════════════════════════════


# Object-type IDs used by the v3 Lists API (it wants the numeric id, not name).
_OBJECT_TYPE_IDS: dict[str, str] = {
    "contacts": "0-1",
    "companies": "0-2",
    "deals": "0-3",
    "tickets": "0-5",
    "products": "0-7",
    "line_items": "0-8",
    "quotes": "0-14",
}


def _api(
    client: Any, method: str, path: str, body: Any = None, qs: dict | None = None
) -> tuple[Any, str | None]:
    """Authenticated raw REST call via the SDK's request builder.

    Returns ``(data, None)`` on 2xx (data = parsed JSON, or raw text) or
    ``(None, error_string)`` otherwise. Never raises for HTTP errors.
    """
    opts: dict = {"method": method.upper(), "path": path}
    if body is not None:
        opts["body"] = body
    if qs:
        opts["qs"] = qs
    try:
        resp = client.api_request(opts)
    except Exception as exc:
        return None, f"[tool-error] HubSpot request failed: {exc}"
    status = getattr(resp, "status_code", 0)
    text = getattr(resp, "text", "") or ""
    if status >= 400:
        return None, _with_scope_hint(f"[tool-error] HubSpot HTTP {status}: {text[:600]}")
    if not text:
        return {}, None
    try:
        return resp.json(), None
    except Exception:
        return text, None


def _loads(label: str, raw: str) -> tuple[Any, str | None]:
    """Parse a JSON tool argument. Returns ``(value, None)`` or ``(None, err)``."""
    try:
        return json.loads(raw), None
    except Exception as exc:
        return None, f"[tool-error] {label}: invalid JSON ({exc})."


def _fmt_list(data, label: str, name_keys: tuple[str, ...] = ()) -> str:
    """Format a REST list response (dict with 'results', or a bare list)."""
    results = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(results, list) or not results:
        return f"No {label} found."
    keys = name_keys or ("name", "label", "subject", "dealname", "title", "email")
    lines = [f"{label} ({len(results)}):"]
    for r in results:
        if isinstance(r, dict):
            rid = r.get("id") or r.get("listId") or r.get("flowId") or "?"
            props = r.get("properties", {}) or {}
            disp = next(
                (r.get(k) or props.get(k) for k in keys if r.get(k) or props.get(k)),
                "",
            )
            lines.append(f"  {disp} (id={rid})".strip())
        else:
            lines.append(f"  {r}")
    return "\n".join(lines)


# ── Tier 1: Properties ──────────────────────────────────────────────────────


@tool
def hubspot_list_properties(object_type: str = "contacts") -> str:
    """List the properties (fields) defined on a HubSpot object type."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", f"/crm/v3/properties/{object_type}")
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"No properties found for {object_type}."
    lines = [f"{object_type} properties ({len(results)}):"]
    for p in results:
        lines.append(
            f"  {p.get('name')} — {p.get('label')} "
            f"[{p.get('type')}/{p.get('fieldType')}] group={p.get('groupName')}"
        )
    return "\n".join(lines)


@tool
def hubspot_create_property(
    object_type: str,
    name: str,
    label: str,
    type: str = "string",
    field_type: str = "text",
    group_name: str = "",
    options_json: str = "",
) -> str:
    """Create a custom property (field) on a HubSpot object type.

    type: string | number | date | datetime | enumeration | bool.
    field_type: text | textarea | number | select | checkbox | date | etc.
    options_json (for enumeration): '[{"label":"A","value":"a"}]'.
    """
    client, err = _hs_client()
    if err:
        return err
    body: dict = {"name": name, "label": label, "type": type, "fieldType": field_type}
    if group_name:
        body["groupName"] = group_name
    if options_json.strip():
        opts, perr = _loads("options_json", options_json)
        if perr:
            return perr
        body["options"] = opts
    data, err = _api(client, "POST", f"/crm/v3/properties/{object_type}", body=body)
    if err:
        return err
    return f"Created property '{name}' on {object_type}."


@tool
def hubspot_create_property_group(object_type: str, name: str, label: str) -> str:
    """Create a property group (a section that groups fields) on an object type."""
    client, err = _hs_client()
    if err:
        return err
    body = {"name": name, "label": label}
    data, err = _api(client, "POST", f"/crm/v3/properties/{object_type}/groups", body=body)
    if err:
        return err
    return f"Created property group '{name}' on {object_type}."


# ── Tier 1: Lists / segments ────────────────────────────────────────────────


@tool
def hubspot_create_list(
    name: str, object_type: str = "contacts", processing_type: str = "MANUAL"
) -> str:
    """Create a HubSpot list.

    processing_type: MANUAL (static — add/remove members yourself) or DYNAMIC
    (filter-based; supply filters later in the UI or via the API).
    """
    client, err = _hs_client()
    if err:
        return err
    otid = _OBJECT_TYPE_IDS.get(object_type, object_type)
    body = {"name": name, "objectTypeId": otid, "processingType": processing_type.upper()}
    data, err = _api(client, "POST", "/crm/v3/lists/", body=body)
    if err:
        return err
    lst = data.get("list", data) if isinstance(data, dict) else {}
    return f"Created list '{name}' (id={lst.get('listId', '?')})."


@tool
def hubspot_get_list(list_id: str) -> str:
    """Get a HubSpot list's details by its ID."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", f"/crm/v3/lists/{list_id}")
    if err:
        return err
    lst = data.get("list", data) if isinstance(data, dict) else {}
    return (
        f"List {lst.get('listId', list_id)}: {lst.get('name', '?')} "
        f"(size={lst.get('size', '?')}, type={lst.get('processingType', '?')})"
    )


@tool
def hubspot_search_lists(query: str = "", limit: int = 20) -> str:
    """Search HubSpot lists by name (empty query returns recent lists)."""
    client, err = _hs_client()
    if err:
        return err
    body = {"query": query, "count": min(max(limit, 1), 100)}
    data, err = _api(client, "POST", "/crm/v3/lists/search", body=body)
    if err:
        return err
    lists = data.get("lists", []) if isinstance(data, dict) else []
    if not lists:
        return "No lists found."
    lines = [f"Lists ({len(lists)}):"]
    for lst in lists:
        lines.append(
            f"  {lst.get('name', '?')} (id={lst.get('listId', '?')}, size={lst.get('size', '?')})"
        )
    return "\n".join(lines)


@tool
def hubspot_add_to_list(list_id: str, record_ids: str) -> str:
    """Add records to a static list. record_ids: comma-separated object IDs."""
    client, err = _hs_client()
    if err:
        return err
    ids = [r.strip() for r in record_ids.split(",") if r.strip()]
    data, err = _api(client, "PUT", f"/crm/v3/lists/{list_id}/memberships/add", body=ids)
    if err:
        return err
    return f"Added {len(ids)} record(s) to list {list_id}."


@tool
def hubspot_remove_from_list(list_id: str, record_ids: str) -> str:
    """Remove records from a static list. record_ids: comma-separated object IDs."""
    client, err = _hs_client()
    if err:
        return err
    ids = [r.strip() for r in record_ids.split(",") if r.strip()]
    data, err = _api(client, "PUT", f"/crm/v3/lists/{list_id}/memberships/remove", body=ids)
    if err:
        return err
    return f"Removed {len(ids)} record(s) from list {list_id}."


@tool
def hubspot_get_list_members(list_id: str, limit: int = 50) -> str:
    """List the record IDs that are members of a HubSpot list."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client,
        "GET",
        f"/crm/v3/lists/{list_id}/memberships",
        qs={"limit": min(max(limit, 1), 250)},
    )
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"List {list_id} has no members (or none returned)."
    ids = [str(r.get("recordId", r)) for r in results]
    return f"List {list_id} members ({len(ids)}): " + ", ".join(ids)


# ── Tier 1: Batch operations ────────────────────────────────────────────────


@tool
def hubspot_batch_create(object_type: str, records_json: str) -> str:
    """Create many records in one call.

    records_json: JSON array of property maps, e.g.
    '[{"email":"a@x.io"},{"email":"b@x.io"}]'.
    """
    client, err = _hs_client()
    if err:
        return err
    records, perr = _loads("records_json", records_json)
    if perr:
        return perr
    inputs = [{"properties": r} for r in records]
    data, err = _api(
        client, "POST", f"/crm/v3/objects/{object_type}/batch/create", body={"inputs": inputs}
    )
    if err:
        return err
    created = data.get("results", []) if isinstance(data, dict) else []
    ids = [r.get("id", "?") for r in created]
    return f"Batch-created {len(ids)} {object_type}: ids={', '.join(map(str, ids))}"


@tool
def hubspot_batch_update(object_type: str, records_json: str) -> str:
    """Update many records in one call.

    records_json: JSON array of {id, properties}, e.g.
    '[{"id":"1","properties":{"firstname":"A"}}]'.
    """
    client, err = _hs_client()
    if err:
        return err
    records, perr = _loads("records_json", records_json)
    if perr:
        return perr
    data, err = _api(
        client, "POST", f"/crm/v3/objects/{object_type}/batch/update", body={"inputs": records}
    )
    if err:
        return err
    updated = data.get("results", []) if isinstance(data, dict) else []
    return f"Batch-updated {len(updated)} {object_type}."


@tool
def hubspot_batch_read(object_type: str, ids: str, properties: str = "") -> str:
    """Read many records by ID in one call. ids: comma-separated object IDs."""
    client, err = _hs_client()
    if err:
        return err
    id_list = [{"id": i.strip()} for i in ids.split(",") if i.strip()]
    body: dict = {"inputs": id_list}
    props = _props_for(object_type, properties)
    if props:
        body["properties"] = props
    data, err = _api(client, "POST", f"/crm/v3/objects/{object_type}/batch/read", body=body)
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"No {object_type} found."
    lines = [f"{object_type} ({len(results)}):"]
    for r in results:
        p = r.get("properties", {}) or {}
        disp = p.get("dealname") or p.get("name") or p.get("email") or ""
        lines.append(f"  {disp} (id={r.get('id', '?')})".strip())
    return "\n".join(lines)


# ── Tier 1: Files ───────────────────────────────────────────────────────────


@tool
def hubspot_upload_file(
    file_path: str, folder_path: str = "/", access: str = "PUBLIC_INDEXABLE"
) -> str:
    """Upload a file to the HubSpot File Manager and return its URL.

    access: PUBLIC_INDEXABLE | PUBLIC_NOT_INDEXABLE | PRIVATE.
    """
    client, err = _hs_client()
    if err:
        return err
    try:
        result = client.files.files_api.upload(
            file=file_path,
            folder_path=folder_path,
            options=json.dumps({"access": access}),
        )
        return (
            f"Uploaded file (id={getattr(result, 'id', '?')}, url={getattr(result, 'url', '?')})."
        )
    except Exception as exc:
        return _fail("hubspot_upload_file", exc)


# ── Tier 1: Associations (labeled + listing) ────────────────────────────────


@tool
def hubspot_list_association_labels(from_object_type: str, to_object_type: str) -> str:
    """List the available association labels/type IDs between two object types."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client, "GET", f"/crm/v4/associations/{from_object_type}/{to_object_type}/labels"
    )
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"No association labels between {from_object_type} and {to_object_type}."
    lines = [f"{from_object_type} → {to_object_type} association types:"]
    for a in results:
        lines.append(
            f"  typeId={a.get('typeId')} category={a.get('category')} "
            f"label={a.get('label') or '(unlabeled)'}"
        )
    return "\n".join(lines)


@tool
def hubspot_list_associations(from_object_type: str, from_id: str, to_object_type: str) -> str:
    """List records of one type associated with a given record."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client,
        "GET",
        f"/crm/v4/objects/{from_object_type}/{from_id}/associations/{to_object_type}",
    )
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"No {to_object_type} associated with {from_object_type} {from_id}."
    ids = [str(r.get("toObjectId", r.get("to_object_id", "?"))) for r in results]
    return f"{to_object_type} associated with {from_object_type} {from_id}: " + ", ".join(ids)


@tool
def hubspot_associate_labeled(
    from_object_type: str,
    from_id: str,
    to_object_type: str,
    to_id: str,
    association_type_id: int,
    category: str = "HUBSPOT_DEFINED",
) -> str:
    """Create a *labeled* association (use hubspot_list_association_labels for IDs).

    category: HUBSPOT_DEFINED | USER_DEFINED | INTEGRATOR_DEFINED.
    For a plain default link, use hubspot_associate instead.
    """
    client, err = _hs_client()
    if err:
        return err
    body = [{"associationCategory": category, "associationTypeId": association_type_id}]
    data, err = _api(
        client,
        "PUT",
        f"/crm/v4/objects/{from_object_type}/{from_id}/associations/{to_object_type}/{to_id}",
        body=body,
    )
    if err:
        return err
    return (
        f"Associated {from_object_type} {from_id} ↔ {to_object_type} {to_id} "
        f"(typeId={association_type_id})."
    )


# ── Tier 1: Timeline events ─────────────────────────────────────────────────


@tool
def hubspot_create_timeline_event(
    event_template_id: str, tokens_json: str, object_id: str = "", email: str = ""
) -> str:
    """Create a custom timeline event on a CRM record.

    Requires a timeline event template (created for your app) — pass its
    event_template_id. Identify the record by object_id OR email. tokens_json
    maps the template's token names to values.
    """
    client, err = _hs_client()
    if err:
        return err
    tokens, perr = _loads("tokens_json", tokens_json)
    if perr:
        return perr
    body: dict = {"eventTemplateId": event_template_id, "tokens": tokens}
    if object_id:
        body["objectId"] = object_id
    elif email:
        body["email"] = email
    else:
        return "[tool-error] hubspot_create_timeline_event: provide object_id or email."
    data, err = _api(client, "POST", "/crm/v3/timeline/events", body=body)
    if err:
        return err
    return f"Created timeline event (template={event_template_id})."


# ── Tier 2: Transactional email (actually sends) ────────────────────────────


@tool
def hubspot_send_transactional_email(
    email_id: int, to_email: str, custom_properties_json: str = ""
) -> str:
    """Send a transactional (single-send) email from a saved HubSpot email.

    email_id: the numeric ID of a transactional email asset in HubSpot.
    Requires the Transactional Email add-on. custom_properties_json fills
    merge tokens, e.g. '{"order_id":"123"}'.
    """
    client, err = _hs_client()
    if err:
        return err
    body: dict = {"emailId": email_id, "message": {"to": to_email}}
    if custom_properties_json.strip():
        cp, perr = _loads("custom_properties_json", custom_properties_json)
        if perr:
            return perr
        body["customProperties"] = cp
    data, err = _api(client, "POST", "/marketing/v3/transactional/single-email/send", body=body)
    if err:
        return err
    status = data.get("status", "sent") if isinstance(data, dict) else "sent"
    return f"Transactional email {email_id} → {to_email}: {status}."


# ── Tier 2: Forms ───────────────────────────────────────────────────────────


@tool
def hubspot_list_forms(limit: int = 20) -> str:
    """List HubSpot marketing forms."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/marketing/v3/forms/", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    return _fmt_list(data, "Forms")


@tool
def hubspot_get_form_submissions(form_id: str, limit: int = 20) -> str:
    """Get recent submissions for a HubSpot form by its GUID."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client,
        "GET",
        f"/form-integrations/v1/submissions/forms/{form_id}",
        qs={"limit": min(max(limit, 1), 50)},
    )
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"No submissions for form {form_id}."
    lines = [f"Form {form_id} submissions ({len(results)}):"]
    for s in results:
        values = s.get("values", [])
        summary = "; ".join(f"{v.get('name')}={v.get('value')}" for v in values[:6])
        lines.append(f"  [{s.get('submittedAt', '?')}] {summary}")
    return "\n".join(lines)


# ── Tier 2: Schemas / custom objects ────────────────────────────────────────


@tool
def hubspot_list_schemas() -> str:
    """List custom object schemas defined in this HubSpot account."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/crm/v3/schemas")
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return "No custom object schemas found."
    lines = [f"Custom object schemas ({len(results)}):"]
    for s in results:
        lines.append(
            f"  {s.get('name')} (objectTypeId={s.get('objectTypeId')}, "
            f"label={(s.get('labels') or {}).get('singular', '?')})"
        )
    return "\n".join(lines)


@tool
def hubspot_create_schema(definition_json: str) -> str:
    """Create a custom object schema. definition_json is the full schema spec.

    Minimal shape: '{"name":"car","labels":{"singular":"Car","plural":"Cars"},
    "primaryDisplayProperty":"model","properties":[{"name":"model","label":
    "Model","type":"string","fieldType":"text"}],"associatedObjects":["CONTACT"]}'.
    """
    client, err = _hs_client()
    if err:
        return err
    definition, perr = _loads("definition_json", definition_json)
    if perr:
        return perr
    data, err = _api(client, "POST", "/crm/v3/schemas", body=definition)
    if err:
        return err
    otid = data.get("objectTypeId", "?") if isinstance(data, dict) else "?"
    return f"Created custom object schema (objectTypeId={otid})."


# ── Tier 2: Exports / imports ───────────────────────────────────────────────


@tool
def hubspot_start_export(
    object_type: str = "contacts", export_format: str = "CSV", properties: str = ""
) -> str:
    """Start an async CRM export. Returns a task ID to poll with hubspot_get_export_status."""
    client, err = _hs_client()
    if err:
        return err
    body: dict = {
        "exportType": "VIEW",
        "format": export_format.upper(),
        "objectType": object_type,
        "language": "EN",
    }
    props = _props_for(object_type, properties)
    if props:
        body["exportName"] = f"{object_type}-export"
        body["objectProperties"] = props
    data, err = _api(client, "POST", "/crm/v3/exports/export/async", body=body)
    if err:
        return err
    task_id = data.get("id", "?") if isinstance(data, dict) else "?"
    return f"Started {object_type} export (taskId={task_id})."


@tool
def hubspot_get_export_status(task_id: str) -> str:
    """Check an async export's status; returns a download URL when complete."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", f"/crm/v3/exports/export/async/tasks/{task_id}/status")
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    return f"Export {task_id}: status={data.get('status')} url={data.get('result', '(pending)')}"


@tool
def hubspot_list_imports(limit: int = 20) -> str:
    """List recent CRM import jobs and their status."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/crm/v3/imports/", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return "No imports found."
    lines = [f"Imports ({len(results)}):"]
    for i in results:
        lines.append(f"  {i.get('importName', '?')} (id={i.get('id')}, state={i.get('state')})")
    return "\n".join(lines)


@tool
def hubspot_get_import_status(import_id: str) -> str:
    """Check a CRM import job's status by ID."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", f"/crm/v3/imports/{import_id}")
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    return (
        f"Import {import_id}: state={data.get('state')} "
        f"name={data.get('importName')} "
        f"counters={data.get('metadata', {}).get('counters', {})}"
    )


# ── Tier 2: Quotes & invoices ───────────────────────────────────────────────


@tool
def hubspot_list_quotes(limit: int = 10) -> str:
    """List HubSpot quotes."""
    return cast(
        str,
        hubspot_list_objects.invoke(
            {
                "object_type": "quotes",
                "limit": limit,
                "properties": "hs_title,hs_status,hs_expiration_date",
            }
        ),
    )


@tool
def hubspot_create_quote(title: str, expiration_date: str = "", properties_json: str = "") -> str:
    """Create a HubSpot quote. expiration_date: YYYY-MM-DD. Associate line
    items / a deal afterwards with hubspot_associate."""
    props: dict = {"hs_title": title}
    if expiration_date:
        props["hs_expiration_date"] = expiration_date
    if properties_json.strip():
        extra, perr = _loads("properties_json", properties_json)
        if perr:
            return perr
        props.update(extra)
    return cast(
        str,
        hubspot_create_object.invoke(
            {"object_type": "quotes", "properties_json": json.dumps(props)}
        ),
    )


@tool
def hubspot_list_invoices(limit: int = 10) -> str:
    """List HubSpot invoices (read-only)."""
    return cast(
        str,
        hubspot_list_objects.invoke(
            {
                "object_type": "invoices",
                "limit": limit,
                "properties": "hs_invoice_status,hs_amount_billed,hs_due_date",
            }
        ),
    )


# ── Tier 3: Workflows (Automation v4 — raw REST, beta) ──────────────────────


@tool
def hubspot_list_workflows(limit: int = 25) -> str:
    """List automation workflows (flows)."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/automation/v4/flows", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    return _fmt_list(data, "Workflows")


@tool
def hubspot_get_workflow(workflow_id: str) -> str:
    """Get an automation workflow (flow) definition by ID."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", f"/automation/v4/flows/{workflow_id}")
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    actions = data.get("actions", [])
    return (
        f"Workflow {data.get('id', workflow_id)}: {data.get('name', '?')} "
        f"(type={data.get('type')}, enabled={data.get('isEnabled')}, "
        f"{len(actions)} action(s))"
    )


@tool
def hubspot_create_workflow(definition_json: str) -> str:
    """Create an automation workflow (flow). definition_json is the full v4 spec.

    Set "type" to CONTACT_FLOW for contact-based workflows or PLATFORM_FLOW for
    all others (deal/ticket/goal-based). This v4 endpoint is in beta; complex
    flows may need the HubSpot UI. Some flows require sensitive-data scopes.
    """
    client, err = _hs_client()
    if err:
        return err
    definition, perr = _loads("definition_json", definition_json)
    if perr:
        return perr
    data, err = _api(client, "POST", "/automation/v4/flows", body=definition)
    if err:
        return err
    fid = data.get("id", "?") if isinstance(data, dict) else "?"
    return f"Created workflow (id={fid}). Verify in HubSpot → Automation → Workflows."


@tool
def hubspot_enroll_in_workflow(workflow_id: str, email: str) -> str:
    """Enroll a contact (by email) into an existing workflow."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client,
        "POST",
        f"/automation/v2/workflows/{workflow_id}/enrollments/contacts/{email}",
    )
    if err:
        return err
    return f"Enrolled {email} into workflow {workflow_id}."


# ── Tier 3: Analytics (read-only — there is no create-dashboard API) ────────


@tool
def hubspot_get_analytics(
    breakdown: str = "totals", time_period: str = "total", start_date: str = "", end_date: str = ""
) -> str:
    """Read traffic/analytics data (read-only — HubSpot has no API to create
    dashboards or reports).

    breakdown: totals | sources | geolocation | utm-campaigns | sessions | …
    time_period: total | daily | weekly | monthly | summarize/daily.
    start_date/end_date: YYYYMMDD (required for non-'total' periods).
    """
    client, err = _hs_client()
    if err:
        return err
    qs: dict = {}
    if start_date:
        qs["start"] = start_date
    if end_date:
        qs["end"] = end_date
    data, err = _api(client, "GET", f"/analytics/v2/reports/{breakdown}/{time_period}", qs=qs)
    if err:
        return err
    text = json.dumps(data, indent=2) if isinstance(data, dict | list) else str(data)
    return f"Analytics {breakdown}/{time_period}:\n{text[:1500]}"


# ── Tier 3: CMS (blog, pages, HubDB, redirects) ─────────────────────────────


@tool
def hubspot_list_blog_posts(limit: int = 10) -> str:
    """List CMS blog posts."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/cms/v3/blogs/posts", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    return _fmt_list(data, "Blog posts")


@tool
def hubspot_create_blog_post(definition_json: str) -> str:
    """Create a CMS blog post. definition_json is the full post spec.

    Minimal: '{"name":"My title","contentGroupId":"<blogId>","postBody":
    "<p>Hello</p>","state":"DRAFT"}'. Use hubspot_api_request GET
    /cms/v3/blogs/posts to discover contentGroupId values.
    """
    client, err = _hs_client()
    if err:
        return err
    definition, perr = _loads("definition_json", definition_json)
    if perr:
        return perr
    data, err = _api(client, "POST", "/cms/v3/blogs/posts", body=definition)
    if err:
        return err
    return f"Created blog post (id={data.get('id', '?') if isinstance(data, dict) else '?'})."


@tool
def hubspot_list_pages(page_type: str = "site-pages", limit: int = 10) -> str:
    """List CMS pages. page_type: site-pages | landing-pages."""
    client, err = _hs_client()
    if err:
        return err
    pt = page_type if page_type in ("site-pages", "landing-pages") else "site-pages"
    data, err = _api(client, "GET", f"/cms/v3/pages/{pt}", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    return _fmt_list(data, pt)


@tool
def hubspot_list_hubdb_tables() -> str:
    """List HubDB tables."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/cms/v3/hubdb/tables")
    if err:
        return err
    return _fmt_list(data, "HubDB tables")


@tool
def hubspot_get_hubdb_rows(table_id: str, limit: int = 25) -> str:
    """Get rows from a HubDB table by table ID or name."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(
        client,
        "GET",
        f"/cms/v3/hubdb/tables/{table_id}/rows",
        qs={"limit": min(max(limit, 1), 100)},
    )
    if err:
        return err
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        return f"HubDB table {table_id} has no rows."
    lines = [f"HubDB {table_id} rows ({len(results)}):"]
    for r in results:
        lines.append(f"  id={r.get('id')} values={r.get('values', {})}")
    return "\n".join(lines)


@tool
def hubspot_create_url_redirect(from_path: str, to_url: str, redirect_style: int = 301) -> str:
    """Create a URL redirect in the CMS. redirect_style: 301 or 302."""
    client, err = _hs_client()
    if err:
        return err
    body = {"routePrefix": from_path, "destination": to_url, "redirectStyle": redirect_style}
    data, err = _api(client, "POST", "/cms/v3/url-redirects", body=body)
    if err:
        return err
    return f"Created redirect {from_path} → {to_url} ({redirect_style})."


# ── Tier 3: Marketing emails ────────────────────────────────────────────────


@tool
def hubspot_list_marketing_emails(limit: int = 20) -> str:
    """List marketing emails."""
    client, err = _hs_client()
    if err:
        return err
    data, err = _api(client, "GET", "/marketing/v3/emails/", qs={"limit": min(max(limit, 1), 100)})
    if err:
        return err
    return _fmt_list(data, "Marketing emails")


# ── Generic escape hatch — any HubSpot endpoint ─────────────────────────────


@tool
def hubspot_api_request(method: str, path: str, body_json: str = "", query_json: str = "") -> str:
    """Call ANY HubSpot REST endpoint not covered by a dedicated tool.

    Use for campaigns, conversations, communication preferences, marketing
    events, webhooks, or any newer API. The access token is applied for you.

    method: GET | POST | PUT | PATCH | DELETE.
    path: e.g. '/marketing/v3/campaigns' (leading slash, no host).
    body_json / query_json: optional JSON strings for the request body / query.
    """
    client, err = _hs_client()
    if err:
        return err
    body = None
    if body_json.strip():
        body, perr = _loads("body_json", body_json)
        if perr:
            return perr
    qs = None
    if query_json.strip():
        qs, perr = _loads("query_json", query_json)
        if perr:
            return perr
    data, err = _api(client, method, path, body=body, qs=qs)
    if err:
        return err
    text = json.dumps(data, indent=2) if isinstance(data, dict | list) else str(data)
    return f"{method.upper()} {path} → OK\n{text[:2000]}"


get_tools, get_tool = make_toolkit_api(globals())
